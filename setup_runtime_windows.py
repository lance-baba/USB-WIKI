"""Wiki-USB Windows 嵌入式运行时安装器（PRD 4.1「Windows 嵌入式环境配置补齐」）。

做四件事：
  1. 下载官方 **python-3.11 embeddable** amd64 包并解压到 ``runtime/python-3.11-embed``
  2. 修补 ``python311._pth`` —— 解除 ``import site`` 注释并追加 ``./Lib/site-packages``
  3. 用内置解释器引导 pip，并把**全部运行依赖**装进 ``Lib/site-packages``
     —— 含 onnxruntime / tokenizers（V1 标准依赖，随 lock 精确锁定）

⚠ 模型字节**不在这里下载**。资源获取是 maintainer 侧的独立动作：

    python scripts/fetch_embedding_resource.py     # 按契约取件 + 校验 → vendor/cache/embedding/

这样「取资源」与「构建 Release」是两个动作，客户安装阶段 0 联网。

用法：
    python setup_runtime_windows.py                # 标准安装（需联网一次）
    python setup_runtime_windows.py --dev           # 开发安装：用 requirements.txt 浮动区间
    python setup_runtime_windows.py --check        # 体检现有运行时**能否跑起来**
    python setup_runtime_windows.py --verify       # 发布完整性校验：答「这个 U 盘能否交付」
    python setup_runtime_windows.py --verify --json # 同上，输出 JSON 供 CI 消费

依赖来源：
    发布构建默认读取 requirements-release.lock（== 精确锁定、可复现）；
    --dev 才用 requirements.txt（兼容区间）。lock 由
    scripts/lock_dependencies.py 从 requirements.txt 重新生成。

⚠ 本脚本可被宿主 Python 运行，但**只向 U 盘目录内写入**，不触碰注册表与系统环境。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUNTIME = BASE / "runtime"
EMBED_DIR = RUNTIME / "python-3.11-embed"
EMBED_PY = EMBED_DIR / "python.exe"
PTH_FILE = EMBED_DIR / "python311._pth"
SITE_PACKAGES = EMBED_DIR / "Lib" / "site-packages"

PY_VERSION = "3.11.9"
EMBED_URL = (
    f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
)
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
MIRROR_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
# ⚠ 这里**不再**有 ONNX 模型下载 URL。模型字节改由「资源契约 + maintainer 取件」提供：
#      resources/embedding/default.json（pin revision / sha256 / size）
#      scripts/fetch_embedding_resource.py → vendor/cache/embedding/（gitignored）
#   构建 Release 时只做本地拷贝；客户安装 0 联网。见 docs/EMBEDDING_ARTIFACT_SELECTION.md。

# 依赖清单：两份，职责不同（见 docs/DEPENDENCY_LOCK.md）
#   requirements.txt            —— 直接依赖 + 兼容区间，用于开发安装（浮动解析）
#   requirements-release.lock   —— 完整闭包 + == 精确锁定，用于发布构建 / CI（可复现）
# 两者必须保持一致：scripts/lock_dependencies.py 从 requirements.txt 生成 lock，
# tests/test_suite.py 的 test_dependency_consistency 校验直接依赖都已在 lock 中 == 锁定。
REQ_FILE: Path = BASE / "requirements.txt"
LOCK_FILE: Path = BASE / "requirements-release.lock"

# onnxruntime / tokenizers 已是 **V1 标准运行依赖**（写在 requirements.txt 里，
# 由 lock 精确锁定），随 runtime 一起安装 —— 不再有「可选 ONNX 步骤」。
# 体积影响实测见 docs/EMBEDDING_ARTIFACT_SELECTION.md。


def load_requirements(path: Path | None = None) -> list[str]:
    """解析依赖清单（按行，跳过空行 / 整行与行尾注释）。

    默认读 *path*；本脚本发布构建默认传 LOCK_FILE（精确锁定），
    ``--dev`` 时传 REQ_FILE（浮动区间）。
    """
    target = path or REQ_FILE
    if not target.exists():
        raise SystemExit(f"[setup] 缺少依赖清单：{target}")
    deps: list[str] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            deps.append(line)
    if not deps:
        raise SystemExit(f"[setup] {target} 中未解析到任何依赖，文件可能已损坏")
    return deps

PTH_CONTENT = """python311.zip
.
Lib/site-packages
import site
"""


def log(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


def _download(url: str, dest: Path, label: str = "") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)

    def hook(block: int, size: int, total: int) -> None:
        if total > 0:
            pct = min(100.0, block * size * 100.0 / total)
            sys.stdout.write(f"\r[setup] 下载 {label or dest.name} … {pct:5.1f}%")
            sys.stdout.flush()

    log(f"GET {url}")
    try:
        urllib.request.urlretrieve(url, dest, reporthook=hook)
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise SystemExit(
            f"\n[setup] 下载失败：{exc}\n"
            f"        请检查网络/代理，或手动下载后放到 {dest}"
        ) from exc
    sys.stdout.write("\n")
    return dest


def step1_download_embed() -> None:
    if EMBED_PY.exists():
        log(f"已存在嵌入式运行时，跳过下载：{EMBED_PY}")
        return
    zip_path = RUNTIME / "python-embed.zip"
    _download(EMBED_URL, zip_path, f"python-{PY_VERSION}-embed-amd64.zip")
    log("解压…")
    EMBED_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(EMBED_DIR)
    zip_path.unlink(missing_ok=True)
    if not EMBED_PY.exists():
        raise SystemExit(f"[setup] 解压后未找到 {EMBED_PY}")
    log(f"运行时就位：{EMBED_DIR}")


def step2_patch_pth() -> None:
    """解除 import site 屏蔽并追加相对依赖目录（PRD 明确要求）。"""
    if not EMBED_DIR.exists():
        raise SystemExit("[setup] 运行时目录不存在，请先执行下载步骤")
    existing = PTH_FILE.read_text(encoding="utf-8") if PTH_FILE.exists() else ""
    if "Lib/site-packages" in existing and "\nimport site" in "\n" + existing:
        log("python311._pth 已修补，跳过")
        return
    PTH_FILE.write_text(PTH_CONTENT, encoding="utf-8")
    log(f"已修补 {PTH_FILE.name}（启用 import site + 追加相对依赖目录）")
    log("  内容如下：")
    for line in PTH_CONTENT.strip().splitlines():
        log(f"    {line}")


def step3_install_deps(mirror: bool, dev: bool = False) -> None:
    SITE_PACKAGES.mkdir(parents=True, exist_ok=True)
    getpip = RUNTIME / "get-pip.py"
    if not (SITE_PACKAGES / "pip").exists():
        _download(GETPIP_URL, getpip, "get-pip.py")
        log("引导 pip…")
        subprocess.run(
            [str(EMBED_PY), str(getpip), "--no-warn-script-location"],
            check=True, cwd=str(EMBED_DIR),
        )
        getpip.unlink(missing_ok=True)

    # 发布构建默认用 **release lock**（== 精确锁定，可复现）；
    # 开发模式（--dev）才用 requirements.txt 的浮动区间。
    src = LOCK_FILE if (LOCK_FILE.exists() and not dev) else REQ_FILE
    deps = load_requirements(src)
    cmd = [str(EMBED_PY), "-m", "pip", "install", "--no-warn-script-location",
           "--disable-pip-version-check", *deps]
    if mirror:
        cmd += ["-i", MIRROR_INDEX]
    log(f"从 {src.name} 安装 {len(deps)} 项核心依赖（{'精确锁定' if src is LOCK_FILE else '浮动区间'}）："
        + " ".join(deps[:6]) + (" …" if len(deps) > 6 else ""))
    result = subprocess.run(cmd, cwd=str(EMBED_DIR))
    if result.returncode != 0:
        raise SystemExit("[setup] 依赖安装失败，请检查网络后重试")
    log("依赖安装完成")


def check_embedding_resource() -> tuple[bool, str]:
    """检查随包嵌入资源**字节**是否已就位（只读，不联网）。

    资源取回是 maintainer 侧的独立动作（scripts/fetch_embedding_resource.py）；
    这里只回答「构建 Release 时会不会因为缺件而失败」。
    """
    import json

    contract = BASE / "resources" / "embedding" / "default.json"
    cache = BASE / "vendor" / "cache" / "embedding"
    if not contract.is_file():
        return False, "缺少资源契约 resources/embedding/default.json"
    try:
        data = json.loads(contract.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, f"资源契约无法解析：{exc}"
    want = {
        "model.onnx": int(data.get("artifact_size") or 0),
    }
    for name, rec in (data.get("tokenizer_files") or {}).items():
        want[name] = int(rec.get("size") or 0)
    missing = [n for n, size in want.items()
               if not (cache / n).is_file() or (cache / n).stat().st_size != size]
    if missing:
        return False, (f"vendor/cache/embedding 缺件或尺寸不符：{', '.join(sorted(missing))}"
                       "（先跑 scripts/fetch_embedding_resource.py）")
    return True, f"{data.get('id')}（{len(want)} 个文件已就位）"


def check() -> int:
    ok = True
    print("=" * 66)
    print("  Wiki-USB 运行时体检")
    print("=" * 66)
    print(f"  安装根目录 : {BASE}")
    print(f"  嵌入式运行时: {'✅ 存在' if EMBED_PY.exists() else '❌ 缺失'}  {EMBED_PY}")
    if PTH_FILE.exists():
        txt = PTH_FILE.read_text(encoding="utf-8")
        site_ok = "import site" in txt and not txt.splitlines().count("#import site")
        sp_ok = "Lib/site-packages" in txt
        print(f"  _pth 补丁  : {'✅' if site_ok else '❌'} import site 已启用，"
              f"{'✅' if sp_ok else '❌'} Lib/site-packages 已追加")
        ok = ok and site_ok and sp_ok
    else:
        print("  _pth 补丁  : ❌ python311._pth 不存在")
        ok = False

    if EMBED_PY.exists():
        probe = (
            "import sqlite3, sys;"
            "print('  python     :', sys.version.split()[0]);"
            "print('  sqlite     :', sqlite3.sqlite_version);"
            "print('  fts5       :', any('FTS5' in r[0] for r in sqlite3.connect(':memory:').execute('pragma compile_options')));"
        )
        print(subprocess.run([str(EMBED_PY), "-c", probe], capture_output=True,
                             text=True).stdout, end="")
        for mod in ("sqlite_vec", "trafilatura", "lxml", "yaml",
                    "onnxruntime", "tokenizers"):
            r = subprocess.run([str(EMBED_PY), "-c", f"import {mod}"],
                               capture_output=True, text=True)
            print(f"  {mod:<11}: {'✅' if r.returncode == 0 else '❌ 未安装'}")
            ok = ok and r.returncode == 0

    res_ok, res_msg = check_embedding_resource()
    print(f"  嵌入资源    : {'✅ ' if res_ok else '⚠ '}{res_msg}")
    web = BASE / "app" / "web"
    print(f"  离线前端   : {'✅' if (web / 'index.html').exists() else '❌'} index.html"
          f" ，{'✅' if (web / 'app.css').exists() else '❌'} app.css"
          f" ，{'✅' if (web / 'app.js').exists() else '❌'} app.js")
    print("=" * 66)
    print("  结论：" + ("✅ 运行时可用于发布" if ok else "❌ 存在缺失项，请执行完整安装"))
    return 0 if ok else 1


# =====================================================================
# 发布完整性校验：回答「这个 U 盘内容能不能直接交付给用户」
#
# 与 check() 的区别 —— 两者职责不重叠，别合并：
#   check()  体检 **运行时能不能跑起来**（python.exe / _pth / 依赖包）
#   verify() 校验 **交付完整性**（文件齐不齐 / 资产在不在 / 是否偷偷依赖外网）
# Portable 发布的实际门槛是后者：跑得起来 ≠ 可以交付。
# =====================================================================

# ⚠ config.ini 不在硬清单里：它是**首次运行由程序生成的运行时文件**
# （含明文密钥，已被 .gitignore），任何发布包 / 干净 checkout 都不该有它，
# 视为必交付项会False-positive「不可交付」。它是否存在由运行期自检负责。
VERIFY_ROOT_FILES = [
    "README.md", "LICENSE",
    "requirements.txt", "requirements-release.lock",
    "setup_runtime_windows.py",
]
VERIFY_LAUNCHERS = ["启动-Windows.bat", "启动-macOS.command", "启动-Linux.sh"]
# ⚠ data/notes、data/originals 是**用户真相源**，由运行期按需创建，
# 不随发布包分发，也不该出现在干净 checkout 里 —— 不算交付缺项。
# data 根目录保留（若已随仓库提交则校验其存在；运行期也会自建）。
VERIFY_DIRS = [
    "app", "app/core", "app/api", "app/web",
    "data",
    "docs", "tests",
]
VERIFY_APP_PY = ["app/launcher.py", "app/server.py", "app/version.py"]
# server.py 模块化后的七个域模块（app/api），缺一个即某组 API 全挂
VERIFY_API_MODULES = [
    f"app/api/{m}.py"
    for m in ("system", "config", "diagnostics", "search", "ask", "library", "capture")
]
# 前端拆分产物（index.html 壳 + app.css + app.js），三者缺一不可
VERIFY_WEB_ASSETS = ["app/web/index.html", "app/web/app.css", "app/web/app.js"]

# 前端资产里**不允许**出现的外部引用 —— 零 CDN 是 Portable 的立身之本。
# 只匹配「加载语义」的外链（src=/href= 与 CSS url()），
# 不误伤 JS 字符串里出现的普通 https:// 文本（那不是资源引用）。
_EXTERNAL_URL_RE = re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", re.I)
_CSS_IMPORT_URL_RE = re.compile(r"""url\(\s*["']?\s*(?:https?:)?//""", re.I)
_WEB_SCAN_SUFFIX = {".html", ".css", ".js"}
# 遍历源目录时跳过的子树：runtime 体量大且为官方发行包自带，
# data 是用户真相源只可能含用户自己的文件。
_JUNK_SKIP_TREES = {"runtime", "data", ".git", ".venv", "node_modules"}

OK = "✅"
BAD = "❌"
WARN = "⚠"


def _exists(rel: str) -> bool:
    return (BASE / rel).exists()


def _scan_external_refs() -> list[str]:
    """前端资产里残留的外部资源引用 —— 有任一处即违反「完全离线」。"""
    hits: list[str] = []
    web = BASE / "app" / "web"
    if not web.exists():
        return hits
    for f in sorted(web.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in _WEB_SCAN_SUFFIX:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _EXTERNAL_URL_RE.search(line) or _CSS_IMPORT_URL_RE.search(line):
                hits.append(f"{f.relative_to(web)}:{lineno}")
    return hits


def _scan_build_junk() -> list[str]:
    """工程垃圾：__pycache__ 目录与 .pyc/.pyo/.DS_Store/Thumbs.db。

    只报告不删除 —— 清理是 ``--clean`` 的职责，这里避免职责重叠。
    """
    junk: list[str] = []
    for root, dirs, files in os.walk(BASE):
        rel = Path(root).relative_to(BASE)
        parts = rel.parts
        if parts and parts[0] in _JUNK_SKIP_TREES:
            dirs[:] = []
            continue
        for d in list(dirs):
            if d == "__pycache__":
                junk.append(str(rel / d) if str(rel) != "." else d)
                dirs.remove(d)                      # prune，不递归进去
        for fn in files:
            hit = (fn.endswith((".pyc", ".pyo"))
                   or fn in (".DS_Store", "Thumbs.db")
                   # 一次性调试脚本的约定前缀（scripts/_tmp_*.py），属工程垃圾
                   or (fn.startswith("_tmp_") and fn.endswith(".py")))
            if hit:
                junk.append(str(rel / fn) if str(rel) != "." else fn)
    return junk


def _path_size(p: Path) -> int:
    """文件或目录树的总字节数，单文件失败不影响整体统计。"""
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for fn in files:
            try:
                total += (Path(root) / fn).stat().st_size
            except OSError:
                pass
    return total


def clean(*, dry_run: bool = False) -> int:
    """清除工程垃圾，让 U 盘发布包不带构建残留。

    安全边界：只动**项目自身源码树**（已跳过 runtime/ 与 data/）——
      * data/ 是用户真相源，误删等于删掉用户的知识库
      * runtime/ 是官方发行包，其中第三方包的散落 .pyc 可能为其分发所需，
        收益不明确而风险存在，故不碰
    被清除的都是 **Python 会自动重建** 的编译缓存与编辑器/调试残留。
    """
    junk = _scan_build_junk()
    if not junk:
        print("  ✅ 无工程垃圾，无需清理")
        return 0

    dirs = sorted({j for j in junk if (BASE / j).is_dir()}, reverse=True)
    files = sorted(j for j in junk if (BASE / j).is_file())
    freed = sum(_path_size(BASE / j) for j in dirs + files)

    verb = "将清除" if dry_run else "已清除"
    print("=" * 66)
    print(f"  Wiki-USB 发布清理{'（预览，不做任何改动）' if dry_run else ''}")
    print("=" * 66)
    print(f"  {verb}：目录 {len(dirs)} 个 / 文件 {len(files)} 个"
          f" ，释放约 {freed / 1048576:.1f} MB")
    for d in dirs[:12]:
        print(f"    ├── {d}/")
    if len(dirs) > 12:
        print(f"    └── … 其余 {len(dirs) - 12} 个目录")
    for fn in files[:12]:
        print(f"    ├── {fn}")
    if len(files) > 12:
        print(f"    └── … 其余 {len(files) - 12} 个文件")

    if dry_run:
        print("-" * 66)
        print("  预览结束，未做任何改动。确认无误后去掉 --dry-run 执行清理。")
        print("=" * 66)
        return 0

    removed_d = removed_f = 0
    for d in dirs:
        p = BASE / d
        # 最后一道保险：只认 __pycache__ 这一个确切目录名
        if p.name != "__pycache__" or not p.is_dir():
            continue
        try:
            shutil.rmtree(p, ignore_errors=True)
            removed_d += 1
        except OSError as exc:
            print(f"    ! 删除失败 {d}: {exc}")
    for fn in files:
        try:
            (BASE / fn).unlink(missing_ok=True)
            removed_f += 1
        except OSError as exc:
            print(f"    ! 删除失败 {fn}: {exc}")

    print("-" * 66)
    print(f"  实际清除：目录 {removed_d} / 文件 {removed_f}"
          f" ，释放约 {freed / 1048576:.1f} MB")
    print("  提示：这些均为编译缓存，下次运行会由 Python 自动重建。")
    print("=" * 66)
    return 0


def verify(*, as_json: bool = False) -> int:
    """发布完整性校验：文件齐不齐、资产在不在、有没有偷偷依赖外网。

    *as_json* 为真时输出结构化结果供 CI 消费；否则输出人类可读报告。
    退出码一致：0 = 可交付，1 = 存在阻断项（缺文件或外链）。
    """
    missing: list[str] = []
    warns: list[str] = []
    rows: list[tuple[str, str, str]] = []

    def row(title: str, status: str, detail: str) -> None:
        rows.append((title, status, detail))
        if not as_json:
            print(f"  {title:<10}: {status} {detail}")

    def group(title: str, rels: list[str]) -> None:
        miss = [r for r in rels if not _exists(r)]
        if miss:
            missing.extend(miss)
            shown = ", ".join(miss[:3]) + ("…" if len(miss) > 3 else "")
            row(title, BAD, f"缺 {len(miss)} 项 -> {shown}")
        else:
            row(title, OK, f"{len(rels)} 项齐全")

    if not as_json:
        print("=" * 66)
        print("  Wiki-USB 发布完整性校验")
        print("=" * 66)
        print(f"  发布根目录 : {BASE}")
        print(f"  校验平台   : {platform.system()} ({os.name})")
        print("-" * 66)

    group("根文件", VERIFY_ROOT_FILES)
    group("启动脚本", VERIFY_LAUNCHERS)
    group("目录骨架", VERIFY_DIRS)
    group("应用核心", VERIFY_APP_PY)
    group("API 模块", VERIFY_API_MODULES)
    group("前端资产", VERIFY_WEB_ASSETS)

    # data/cache.db 是**可重建的衍生索引**（MD 才是真相源），缺失只警告不阻断
    if not _exists("data/cache.db"):
        warns.append("data/cache.db 未生成 —— 首次启动会自动重建，不影响发布")
        row("索引缓存", WARN, "未生成（首次启动自动重建，非阻断）")
    else:
        row("索引缓存", OK, "已存在")

    docs = sorted((BASE / "docs").glob("*.md")) if _exists("docs") else []
    if len(docs) < 4:
        warns.append(f"docs/ 下 .md 仅 {len(docs)} 篇，文档四件套可能不全")
        row("文档", WARN, f"仅 {len(docs)} 篇")
    else:
        row("文档", OK, f"{len(docs)} 篇")

    # 嵌入式运行时：Windows 发布必需；macOS / Linux 走宿主 Python（PRD 设计如此）
    if os.name == "nt":
        rt_items = ("python.exe", "python311._pth", "Lib/site-packages")
        rt_miss = [p for p in rt_items if not (EMBED_DIR / p).exists()]
        if rt_miss:
            missing.extend(f"runtime/python-3.11-embed/{p}" for p in rt_miss)
            row("运行时", BAD, f"缺 {', '.join(rt_miss)}，请先执行安装")
        else:
            row("运行时", OK, "python.exe + _pth + site-packages")
    elif EMBED_PY.exists():
        row("运行时", OK, "存在")
    else:
        row("运行时", WARN, "未打包（非 Windows 由宿主 Python 运行，正常）")

    # 离线自包含：Portable 的底线 —— U 盘拔出网络也必须完整可用
    ext = _scan_external_refs()
    if ext:
        missing.extend(f"外链引用 {h}" for h in ext)
        row("离线自包含", BAD, f"前端残留 {len(ext)} 处外链 -> {', '.join(ext[:3])}")
    else:
        row("离线自包含", OK, "前端无 CDN / 外部资源引用")

    junk = _scan_build_junk()
    if junk:
        warns.append(f"{len(junk)} 项工程垃圾未清理")
        row("发布清洁度", WARN, f"{len(junk)} 项垃圾（__pycache__/.pyc），建议 --clean")
    else:
        row("发布清洁度", OK, "无构建残留")

    deliverable = not missing

    if as_json:
        print(json.dumps({
            "deliverable": deliverable,
            "root": str(BASE),
            "platform": platform.system(),
            "missing": missing,
            "warnings": warns,
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in rows],
        }, ensure_ascii=False, indent=2))
        return 0 if deliverable else 1

    print("=" * 66)
    if missing:
        print(f"  结论：{BAD} 不可交付 —— 缺失 {len(missing)} 项")
        for m in missing[:10]:
            print(f"        - {m}")
        if len(missing) > 10:
            print(f"        … 其余 {len(missing) - 10} 项")
    else:
        print(f"  结论：{OK} 可直接交付")
    for w in warns:
        print(f"  提示：{WARN} {w}")
    print("=" * 66)
    return 0 if deliverable else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Wiki-USB Windows 嵌入式运行时安装器")
    ap.add_argument("--check", action="store_true", help="仅体检现有运行时能否跑起来")
    ap.add_argument("--verify", action="store_true",
                    help="发布完整性校验：文件/资产/启动脚本齐不齐、是否残留外链（答「能否交付」）")
    ap.add_argument("--json", action="store_true",
                    help="以 JSON 输出校验结果（配合 --verify，便于 CI 消费）")
    ap.add_argument("--clean", action="store_true",
                    help="清理工程垃圾（__pycache__/.pyc/编辑器残留），不动 runtime/ 与 data/")
    ap.add_argument("--dry-run", action="store_true",
                    help="配合 --clean：只预览将被清除的内容，不实际删除")
    # ⚠ `--with-onnx` 已移除：onnxruntime / tokenizers 是 V1 标准运行依赖，随 lock 安装；
    #   模型字节由 scripts/fetch_embedding_resource.py 单独取回（maintainer 侧动作）。
    ap.add_argument("--no-mirror", action="store_true", help="不使用清华 PyPI 镜像")
    ap.add_argument("--dev", action="store_true",
                   help="开发安装：用 requirements.txt 的浮动区间而非 release lock")
    ap.add_argument("--force", action="store_true", help="重装依赖")
    args = ap.parse_args()

    if args.check:
        return check()

    if args.verify:
        return verify(as_json=args.json)

    if args.clean:
        return clean(dry_run=args.dry_run)

    if os.name != "nt":
        print("[setup] 提示：本脚本用于生成 Windows 发布版。")
        print("        macOS / Linux 无需嵌入式包 —— 直接运行 启动-macOS.command / 启动-Linux.sh，")
        print("        启动脚本会自动检测宿主 Python 3.10+ 并提示安装缺失依赖。")

    if sys.version_info[:2] < (3, 9):
        raise SystemExit("[setup] 宿主 Python 版本过低，请使用 3.9+ 运行本脚本")

    log(f"安装根目录：{BASE}")
    step1_download_embed()
    step2_patch_pth()
    if args.force and SITE_PACKAGES.exists():
        shutil.rmtree(SITE_PACKAGES, ignore_errors=True)
        log("已清空旧的 site-packages（--force）")
    step3_install_deps(mirror=not args.no_mirror, dev=args.dev)
    # 模型字节不在这里下载（那是 maintainer 的取件动作，见 scripts/fetch_embedding_resource.py）；
    # 这里只提示「构建 Release 前要不要先取件」，不在安装路径上联网。
    res_ok, res_msg = check_embedding_resource()
    log(("嵌入资源：" + res_msg) if res_ok
        else ("⚠ " + res_msg + "（不加也能跑，但 build_release.py --strict 会失败）"))

    print()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
