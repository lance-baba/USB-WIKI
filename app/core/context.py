"""应用上下文 —— 统一装配数据库、嵌入源、AI 网关、增量同步器与运行期告警。

启动顺序（对应 PRD 4.1）：
    WAL 残留自愈 → 打开主库 → 建 Schema → 解析嵌入源 → 签名守卫比对 → 启动同步器
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .db import SCHEMA_VERSION  # noqa: E402
from . import migrations as migrations_mod  # noqa: E402
from . import config, db as db_mod, embedder as embedder_mod, indexer, llm, net_util, ollama_runtime, paths, proc as proc_mod, sync
from .log_util import get_logger

log = get_logger()


@dataclass
class AppContext:
    db: db_mod.Database | None = None
    embedder: embedder_mod.BaseEmbedder | None = None
    embedder_source: str = "none"
    #: 随包嵌入资源（仅 local_onnx 时有值）—— 供 BUILD_INFO / diagnostics 复用
    embedding_resource: object | None = None
    #: 没用到「配置指定的嵌入源」时的原因（diagnostics 的 fallback_reason）
    embedding_fallback_reason: str = ""
    gateway: llm.Gateway | None = None
    syncer: sync.NoteSyncer | None = None
    warnings: list[str] = field(default_factory=list)   # 需用户行动 → 界面告警条
    notes: list[str] = field(default_factory=list)      # 系统已自愈 → 设置页「运行详情」
    started_at: float = 0.0
    boot_report: dict = field(default_factory=dict)
    _boot_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 索引结构升级状态（由 boot 的版本检查填写）
    _needs_full_rebuild: bool = False
    _shutting_down: bool = False      # 初始化期间收到退出请求时置位，供 boot 提前收手
    _migration: dict = field(default_factory=dict)
    #: 离线授权状态（boot 时重算设备指纹并验签；见 app/core/license.py）。
    #  只读快照，供 /api/status 的顶栏与设置页展示；**不参与任何功能放行**。
    license: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def boot(self, db_path=None, start_syncer: bool = True, probe_ollama: bool = True) -> dict:
        # 0) 子进程「一律不弹窗」的进程级默认（Windows）—— 必须早于任何可能
        #    import 第三方重量级库（如 onnxruntime）的动作，否则第三方内部
        #    的 platform.system() → cmd /c ver 会闪出黑框。见 app/core/proc.py。
        proc_mod.install_silent_subprocess()
        with self._boot_lock:
            if self.db is not None:
                return self.boot_report

            t0 = time.time()
            paths.ensure_dirs()

            # 1) WAL 残留自愈（必须在任何连接建立之前）
            healed = db_mod.wal_self_heal(db_path)

            # 1.5) 索引结构版本检查 —— **必须早于任何建表动作**。
            # 旧代码顺序是「先跑完 CREATE TABLE IF NOT EXISTS 再写版本号」，那会让程序先
            # 部分修改旧库、之后才发现版本不对；版本号还会被 INSERT OR REPLACE 静默改写，
            # 从此查不出它原本是哪一版。现在改为：只读探测 → 判版本 → 决定动作。
            self._needs_full_rebuild = False
            # db_path 可为 None（用默认位置）—— 必须显式解析，
            # 否则探测会拿到 None 而抛 TypeError。
            _cache_path = db_path or paths.CACHE_DB
            self._migration = migrations_mod.rebuild_cache_if_incompatible(_cache_path)
            _act = self._migration.get("action")
            if _act == "rebuild":
                # 索引结构已变：旧库已备份并清空，稍后 Schema 建好再全量重建
                self._needs_full_rebuild = True
                self.notes.append(self._migration.get("message", ""))
            elif _act == "create":
                self.notes.append(self._migration.get("message", ""))

            # 2) 打开主库（此时仅建立连接，Schema 推迟到确定真实向量维度之后再建）
            dim = config.get_int("AI", "embedding_dim", 512)
            self.db = db_mod.get_db(db_path=db_path, embedding_dim=dim)

            # 3) 先建网关（其 Ollama 健康探测带 60s 缓存），供嵌入源解析复用
            self.gateway = llm.get_gateway(db=self.db, embedder=None)

            # 4) 解析嵌入源（含 onnxruntime AVX2 防护 / 逐级降级 / 维度自动探测）
            resolved = embedder_mod.resolve(
                config.get,
                ollama_healthy=self.gateway.ollama_status,
            )
            self.embedder = resolved.embedder
            self.embedder_source = resolved.source
            self.embedding_resource = resolved.resource
            self.embedding_fallback_reason = resolved.fallback_reason
            self.warnings.extend(resolved.warnings)
            self.notes.extend(resolved.notes)

            # 初始化期间用户已经点了「安全退出」→ 提前收手。
            # 否则下面会去写一个已被 shutdown 置空的 gateway，
            # 抛 `'NoneType' object has no attribute 'embedder'`，
            # 而且那次异常会被当成「初始化失败」上报给用户。
            if self._shutting_down:
                log.info("初始化期间收到退出请求，已中止后续初始化")
                return self.boot_report

            self.gateway.embedder = self.embedder

            # 5) ⚠ 用真实维度建向量表。Ollama / API 的维度由模型决定（自动探测得到），
            #    若仍沿用 config 里的 embedding_dim，会出现「表按 512 建、向量 768 维
            #    全部被拒写」的静默失效 —— 向量路直接变 0 候选。
            actual_dim = int(getattr(self.embedder, "dim", dim) or dim)
            if actual_dim != self.db.embedding_dim:
                log.info("向量表维度跟随嵌入模型：%d -> %d", self.db.embedding_dim, actual_dim)
                self.db.embedding_dim = actual_dim
            if self._shutting_down:      # 上面的嵌入源解析可能耗时较久
                log.info("初始化期间收到退出请求，已中止建表")
                return self.boot_report

            self.db.init_schema()

            # 3.5) 结构升级后的一次性全量重建。
            # 用重建而非增量迁移：Markdown 是真相源、cache.db 是纯派生索引，维护 ALTER 链
            # 只会增加错误面。注意：**只重建索引，绝不改动 data/notes**。
            if self._needs_full_rebuild:
                try:
                    from . import indexer as _indexer  # noqa: PLC0415

                    rep = _indexer.rebuild_all(self.db, self.embedder)
                    log.info("结构升级后全量重建完成: %s", rep)
                    self.notes.append(
                        f"索引已按新结构重建：{rep.get('indexed', 0)} 篇 / "
                        f"{rep.get('chunks', 0)} 切片"
                    )
                except Exception as exc:  # noqa: BLE001 - 重建失败不阻断启动，但必须明示
                    log.error("结构升级后全量重建失败: %s", exc)
                    self.warnings.append(
                        "索引结构已升级，但全量重建失败，请到「设置」页手动点一次「全量重建索引」"
                    )
                finally:
                    self._needs_full_rebuild = False

            # 6) 向量空间签名守卫
            #    bundled local_onnx 用**资源自身的 id** 作为 model 段（不再读配置里的
            #    embedding_model_name —— 那个键是给 ollama / api 嵌入源用的），
            #    并带上 artifact / tokenizer 的 SHA256 与精度：只记「模型名 + 维度」
            #    区分不了「同名但字节换了」的 artifact。
            if self.embedder is not None:
                sig_model = (resolved.resource.id if resolved.resource is not None
                             else config.get_str("AI", "embedding_model_name", ""))
                try:
                    extra = self.embedder.signature_extra() or {}
                except Exception as exc:  # noqa: BLE001 - 只影响签名粒度
                    log.debug("读取嵌入签名附加字段失败: %s", exc)
                    extra = {}
                mismatch = self.db.check_signature(resolved.source, sig_model,
                                                   actual_dim, extra=extra)
                if mismatch:
                    self.warnings.append(mismatch + "，需点击「全量重建索引」")
                    self.db.signature_mismatch = mismatch
            else:
                # 兜底降级（无任何嵌入源）：绝不动已有向量索引，也不提示重建。
                # 否则会形成「Ollama 关→哈希签名覆盖→Ollama 开→又要重建」的破坏性循环。
                stored_dim = self.db.get_meta(db_mod.META_VEC_DIM)
                if stored_dim and stored_dim != str(self.db.embedding_dim):
                    self.db.signature_mismatch = (
                        "向量嵌入暂不可用"
                        f"（{resolved.fallback_reason or '本机没有可用的嵌入源'}），"
                        f"已退化为纯 FTS5 词法检索；原 {stored_dim} 维向量索引已保留，"
                        f"恢复嵌入源后自动恢复向量召回"
                    )

            # 7) 预解析一次 AI 提供方，让状态面板显示「真实可用」而非初始占位值。
            #    注意 resolve_provider 只返回结果，必须手动回写 last_provider。
            #    在这之前先把「Ollama 静默自启动」丢到后台 —— 它会（若需要）拉起
            #    ollama serve 并刷新健康缓存；本步不阻塞，拿到的可能是「未在线」，
            #    随后前端轮询会自然收敛到真实状态。
            if probe_ollama:
                threading.Thread(target=self._autostart_ollama, daemon=True).start()
            try:
                _prov, _warns = self.gateway.resolve_provider()
                self.gateway.state.last_provider = _prov
            except Exception as exc:  # noqa: BLE001 - 只影响展示，不影响功能
                log.debug("预解析 AI 提供方失败: %s", exc)

            # 8) 后台健康探测（异步，不阻塞首屏）
            if probe_ollama:
                threading.Thread(
                    target=lambda: self.gateway.ollama_status(force=True), daemon=True
                ).start()

            # 9) 外部文件增量同步
            if start_syncer:
                self.syncer = sync.NoteSyncer(self.db, self.embedder)
                self.syncer.start()

            # 10) 离线授权校验：重算本机设备指纹 + 用内置公钥验签。
            #     需求：每次启动都重算并校验。只读、不写盘、**绝不阻断启动** ——
            #     授权不足是「功能策略」，不是启动故障。异常一律吞掉只记 debug。
            try:
                from . import license as _license  # noqa: PLC0415

                self.license = _license.status()
            except Exception as exc:  # noqa: BLE001
                log.debug("授权状态解析失败（忽略）: %s", exc)
                self.license = {}

            self.started_at = time.time()
            # 两个版本**语义不同，分开返回**：
            # app_version = 用户拿到的软件版本（三段式）
            # schema_version = cache.db 的结构兼容版本
            # 只给一个模糊的 "version" 会让以后排障分不清是哪个。
            from app.version import APP_VERSION  # noqa: PLC0415

            self.boot_report = {
                "app_version": APP_VERSION,
                "schema_version": migrations_mod.CURRENT_SCHEMA_VERSION,
                "wal_self_healed": healed,
                "embedding_source": resolved.source,
                "embedding_dim": actual_dim,
                # 随包资源 id（未启用时为 None）+ 未用上配置来源的原因
                "embedding_id": getattr(resolved.resource, "id", None),
                "embedding_fallback_reason": resolved.fallback_reason,
                "vec_ready": bool(self.db.vec_table_ready and not self.db.signature_mismatch),
                "boot_ms": int((time.time() - t0) * 1000),
                "warnings": list(self.warnings),
                "notes": list(self.notes),
            }
            for w in self.warnings:
                log.warning("启动告警: %s", w)
            for nt in self.notes:
                log.info("启动说明: %s", nt)
            log.info(
                "上下文就绪 (%.0fms) 嵌入源=%s 向量=%s",
                (time.time() - t0) * 1000, resolved.source, self.boot_report["vec_ready"],
            )
            return self.boot_report

    # ------------------------------------------------------------------
    def shutdown(self) -> dict:
        report: dict = {"syncer": False, "db": {}}
        # 先置位：boot 线程可能在初始化中途，它据此提前收手，
        # 避免去操作马上要被置空的 gateway / db（实测会抛 AttributeError）。
        self._shutting_down = True
        if self.syncer is not None:
            try:
                self.syncer.stop()
                report["syncer"] = True
            except Exception as exc:  # noqa: BLE001
                log.error("同步器停止异常: %s", exc)
            self.syncer = None
        if self.db is not None:
            report["db"] = self.db.checkpoint_and_close()
            db_mod._db = None  # noqa: SLF001 - 释放全局单例，保证下次启动重新自愈
            self.db = None
        self.gateway = None
        self.embedder = None
        log.info("应用上下文已安全退出")
        return report

    def rebuild_index(self, recreate_vec: bool = False) -> dict:
        if self.db is None:
            return {"ok": False, "error": "上下文未初始化"}
        return indexer.rebuild_all(self.db, self.embedder, recreate_vec=recreate_vec)

    def ai_readiness(self, probe: bool = False) -> dict:
        """Level 3 对话能力就绪状态。

        ``probe=False``（默认）读网关缓存；网关侧会在缓存超过 HEALTH_TTL
        过期时自动重探一次（TTL 门控，最多每 60s 发一次请求）——
        这让「应用启动后才开启 Ollama」的场景能在 ≤60s 内自动转为在线。
        ``probe=True`` 绕过缓存现场重探（/api/status?probe=1）。
        """
        if self.gateway is None:
            return {
                "state": llm.STATE_NO_RUNTIME, "reason": "服务尚未初始化完成",
                "chat_ready": False, "ollama_available": False, "ollama_probed": False,
                "installed_models": [], "installed_model_count": 0,
                "selected_model": None, "selected_model_installed": False,
                "cloud_api_configured": False,
            }
        try:
            ready = self.gateway.ai_readiness(probe=probe)
        except Exception as exc:  # noqa: BLE001 - 状态展示失败不得影响 /api/status
            log.debug("AI 就绪状态解析失败: %s", exc)
            return {
                "state": llm.STATE_NO_RUNTIME, "reason": "就绪状态解析失败",
                "chat_ready": False, "ollama_available": False, "ollama_probed": False,
                "installed_models": [], "installed_model_count": 0,
                "selected_model": None, "selected_model_installed": False,
                "selected_model_capabilities": [],
                "cloud_api_configured": False,
            }
        return {
            k: ready[k] for k in (
                "state", "reason", "chat_ready", "ollama_available", "ollama_probed",
                "installed_models", "installed_model_count", "selected_model",
                "selected_model_installed", "selected_model_capabilities",
                "cloud_api_configured",
            )
        }

    def _autostart_ollama(self) -> None:
        """后台把 Ollama 拉起来（配置 ``ollama_autostart=0`` 时只探测不拉起）。

        为什么要有它：产品把本地 AI 全押在 Ollama 上，但 Ollama 默认**不是**开机自启的
        系统服务 —— 用户不点开托盘程序，`/api/tags` 就连不上，本地对话与向量检索一起
        降级，而用户很难自己想到「得先去开 Ollama」。这里静默补上这一步。

        铁律：**只在自己的线程里做**，绝不阻塞启动；找不到 / 起不来一律按降级链继续，
        只往 notes 里留一句实话，不抛异常、不改任何既有行为。
        """
        try:
            enabled = config.get_bool("AI", "ollama_autostart", True)
        except Exception:  # noqa: BLE001 - 配置读不到就按默认开
            enabled = True
        host = config.get_str("AI", "ollama_host", "http://127.0.0.1:11434")
        try:
            res = ollama_runtime.ensure_running(host, enabled=enabled)
        except Exception as exc:  # noqa: BLE001 - 自启动失败绝不能影响应用
            log.debug("Ollama 自启动异常（已忽略）: %s", exc)
            return
        try:
            if res.get("started"):
                self.notes.append(
                    f"已自动启动本地 Ollama（等待 {res.get('waited_s')}s 后就绪）")
            elif enabled and not res.get("already"):
                # 如实告知「没拉起」，用户才有线索去自查（比如没装 Ollama）
                self.notes.append(f"未能自动启动本地 Ollama：{res.get('reason')}")
            # 刷新健康与模型缓存 —— 否则前端轮询拿到的还是启动时的「未在线」
            if self.gateway is not None:
                self.gateway.ollama_status(force=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("自启动后刷新状态失败: %s", exc)

    def status(self, probe: bool = False) -> dict:
        if self.db is None:
            return {"ready": False}
        stats = self.db.stats()
        sig = self.db.get_signature()
        gw_state = self.gateway.state if self.gateway else None
        # 顶栏「AI 对话」标签：按当前配置 + 就绪状态实时推导（详见下方 "resolved" 注释）
        mode = config.get_str("AI", "provider", "auto").strip().lower()
        readiness = self.ai_readiness(probe=probe)
        has_key = bool(config.get_str("AI", "api_key", ""))
        if mode == "offline":
            resolved_now = "offline"
        elif mode == "api":
            resolved_now = "api" if has_key else "error"
        elif mode == "ollama":
            resolved_now = "ollama" if readiness.get("chat_ready") else "offline"
        elif readiness.get("chat_ready"):
            resolved_now = "ollama"
        elif has_key:
            resolved_now = "api"
        else:
            resolved_now = "offline"
        return {
            "app_version": __import__("app.version", fromlist=["x"]).APP_VERSION,
            "schema_version": migrations_mod.CURRENT_SCHEMA_VERSION,
            "ready": True,
            "port": self.port,
            "uptime": round(time.time() - (self.started_at or time.time()), 1),
            "db": stats,
            "embedding": {
                "source": self.embedder_source,
                "dim": getattr(self.embedder, "dim", None),
                "signature": sig,
                # 随包资源身份（未启用时 None）—— 用户能看出「用的是哪一个 artifact」
                "id": getattr(self.embedding_resource, "id", None),
                "precision": getattr(self.embedding_resource, "precision", None),
                "artifact_sha256": (getattr(self.embedding_resource, "artifact_sha256", "") or "")[:16] or None,
                "fallback_reason": self.embedding_fallback_reason or None,
            },
            "ai": {
                "provider_mode": mode,
                # ⚠ ollama_healthy 只表示「/api/tags 可访问」，不代表能对话；
                #   能否对话看 state / chat_ready（A4.1 契约）。
                "ollama_healthy": bool(gw_state and gw_state.ollama_healthy),
                "ollama_detail": gw_state.ollama_detail if gw_state else "",
                "api_key_configured": bool(config.get_str("AI", "api_key", "")),
                # 「AI 对话」顶栏标签必须是**按当前配置与就绪状态实时算**的结果。
                # 早先直接透传 last_provider（=「上次实际解析结果」），用户改完设置后
                # 它不会自动更新 → 出现「设置页说『对话能力就绪』、顶栏说『尚未配置』」
                # 的自相矛盾，用户据此判断「没设置成功」。last_provider 仅留作诊断。
                "resolved": resolved_now,
                "last_provider": gw_state.last_provider if gw_state else "",
                "last_error": gw_state.last_error if gw_state else "",
                **readiness,
            },
            "sync": self.syncer.status() if self.syncer else {"running": False},
            "license": self.license or {},        # 离线授权状态（顶栏芯片 + 设置页卡片）
            # 需行动 → 顶部告警条。⚠ db.signature_mismatch 会经 db 通道单独在前端渲染，
            # 这里若原样再列一份，同一段文字会出现**两张一模一样的告警卡片**
            # （2026-09-30 用户实测「多了个提示」）。故按文本去重，单一事实源是 db。
            "warnings": [w for w in self.warnings
                         if w != getattr(self.db, "signature_mismatch", None)],
            "notes": list(self.notes),            # 已自愈 → 设置页运行详情
            "boot": self.boot_report,
        }

    port: int = 0


_ctx = AppContext()
_ctx_lock = threading.Lock()


def get_ctx() -> AppContext:
    return _ctx
