#!/usr/bin/env python3
"""Release Builder —— 把源码仓库组装成发布产物 dist/USB-WIKI-vX.Y.Z-win-x64/。

产物是**构建产物**，不进 Git（dist/ 已在 .gitignore）。

布局：
    dist/USB-WIKI-v1.3.0-win-x64/
    ├─ installer/
    │  ├─ install.py            (SSD 安装骨架，拷贝自 scripts/install_windows.py)
    │  ├─ install.bat           (Windows：用自带嵌入式 Python 跑 install.py)
    │  └─ release_integrity.py  (介质校验唯一实现，随安装器同目录分发)
    ├─ payload/
    │  ├─ app/                  (拷贝自仓库 app/)
    │  └─ python-runtime/       (拷贝自仓库 runtime/python-3.11-embed/)
    ├─ LICENSES/                (第三方分发许可清单，collect_licenses.py 生成)
    ├─ BUILD_INFO.json          (版本可追溯性)
    ├─ RELEASE_MANIFEST.json    (机器可读清单：相对路径 / 固定排序 / 含 sha256)
    └─ SHA256SUMS               (传统校验和，由清单派生)

约定（V1 Freeze / A3 范围）：
- 不联网、不 pip、不下载任何 >100MB 资源。
- 版本号唯一来源：app.version.APP_VERSION（本文件不写版本字面量）。
- SHA256 定位是 **Integrity**（介质损坏检测），不是 Authenticity —— 不签名、不加密。
- BUILD_INFO 绝不含 用户名 / 绝对路径 / HOME / LOCALAPPDATA / IP / 机器标识 / key。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # scripts/
REPO = HERE.parent                                 # 仓库根

#: 随发布交付的文档白名单（**显式**列出：内部过程文档 / 已知问题备忘不进客户包）
RELEASE_DOC_FILES = (
    "START_HERE.txt",   # Digital Pilot：面向普通用户的唯一入口文档（其余为随包开发文档）
    "README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md",
    "CODE_OF_CONDUCT.md", "CONTRIBUTING.md",
)
RELEASE_DOCS_DIR_FILES = (
    "DATA_CONTRACT_V1.md", "DEPENDENCY_LOCK.md", "设计与实现.md",
    "项目架构说明.md", "测试报告.md", "V1.3工程收口基线.md",
)


def _ensure_repo_on_path() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))


def _copytree_prune(src: Path, dst: Path) -> None:
    """拷贝目录，剔除 __pycache__ / *.pyc（发布物不该带解释器缓存）。"""
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        dirs_exist_ok=True,
    )


def _fail_build(msg: str) -> "None":
    print(f"[build] 错误：{msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 各构建阶段
# ---------------------------------------------------------------------------
def _stage_payload(dist_root: Path) -> bool:
    """app/ + python-runtime/。返回 runtime 是否就位。"""
    payload = dist_root / "payload"
    if not (REPO / "app").is_dir():
        _fail_build(f"仓库 app/ 不存在，无法构建 payload：{REPO / 'app'}")
    _copytree_prune(REPO / "app", payload / "app")

    rt_src = REPO / "runtime" / "python-3.11-embed"
    rt_dst = payload / "python-runtime"
    if (rt_src / "python.exe").is_file():
        _copytree_prune(rt_src, rt_dst)
        return True
    return False


#: 嵌入资源：取件（maintainer）与构建（builder）是两个动作，构建**绝不联网**
EMBEDDING_CONTRACT = REPO / "resources" / "embedding" / "default.json"
EMBEDDING_CACHE = REPO / "vendor" / "cache" / "embedding"

#: stable 判定码（与 strict Gate 的错误码一一对应）
EMB_OK = "ok"
EMB_ARTIFACT_MISSING = "EMBEDDING_ARTIFACT_MISSING"
EMB_METADATA_INVALID = "EMBEDDING_METADATA_INVALID"
EMB_HASH_MISMATCH = "EMBEDDING_HASH_MISMATCH"
EMB_TOKENIZER_MISSING = "EMBEDDING_TOKENIZER_MISSING"
EMB_LICENSE_MISSING = "EMBEDDING_LICENSE_MISSING"


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _stage_embedding(dist_root: Path) -> dict:
    """把随包嵌入资源拷进 ``payload/embedding/``（**只做本地拷贝**）。

    返回 ``{staged, code, reason, id, build_info}``。
    构建期做一次**全量 SHA256**：构建不是每次启动，24MB 的一次性校验值得付；
    运行时则刻意不重算（见 app/core/embedder.py 的 load_embedding_resource）。
    """
    report: dict = {"staged": False, "code": EMB_OK, "reason": "",
                    "id": None, "build_info": None, "applicable": True}

    if not EMBEDDING_CONTRACT.is_file():
        # 仓库根本未声明嵌入能力（微型/玩具仓库、纯词法形态）：跳过，不强制随包。
        report.update(code=EMB_METADATA_INVALID,
                      reason=f"缺少资源契约 {EMBEDDING_CONTRACT.name}",
                      applicable=False)
        return report
    try:
        contract = json.loads(EMBEDDING_CONTRACT.read_text(encoding="utf-8"))
    except ValueError as exc:
        report.update(code=EMB_METADATA_INVALID, reason=f"资源契约无法解析：{exc}")
        return report
    if contract.get("format_version") != 1:
        report.update(code=EMB_METADATA_INVALID, reason="资源契约 format_version 不受支持")
        return report

    local = contract.get("local_files") or {}
    model_name = Path(str(local.get("model") or "model.onnx")).name
    tok_name = Path(str(local.get("tokenizer") or "tokenizer.json")).name
    wanted: dict[str, str] = {model_name: str(contract.get("artifact_sha256") or "")}
    for name, rec in (contract.get("tokenizer_files") or {}).items():
        wanted[name] = str(rec.get("sha256") or "")

    missing = [n for n in wanted if not (EMBEDDING_CACHE / n).is_file()]
    if missing:
        report.update(code=EMB_ARTIFACT_MISSING,
                      reason=(f"vendor/cache/embedding 缺件：{', '.join(sorted(missing))}"
                              "（先跑 scripts/fetch_embedding_resource.py）"))
        return report
    if tok_name not in wanted or not (EMBEDDING_CACHE / tok_name).is_file():
        report.update(code=EMB_TOKENIZER_MISSING, reason=f"缺少 tokenizer 文件 {tok_name}")
        return report

    dest = dist_root / "payload" / "embedding"
    dest.mkdir(parents=True, exist_ok=True)
    for name in sorted(wanted):
        src = EMBEDDING_CACHE / name
        actual = _sha256_file(src)
        if actual != wanted[name].lower():
            report.update(code=EMB_HASH_MISMATCH,
                          reason=f"{name}：SHA256 与契约不符（期望 {wanted[name][:16]}…，"
                                 f"实际 {actual[:16]}…）")
            return report
        shutil.copy2(src, dest / name)
    # 契约本体（运行时按 artifact.json 解析，不猜文件名）
    shutil.copy2(EMBEDDING_CONTRACT, dest / "artifact.json")

    report.update(
        staged=True, id=contract.get("id"),
        build_info={
            "id": contract.get("id"),
            "base_model": contract.get("base_model"),
            "artifact_source": contract.get("artifact_source"),
            "artifact_revision": contract.get("artifact_revision"),
            "precision": contract.get("precision"),
            "dimension": contract.get("dimension"),
            "artifact_sha256": str(contract.get("artifact_sha256") or "").lower(),
            "tokenizer_sha256": str(((contract.get("tokenizer_files") or {}).get(tok_name) or {})
                                    .get("sha256") or "").lower(),
        },
    )
    return report


def _installer_bat(subcmd: str) -> str:
    """生成 installer/*.bat 的内容（**纯 ASCII**）。

    硬性约束（否则 cp936 宿主机上 .bat 内嵌中文会乱码）：
      · 本体只含 ASCII，中文提示一律由 Python 打印；
      · **不使用括号块** —— 括号内 `%VAR%` 在解析期展开会拿到过期值，用 label/goto 扁平结构。
    关键 UX（2026-09-20 修）：双击 .bat 时 Windows 给了它一个新控制台，**进程一退出窗口即关闭**；
    此前没有 `pause`，导致用户「输入路径回车后窗口就没了」，成功/报错全都看不见。
    现在无论成功失败都 `pause` 等待按键，用户能看到完整输出。
    """
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if not exist \"..\\payload\\python-runtime\\python.exe\" goto media_missing\r\n"
        f"..\\payload\\python-runtime\\python.exe install.py {subcmd} %*\r\n"
        "set RC=%ERRORLEVEL%\r\n"
        "if not \"%RC%\"==\"0\" goto failed\r\n"
        "echo.\r\n"
        "echo [OK] Done. Return code 0.\r\n"
        "goto hold\r\n"
        ":failed\r\n"
        "echo.\r\n"
        "echo [ERROR] Failed with exit code %RC%. Please read the message above.\r\n"
        "goto hold\r\n"
        ":hold\r\n"
        "echo.\r\n"
        "echo Press any key to close this window . . .\r\n"
        "pause >nul\r\n"
        "exit /b %RC%\r\n"
        ":media_missing\r\n"
        "echo MEDIA_CORRUPTED: payload\\python-runtime\\python.exe missing\r\n"
        "echo Press any key to close this window . . .\r\n"
        "pause >nul\r\n"
        "exit /b 5\r\n"
    )


def _stage_installer(dist_root: Path) -> None:
    installer_dir = dist_root / "installer"
    installer_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "install_windows.py", installer_dir / "install.py")
    # 介质校验唯一实现：安装器同目录副本，避免「构建/校验」两套枚举漂移
    shutil.copy2(HERE / "release_integrity.py", installer_dir / "release_integrity.py")
    # ⚠ 两个 .bat 都必须带上**子命令**：install.py 是 subparsers 结构，
    #   `install.py --app-target X` 会被 argparse 当成「X 是子命令」而报 invalid choice
    #   （旧版 install.bat 写的是 `install.py %*`，缺 `install` 子命令 → 带参数的 CLI 用法一直是坏的）。
    (installer_dir / "install.bat").write_text(_installer_bat("install"), encoding="ascii")
    (installer_dir / "uninstall.bat").write_text(_installer_bat("uninstall"), encoding="ascii")
    if not (installer_dir / "install.py").is_file():
        _fail_build("installer/install.py 生成失败")
    if not (installer_dir / "release_integrity.py").is_file():
        _fail_build("installer/release_integrity.py 生成失败")


def _stage_docs(dist_root: Path) -> int:
    """随包文档（白名单）—— MANIFEST 需能覆盖到「docs」。"""
    count = 0
    for name in RELEASE_DOC_FILES:
        src = REPO / name
        if src.is_file():
            shutil.copy2(src, dist_root / name)
            count += 1
    docs_dst = dist_root / "docs"
    for name in RELEASE_DOCS_DIR_FILES:
        src = REPO / "docs" / name
        if src.is_file():
            docs_dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, docs_dst / name)
            count += 1
    return count


def _version_sources() -> dict:
    """schema / data format 的**唯一来源**，一律 import 现取，不写字面量。"""
    from app.core.migrations import CURRENT_SCHEMA_VERSION      # noqa: PLC0415
    from app.core.library import DATA_FORMAT_VERSION            # noqa: PLC0415
    return {"schema_version": CURRENT_SCHEMA_VERSION,
            "data_format_version": DATA_FORMAT_VERSION}


def _build_info(root: Path, platform: str, rt_ok: bool,
                embedding: dict | None = None) -> dict:
    import release_integrity as ri                              # noqa: PLC0415

    lock = REPO / "requirements-release.lock"
    lock_sha = ri.sha256_file(lock) if lock.is_file() else None
    py_ver = ri.resolve_python_version(root / "payload" / "python-runtime") if rt_ok else None
    versions = _version_sources()
    info = ri.build_info(
        platform=platform,
        commit=ri.resolve_git_commit(REPO),
        python_version=py_ver,
        dependency_lock_sha256=lock_sha,
        schema_version=versions["schema_version"],
        data_format_version=versions["data_format_version"],
    )
    # §18：随包嵌入资源身份。只记「是什么字节」，**不记任何绝对路径**。
    info["embedding"] = embedding
    return info


def build(platform: str, output: Path, strict: bool = False) -> Path:
    _ensure_repo_on_path()
    from app.version import APP_VERSION            # 唯一版本源  # noqa: PLC0415

    name = f"USB-WIKI-v{APP_VERSION}-{platform}"
    dist_root = output / name
    if dist_root.exists():
        shutil.rmtree(dist_root)

    rt_ok = _stage_payload(dist_root)
    if not rt_ok:
        if strict:
            _fail_build(f"严格模式：未找到嵌入式 Python "
                        f"{REPO / 'runtime' / 'python-3.11-embed'}。"
                        " 正式发布前请先 `python setup_runtime_windows.py`。")
        print(f"[build] 警告：未找到嵌入式 Python，跳过 python-runtime 拷贝。"
              " 正式发布前请先 `python setup_runtime_windows.py`。", file=sys.stderr)

    _stage_installer(dist_root)
    doc_count = _stage_docs(dist_root)
    dist_root.mkdir(parents=True, exist_ok=True)

    import collect_licenses as cl                               # noqa: PLC0415
    import release_integrity as ri                              # noqa: PLC0415

    # 1) LICENSES（只审实际随包内容；无运行时则记录 inventory_complete=false）
    licenses = cl.collect(
        dist_root / "payload" / "python-runtime",
        REPO / "requirements-release.lock",
        dist_root,
        python_version=ri.resolve_python_version(dist_root / "payload" / "python-runtime"),
    )

    # 1.5) 随包嵌入资源（**只做本地拷贝**，缺件绝不联网现下）
    emb = _stage_embedding(dist_root)
    if emb["staged"]:
        # 2) BUILD_INFO 带上 embedding 块（§18）
        info = _build_info(dist_root, platform, rt_ok, emb["build_info"])
    elif emb.get("applicable"):
        info = _build_info(dist_root, platform, rt_ok, None)
        msg = (f"[build] 嵌入资源未随包：{emb['code']} —— {emb['reason']}")
        if strict:
            print(msg, file=sys.stderr)
        else:
            print(msg + "（非 strict：允许不带嵌入资源）", file=sys.stderr)
    else:
        # 仓库未声明嵌入能力：不打印「未随包」告警（那是契约缺失导致的误报）
        info = _build_info(dist_root, platform, rt_ok, None)
    ri.write_build_info(dist_root, info)

    # 3) RELEASE_MANIFEST → 4) SHA256SUMS（同一套枚举，单一出口）
    _, manifest = ri.write_manifest(dist_root)
    ri.write_checksums(dist_root, manifest)

    # 5) 自校验：构建产物必须能通过自己的介质校验（否则「看起来成功」最危险）
    check = ri.verify_media(dist_root)

    if strict:
        _strict_gate(dist_root, rt_ok, info, licenses, manifest, check, doc_count, emb)

    print(f"{'BUILD OK' if strict else 'DEV BUILD OK（非正式发布）'} -> {dist_root}")
    print(f"  发布格式版本           {ri.FORMAT_VERSION}")
    print(f"  文件数（清单）         {len(manifest['files'])}")
    print(f"  payload/app/           {(dist_root / 'payload' / 'app').is_dir()}")
    print(f"  payload/python-runtime/ {rt_ok}")
    print(f"  installer/              {(dist_root / 'installer' / 'install.py').is_file()}")
    print(f"  随包文档               {doc_count}")
    _summary = licenses["summary"]
    print(f"  第三方组件             {_summary['packages']}"
          f"（待复核 {_summary.get('review_required', 0)}"
          f" / 仅元数据 {_summary.get('metadata_only', 0)}）")
    if _summary.get("locked_missing"):
        print(f"  ⚠ LOCK_ENTRY_MISSING     {_summary['locked_missing']}")
    if _summary.get("version_mismatch"):
        # 发布构建从 lock 安装，正常应零差异；有差异说明 runtime 非由当前锁构建。
        # 非 strict 只是开发态提示；strict 下同一条件会以 RUNTIME_LOCK_MISMATCH 直接失败。
        print("  ⚠ RUNTIME_LOCK_MISMATCH  "
              + "；".join(_summary["version_mismatch"]))
    if _summary.get("required_metadata_only"):
        print("  ⚠ LICENSE_TEXT_MISSING   随包运行依赖缺许可原文："
              + "，".join(_summary["required_metadata_only"]))
    if _summary.get("vendor_problems"):
        print("  ⚠ VENDOR_LICENSE_INVALID " + "；".join(_summary["vendor_problems"]))
    print(f"  介质自校验             {check.code}"
          + (f" 失败 {len(check.failures)} 项" if check.failures else ""))
    return dist_root


def _strict_gate(dist_root: Path, rt_ok: bool, info: dict, licenses: dict,
                 manifest: dict, check, doc_count: int, emb: dict) -> None:
    """正式发布的完整门禁 —— 任一条件不满足即非零退出。

    每个条件带**稳定错误码**，便于 CI / 售后按码定位（不用去解析中文描述）：

        APP_MISSING / RUNTIME_MISSING / INSTALLER_MISSING
        BUILD_INFO_INCOMPLETE / MEDIA_SELFCHECK_FAILED
        LICENSES_INCOMPLETE / LOCK_ENTRY_MISSING
        **RUNTIME_LOCK_MISMATCH** —— 随包运行时与依赖锁版本不一致
        **LICENSE_TEXT_MISSING** —— 随包运行依赖只有元数据、缺许可原文
        **VENDOR_LICENSE_INVALID** —— vendor 许可原文库缺件/被改动
        LICENSE_REVIEW_REQUIRED_REMAINING —— 仍有无法确认许可的组件
        **EMBEDDING_ARTIFACT_MISSING / EMBEDDING_METADATA_INVALID /
        EMBEDDING_HASH_MISMATCH / EMBEDDING_TOKENIZER_MISSING /
        EMBEDDING_LICENSE_MISSING** —— 随包嵌入资源不可用
    """
    problems: list[tuple[str, str]] = []

    def need(ok: bool, code: str, detail: str) -> None:
        if not ok:
            problems.append((code, detail))

    summary = licenses.get("summary") or {}

    need((dist_root / "payload" / "app").is_dir(),
         "APP_MISSING", "缺少 payload/app/")
    need(rt_ok and (dist_root / "payload" / "python-runtime" / "python.exe").is_file(),
         "RUNTIME_MISSING", "缺少 payload/python-runtime/python.exe")
    need((dist_root / "installer" / "install.py").is_file(),
         "INSTALLER_MISSING", "缺少 installer/install.py")
    need((dist_root / "installer" / "install.bat").is_file(),
         "INSTALLER_MISSING", "缺少 installer/install.bat")
    need((dist_root / "installer" / "uninstall.bat").is_file(),
         "INSTALLER_MISSING", "缺少 installer/uninstall.bat")
    need(bool(info.get("git_commit")) and info.get("git_commit") != "unknown",
         "BUILD_INFO_INCOMPLETE", "BUILD_INFO.git_commit 未解析（版本不可追溯）")
    need(bool(info.get("python_version")),
         "BUILD_INFO_INCOMPLETE", "BUILD_INFO.python_version 未解析")
    need(bool(info.get("dependency_lock_sha256")),
         "BUILD_INFO_INCOMPLETE", "BUILD_INFO.dependency_lock_sha256 缺失（依赖锁不在仓库）")

    # A3.1 硬门禁 #1：BUILD_INFO 记录的是「这份 lock」的 SHA256，
    # 那么随包运行时就必须确实由这份 lock 构建 —— 否则可追溯性是假的。
    need(not summary.get("version_mismatch"),
         "RUNTIME_LOCK_MISMATCH",
         "随包运行时与依赖锁版本不一致（请按 lock 重建 runtime，禁止反向改 lock 迎合旧 runtime）："
         + "；".join(summary.get("version_mismatch") or []))

    # A3 门禁：许可清单
    need(bool(licenses.get("inventory_complete")),
         "LICENSES_INCOMPLETE", "LICENSES 清单不完整（inventory_complete=false）")
    need(not summary.get("vendor_problems"),
         "VENDOR_LICENSE_INVALID",
         "vendor 许可原文库不一致：" + "；".join(summary.get("vendor_problems") or []))
    need(not summary.get("locked_missing"),
         "LOCK_ENTRY_MISSING",
         "依赖锁条目未随包：" + ", ".join(summary.get("locked_missing") or []))
    need(not summary.get("required_metadata_only"),
         "LICENSE_TEXT_MISSING",
         "随包运行依赖只有元数据、缺许可原文（分发义务未闭）："
         + ", ".join(summary.get("required_metadata_only") or []))
    need(not summary.get("review_required"),
         "LICENSE_REVIEW_REQUIRED_REMAINING",
         f"{summary.get('review_required')} 个组件许可待人工复核"
         "（LICENSE_REVIEW_REQUIRED，V1 发布前必须清零）")

    # ---- A4.2b：随包嵌入资源（V1 标准能力，缺件即拒绝发布）----
    #   ⚠ 这里**永远不会**联网补件：取件是 maintainer 的独立动作。
    #     构建内偷偷下载会让「构建可复现」失效，也会让客户安装阶段的网络假设失真。
    #   - 资源契约（resources/embedding/default.json）声明了嵌入能力 ⇒ 必须随包且可校验；
    #   - 未声明（微型/玩具仓库、纯词法形态）⇒ 跳过，不强制（避免把「没用到嵌入」判红）。
    if emb.get("applicable", True):
        emb_code = emb.get("code") or EMB_OK
        need(emb_code == EMB_OK and bool(emb.get("staged")),
             emb_code or EMB_ARTIFACT_MISSING,
             f"随包嵌入资源不可用：{emb.get('reason') or '未知原因'}"
             "（先跑 scripts/fetch_embedding_resource.py）")
        need((dist_root / "payload" / "embedding" / "artifact.json").is_file(),
             EMB_METADATA_INVALID, "payload/embedding/artifact.json 缺失（运行时按它解析资源）")
        need((dist_root / "payload" / "embedding" / "model.onnx").is_file(),
             EMB_ARTIFACT_MISSING, "payload/embedding/model.onnx 缺失")
        need((dist_root / "payload" / "embedding" / "tokenizer.json").is_file(),
             EMB_TOKENIZER_MISSING, "payload/embedding/tokenizer.json 缺失")
        need(bool(info.get("embedding")),
             EMB_METADATA_INVALID, "BUILD_INFO.embedding 缺失（资源身份不可追溯）")
        # 模型许可必须随 Release 保留：BAAI MIT 原文 + PROVENANCE（如实记录「转换产物」关系）
        model_lic = dist_root / "LICENSES" / "models" / "bge-small-zh-v1.5"
        need((model_lic / "LICENSE").is_file() and (model_lic / "PROVENANCE.json").is_file(),
             EMB_LICENSE_MISSING,
             "LICENSES/models/bge-small-zh-v1.5/ 缺 LICENSE 或 PROVENANCE.json"
             "（base model MIT 声明必须随 Release 保留）")

    need(bool(manifest.get("files")), "MANIFEST_EMPTY", "RELEASE_MANIFEST 为空")
    need((dist_root / "SHA256SUMS").is_file(),
         "CHECKSUMS_MISSING", "缺少 SHA256SUMS")
    need(doc_count > 0, "DOCS_MISSING", "随包文档为 0（文档白名单与仓库不匹配）")
    need(check.ok, "MEDIA_SELFCHECK_FAILED",
         f"介质自校验未通过（{check.code}）：" + "；".join(check.failures[:5]))

    if problems:
        codes = " ".join(dict.fromkeys(c for c, _ in problems))
        print(f"[build] GATE FAILED codes={codes}", file=sys.stderr)
        _fail_build("严格模式门禁未通过 —— "
                    + " | ".join(f"{c}: {d}" for c, d in problems))


def main() -> int:
    ap = argparse.ArgumentParser(description="USB-WIKI Release Builder")
    ap.add_argument("--platform", default="win-x64")
    ap.add_argument("--output", default=str(REPO / "dist"), help="dist 根目录")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式（正式发布）：app/runtime/installer/BUILD_INFO/"
                         "LICENSES/MANIFEST/SHA256SUMS/自校验 全通过才退出 0；"
                         "开发模式缺 runtime 仍可构建")
    args = ap.parse_args()
    build(args.platform, Path(args.output).resolve(), strict=args.strict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
