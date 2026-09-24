"""config.ini 读写与校验。

对外提供线程安全的 getter，以及首次运行自动生成默认模板的能力（PRD 5.2）。
"""
from __future__ import annotations

import configparser
import io
import threading
from pathlib import Path
from typing import Any

from . import atomic_io, paths, redact
from .log_util import get_logger

log = get_logger()

DEFAULT_TEMPLATE = """[SYSTEM]
# 绑定主机与端口 (端口被占用时自动 +1 递增探测, 最多 10 次)
host = 127.0.0.1
port = 28765
# 外部文件扫描轮询间隔 (秒)
sync_interval = 15
# 日志级别: DEBUG / INFO / WARNING / ERROR
log_level = INFO

[AI]
# 注入给模型的「知识片段」预算。父块是章节粒度，实测单块可达 1200 字符；
# 不封顶时命中 5 块就是 5000+ 字符（≈7.5k tokens），小模型要么溢出、
# 要么被无关长文淹没。每段会优先截取**与查询词最接近的窗口**而非从头截。
inject_per_parent_chars = 400
inject_total_chars = 1800
# 模式选择: auto(自动探测) / ollama(本地优先) / api(云端优先) / offline(纯离线免AI)
provider = auto

# --- 通用 OpenAI 兼容接口配置 (支持 DeepSeek / 阿里百炼 / Kimi 等) ---
# 注意: 本文件存放于移动存储介质, 请勿在公共借出设备中留存高余额 API Key!
api_base_url = https://api.deepseek.com/v1
api_key =
api_chat_model = deepseek-chat

# --- 本地 Ollama 守护进程配置 ---
ollama_host = http://127.0.0.1:11434
# ⚠ 本地对话模型**故意留空**：产品尚未推荐任何模型，Core 不得替用户预设。
#   留空时程序会报告 selection_required，由用户在「设置 → AI → 本地对话模型」
#   里从**本机已安装**的模型中选择；选之前不会自动挑任何一个（也不猜 models[0]）。
#   升级用户原有的值会被原样保留，不会静默改写。
ollama_chat_model =

# --- 向量模型配置 ---
# local_onnx(内置微型 bge-small-zh) / ollama(通过 ollama 计算) / api(通过 API 计算)
embedding_source = local_onnx
embedding_dim = 512
embedding_model_name = bge-small-zh-q4
top_k_parents = 5
recall_candidates = 20

[ANALYZE]
# 入库时是否用本地模型生成一句话摘要（增强层）。
# 确定性层（关键词 / 实体 / 语言）不受此开关影响，永远执行。
# 设为 1 时：模型不在线会自动跳过，不影响入库速度与成功率。
ai_summary = 1

[SEARCH]
# 是否允许「仅语义相关（没有字面命中）」的内容进入引用来源。
# 0 = 不允许（默认）：引用必须有词法依据，宁可回答「没找到」，
#     也不用语义相近但无关的文档冒充出处 —— 这是实测踩过的坑：
#     库里没有「杜苏芮」，它却因向量最近邻被当成台风问答的来源引用。
# 1 = 允许：召回更多，但引用可能不相关，自行权衡。
allow_semantic_only = 0

[IMPORT]
# 文件摄入边界。默认值保守，同时保证正常 DOCX/PPTX/XLSX/EPUB 能打开。
# 普通文件（PDF/CSV/JSON/Markdown/HTML…）单文件上限 —— 攻击者不需要 ZIP，
# 直接扔超大文本也能打爆处理流程。
max_file_mb = 50
# 压缩容器（OOXML / EPUB）限制。两层防护：
# 1) 中央目录预检查这些声明值；2) 读取时再按实际输出字节限流。
max_archive_entries = 5000
max_archive_entry_mb = 50
max_archive_total_mb = 200
max_compression_ratio = 100

[CRAWLER]
# 是否允许抓取**非公网**地址（局域网 / 内网 / 本机）。
# 默认 0：只允许解析结果为公网地址的目标，且逐跳校验重定向（防 SSRF）。
# 置 1 仅表示「你明确允许抓局域网资源」——
# 协议白名单（只 http/https）、URL 合法性、重定向跳数与环检测**一律不变**。
allow_private_network = 0
# 正文提取字数降级门限
min_body_chars = 150
# 是否允许调用本机 Headless Chrome 处理动态页 (0: 禁用, 1: 启用)
enable_headless_chrome = 0
# HTTP 请求超时 (秒)
request_timeout = 20
# 降级快照保留字符数
snapshot_chars = 1000
# 剪藏网页时是否把 CSS/图片/字体等子资源一起抓下来存入本地资源池 (0: 关闭)
# 开启后「原版预览」在不联网时也能还原版式，且浏览时不会向外部站点发任何请求；
# 关闭则退化为「结构存档」：只留 HTML，样式与图片需联网才能显示。
# ⚠ 注意：默认不下载脚本 —— 预览用沙箱禁用了脚本，存下来没有意义还增大体积与风险。
save_assets = 1
# 单个网页的资源预算 (KB)。超出后剩余资源改用占位，避免个别页面吃掉整块 U 盘。
assets_budget_kb = 5120
# 单个资源的大小上限 (KB)。超过则跳过并留占位（保留原始尺寸，避免撑破布局）。
asset_max_kb = 512
"""

