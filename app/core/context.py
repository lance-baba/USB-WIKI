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
from . import config, db as db_mod, embedder as embedder_mod, indexer, llm, net_util, paths, sync
from .log_util import get_logger

log = get_logger()


@dataclass
class AppContext:
    db: db_mod.Database | None = None
    embedder: embedder_mod.BaseEmbedder | None = None
    embedder_source: str = "none"
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

    # ------------------------------------------------------------------
    def boot(self, db_path=None, start_syncer: bool = True, probe_ollama: bool = True) -> dict:
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
            model_name = config.get_str("AI", "embedding_model_name", "bge-small-zh-q4")
            if self.embedder is not None:
                mismatch = self.db.check_signature(resolved.source, model_name, actual_dim)
                if mismatch:
                    self.warnings.append(mismatch + "，需点击「全量重建索引」")
                    self.db.signature_mismatch = mismatch
            else:
                # 兜底降级（无任何嵌入源）：绝不动已有向量索引，也不提示重建。
                # 否则会形成「Ollama 关→哈希签名覆盖→Ollama 开→又要重建」的破坏性循环。
                stored_dim = self.db.get_meta(db_mod.META_VEC_DIM)
                if stored_dim and stored_dim != str(self.db.embedding_dim):
                    self.db.signature_mismatch = (
                        f"向量嵌入暂不可用（本机缺少 onnxruntime，Ollama 也未在线），"
                        f"已退化为纯 FTS5 词法检索；原 {stored_dim} 维向量索引已保留，"
                        f"恢复嵌入源后自动恢复向量召回"
                    )

            # 7) 预解析一次 AI 提供方，让状态面板显示「真实可用」而非初始占位值。
            #    注意 resolve_provider 只返回结果，必须手动回写 last_provider。
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

    def ai_readiness(self) -> dict:
        """Level 3 对话能力就绪状态（**只读缓存**，不触发网络探测）。

        供 /api/status 与诊断复用；现场探测走 ``gateway.ai_readiness(probe=True)``
        或 ``/api/ollama/models``（设置页用）。
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
            ready = self.gateway.ai_readiness()
        except Exception as exc:  # noqa: BLE001 - 状态展示失败不得影响 /api/status
            log.debug("AI 就绪状态解析失败: %s", exc)
            return {
                "state": llm.STATE_NO_RUNTIME, "reason": "就绪状态解析失败",
                "chat_ready": False, "ollama_available": False, "ollama_probed": False,
                "installed_models": [], "installed_model_count": 0,
                "selected_model": None, "selected_model_installed": False,
                "cloud_api_configured": False,
            }
        return {
            k: ready[k] for k in (
                "state", "reason", "chat_ready", "ollama_available", "ollama_probed",
                "installed_models", "installed_model_count", "selected_model",
                "selected_model_installed", "cloud_api_configured",
            )
        }

    def status(self) -> dict:
        if self.db is None:
            return {"ready": False}
        stats = self.db.stats()
        sig = self.db.get_signature()
        gw_state = self.gateway.state if self.gateway else None
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
            },
            "ai": {
                "provider_mode": config.get_str("AI", "provider", "auto"),
                # ⚠ ollama_healthy 只表示「/api/tags 可访问」，不代表能对话；
                #   能否对话看 state / chat_ready（A4.1 契约）。
                "ollama_healthy": bool(gw_state and gw_state.ollama_healthy),
                "ollama_detail": gw_state.ollama_detail if gw_state else "",
                "api_key_configured": bool(config.get_str("AI", "api_key", "")),
                "resolved": gw_state.last_provider if gw_state else "",
                "last_error": gw_state.last_error if gw_state else "",
                **self.ai_readiness(),
            },
            "sync": self.syncer.status() if self.syncer else {"running": False},
            "warnings": list(self.warnings),      # 需行动 → 顶部告警条
            "notes": list(self.notes),            # 已自愈 → 设置页运行详情
            "boot": self.boot_report,
        }

    port: int = 0


_ctx = AppContext()
_ctx_lock = threading.Lock()


def get_ctx() -> AppContext:
    return _ctx
