"""发布介质测试夹具 —— 在临时目录里造一个**结构完整**的假发布包。

为什么要夹具
============
A3 起，安装器在动任何东西之前必须先过介质完整性闸门（RELEASE_MANIFEST + SHA256）。
因此凡是走 `install()` 的测试，都必须先有一份**能通过校验**的介质。
把「造介质」收敛到这一处，A2 / A3 各场景才不必各自拼装。

**全程只用临时目录**，绝不触碰真实 LOCALAPPDATA / Documents / Library / 桌面。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"


def _load(name: str, filename: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    # 必须先进 sys.modules：@dataclass 解析 `from __future__ import annotations` 的
    # 字符串注解时要查 sys.modules[cls.__module__]，否则 AttributeError: NoneType.__dict__
    sys.modules[name] = mod
    # 测试加载不得在 scripts/ 留下 __pycache__（否则污染 --verify / --clean 的发布清洁度判定）
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


def release_integrity():
    return _load("release_integrity_fixture", "release_integrity.py")


def load_installer():
    return _load("install_windows_fixture", "install_windows.py")


def load_script(module_name: str, filename: str):
    """按文件名加载 scripts/ 下的脚本（自动注册 sys.modules，供测试独立使用）。"""
    return _load(module_name, filename)


def write_release_artifacts(root: Path, *, app_version: str = "9.9.9") -> Path:
    """生成 BUILD_INFO.json / RELEASE_MANIFEST.json / SHA256SUMS（供临时介质使用）。"""
    ri = release_integrity()
    info = ri.build_info(
        platform="win-x64",
        commit="0" * 40,
        python_version="3.11.9",
        dependency_lock_sha256="f" * 64,
        schema_version="1.4",
        data_format_version=1,
        build_time_utc="2026-01-01T00:00:00Z",
    )
    info["app_version"] = app_version
    ri.write_build_info(root, info)
    _, manifest = ri.write_manifest(root)
    ri.write_checksums(root, manifest)
    return root


def make_fake_release(parent: Path, *, app_version: str = "1.0.0",
                      marker: str = "v1") -> Path:
    """造一个最小但**可用/可校验**的假发布包，返回发布根目录。

    结构与真实发布一致：installer/ + payload/{app,python-runtime} +
    LICENSES/ + BUILD_INFO.json + MANIFEST + SHA256SUMS。
    python.exe 是占位空文件（非 Windows / 无运行时环境下不会真的被启动）。
    """
    root = Path(parent) / "release"
    if root.exists():
        import shutil
        shutil.rmtree(root)
    (root / "installer").mkdir(parents=True)
    (root / "payload" / "app").mkdir(parents=True)
    (root / "payload" / "python-runtime").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "LICENSES").mkdir(parents=True)

    (root / "payload" / "app" / "launcher.py").write_text(
        "# fake launcher\n", encoding="utf-8")
    (root / "payload" / "app" / "VERSION.txt").write_text(
        app_version, encoding="utf-8")
    (root / "payload" / "app" / "mod.py").write_text(
        f"MARKER = {marker!r}\n", encoding="utf-8")
    (root / "payload" / "python-runtime" / "python.exe").write_bytes(b"")

    (root / "installer" / "install.py").write_text(
        (SCRIPTS / "install_windows.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    (root / "installer" / "release_integrity.py").write_text(
        (SCRIPTS / "release_integrity.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    (root / "installer" / "install.bat").write_text(
        "@echo off\r\nsetlocal\r\n", encoding="ascii")

    (root / "docs" / "README.md").write_text("# fake\n", encoding="utf-8")
    (root / "LICENSES" / "THIRD_PARTY.json").write_text(
        '{"format_version": 1, "inventory_complete": true, "packages": []}\n',
        encoding="utf-8")

    write_release_artifacts(root, app_version=app_version)
    return root