_lock = threading.RLock()
_parser: configparser.ConfigParser | None = None
_path: Path = paths.CONFIG_FILE


def _new_parser() -> configparser.ConfigParser:
    p = configparser.ConfigParser(interpolation=None)
    # 保留 key 大小写（api_key / API_KEY 语义一致，但避免意外归一化）
    p.optionxform = str  # type: ignore[assignment]
    return p


def load(path: Path | None = None, force: bool = False) -> configparser.ConfigParser:
    """加载（并在缺失时生成）配置文件。"""
    global _parser, _path
    with _lock:
        if path is not None:
            _path = path
            _parser = None
        if _parser is not None and not force:
            return _parser

        if not _path.exists():
            try:
                _path.parent.mkdir(parents=True, exist_ok=True)
                atomic_io.atomic_write_text(_path, DEFAULT_TEMPLATE)
                log.info("已生成默认配置模板: %s", _path)
            except OSError as exc:
                log.warning("默认配置写入失败(将以内存模板运行): %s", exc)

        parser = _new_parser()
        try:
            parser.read(_path, encoding="utf-8")
        except (OSError, configparser.Error) as exc:
            log.error("配置读取失败, 回退内存默认值: %s", exc)
        # 补齐缺失的 section/key（用户手工删减配置时不至于 KeyError）
        fallback = _new_parser()
        fallback.read_string(DEFAULT_TEMPLATE)
        for section in fallback.sections():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in fallback.items(section):
                if not parser.has_option(section, key):
                    parser.set(section, key, value)

        # [GRAPH]（星图）功能已移除 → 清掉历史 config.ini 里残留的该段：
        # 否则设置页会把它当「未知键」以英文原始键兜底显示。下次保存即从文件消失。
        if parser.has_section("GRAPH"):
            parser.remove_section("GRAPH")

        _parser = parser
        return _parser


def get(section: str, key: str, default: Any = None) -> Any:
    p = load()
    try:
        if p.has_option(section, key):
            return p.get(section, key)
    except (configparser.Error, Exception):  # noqa: BLE001 - 配置永不阻断启动
        pass
    if default is not None:
        return default
    fallback = _new_parser()
    fallback.read_string(DEFAULT_TEMPLATE)
    try:
        return fallback.get(section, key)
    except Exception:  # noqa: BLE001
        return default


def get_int(section: str, key: str, default: int = 0) -> int:
    raw = get(section, key, default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def get_float(section: str, key: str, default: float = 0.0) -> float:
    raw = get(section, key, default)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def get_bool(section: str, key: str, default: bool = False) -> bool:
    raw = str(get(section, key, default)).strip().lower()
    return raw in ("1", "true", "yes", "on", "y")


def get_str(section: str, key: str, default: str = "") -> str:
    raw = get(section, key, default)
    return "" if raw is None else str(raw).strip()


def as_dict() -> dict[str, dict[str, str]]:
    p = load()
    return {s: dict(p.items(s)) for s in p.sections()}


def defaults() -> dict[str, dict[str, str]]:
    """内置默认配置（解析自 ``DEFAULT_TEMPLATE``）—— 供设置页「恢复默认」使用。

    只含出厂默认值，不含任何用户填写内容（``api_key`` 默认为空串）。
    """
    p = _new_parser()
    p.read_string(DEFAULT_TEMPLATE)
    return {s: dict(p.items(s)) for s in p.sections()}


def update(values: dict[str, dict[str, Any]], persist: bool = True) -> dict[str, dict[str, str]]:
    """批量更新配置；persist=True 时回写 config.ini。"""
    global _parser
    with _lock:
        p = load()
        for section, items in (values or {}).items():
            if not p.has_section(section):
                p.add_section(section)
            for key, value in items.items():
                # ⚠ 脱敏占位值必须**跳过**：前端读取设置时拿到的是 `sk-****abcd`，
                # 若用户只改了别的字段再保存，把这个占位写回去就会把真 key
                # 覆盖成一串星号。**未修改 ≠ 要写回脱敏值。**
                if redact.is_masked(value):
                    continue
                # 空串 = 明确清空（用户主动删掉 key）；此处不做「空即未改」的猜测
                p.set(section, str(key), "" if value is None else str(value))
        if persist:
            try:
                # config.ini 含 API Key，写坏即永久丢失且无法重建 → 原子写。
                # 先写进内存缓冲再整块落盘：替换前目标始终是完整旧内容。
                buf = io.StringIO()
                p.write(buf)
                atomic_io.atomic_write_text(_path, buf.getvalue())
            except OSError as exc:
                log.error("配置回写失败: %s", exc)
        return {s: dict(p.items(s)) for s in p.sections()}


def reload() -> configparser.ConfigParser:
    return load(force=True)
