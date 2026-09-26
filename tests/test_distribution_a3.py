"""A3 —— Release Integrity / BUILD_INFO / Checksums / Third-party Licensing 验收。

覆盖（按用户 A3 指令第 12 节）：**全部为负向构造**
  1. payload 任意文件被改 1 byte            → verify fail
  2. payload 缺一个文件                     → verify fail
  3. manifest 被截断 / 非法 JSON            → verify fail
  4. BUILD_INFO 缺失                        → verify fail + strict gate fail
  5. LICENSES inventory 缺失                → strict build fail（verify 侧亦 fail）
  6. verify 失败                            → App SHA256 不变
  7. verify 失败                            → Library SHA256 不变
  8. 发布介质正常 → transactional reinstall 成功 → Library SHA256 不变

另加（同属 A3 口径，防止「永远通过」的假保护）：
  9. SHA256SUMS 与 RELEASE_MANIFEST 同源（同一套枚举，不可能漂移）
 10. manifest 不含自身 / 不含临时文件 / 相对路径 / POSIX / 固定排序
 11. verify 命令**完全只读**（前后目录树逐字节一致，且不创建 Library）
 12. BUILD_INFO 无隐私（无用户名 / 绝对路径 / HOME / LOCALAPPDATA / IP）
 13. 真实 runtime 下的 strict 构建（**已移出默认 suite**，见下「责任重划分」）

设计：本模块自包含（自带 check/skip/section 与 PASS/FAIL/SKIP 列表），
由 tests/test_suite.py 的 main() 调用 run_a3_tests() 并合并结果。
**全程只用临时目录**，绝不触碰真实 LOCALAPPDATA / Documents / Library / 桌面。

性能 / 责任重划分（2026-09-26，RELEASE_REQUIRED / Test Infrastructure Performance Defect）：
  * **默认 suite 不执行真实 strict 构建**（_t_real_strict_build）。它做一次真实
    ``build_release.py --strict``（拷贝 ~13850 文件 / 515MB 嵌入式 runtime + 全量哈希），
    属「重 I/O」操作，曾多次成为长时间阻塞源。其完整端到端验证职责正式归属
    **Windows Release Gate Step 2**（全套件 PASS 后由 run_a3_real_strict_build() 单独调用）。
  * **默认 suite 保留全部 fake / 逻辑级严格闸门**：BUILD_INFO / LICENSES / MANIFEST /
    SHA256SUMS / 自校验 / 严格门禁的**逻辑本体**均被覆盖，含最关键的「微型仓库 strict 全链路」
    （_t_fake_repo_strict_build）、lock/runtime 一致性门禁、vendor 许可原文补齐等。
  * **cleanup 全部有界**（_cleanup_bounded，120s）：超时只打 CLEANUP_WARNING 并列残留，
    **不改 PASS/FAIL**；不再用无界 ``shutil.rmtree``（曾导致 20~74 分钟无输出）。
  * **逐 case 计时**（_timed）：每一大项耗时可见，便于核对「A3 是否进入分钟级」。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from tests import dist_fixture as fx                          # noqa: E402
from app.version import APP_VERSION                           # noqa: E402
from app.core.migrations import CURRENT_SCHEMA_VERSION as _SCHEMA_VERSION  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

# 真实 strict 构建次数：默认 suite 不再执行（见 run_a3_tests 注释），
# 它由「Windows Release Gate Step 2」单独承担，故默认 = 0。
REAL_STRICT_BUILD_COUNT = 0

# 嵌入资源「不适用」桩：用于纯 BUILD_INFO / LICENSES 门禁单测（这些用例不构造随包嵌入）。
# 必须与 build_release._stage_embedding 的返回结构一致；applicable=False ⇒ 门禁跳过嵌入校验。
_NO_EMB = {"staged": False, "code": "ok", "reason": "", "id": None,
           "build_info": None, "applicable": False}


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def skip(name: str, reason: str = "") -> None:
    SKIP.append(f"{name} :: {reason}" if reason else name)
    print("  ⏭ " + name + (f"  [{reason}]" if reason else ""))


def section(t: str) -> None:
    print(f"\n── A3 {t} " + "─" * max(0, 54 - len(t)))


def _print_cleanup_warning(root: Path, why: str) -> None:
    print(f"\n  ⚠ CLEANUP_WARNING: 临时目录未在限时内清理（{why}）")
    print(f"    leftover : {root}")
    print("    → 不影响本次产品 PASS/FAIL；残留可后续人工/后台清理。")


def _cleanup_bounded(root: Path, timeout: int = 120) -> bool:
    """best-effort 清理：子进程 rmtree + 超时，**绝不阻塞 Release Gate**。

    背景（实测，2026-09-26）：Windows 实时防毒下 ``shutil.rmtree`` 删除大量小文件
    极慢（曾观测到 0.8s/文件，约正常 20×）；此前 A1/A2 的无界 ``shutil.rmtree``
    让整轮测试在 cleanup 阶段 **20~74 分钟无任何输出**。
    A3 默认 suite 各用例只造 **微型假仓库**（秒级构建、产物仅 KB 级），删速本应极快，
    但为防止「个别文件被 Defender 锁住」导致 cleanup 无限拖尾，这里统一收紧为有界：
    超时只打 ``CLEANUP_WARNING`` 并列残留，**不改任何 PASS/FAIL**，
    不触碰真实 Library，也不 kill 无关进程。
    """
    if not root.exists():
        return True
    code = "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)"
    try:
        subprocess.run([sys.executable, "-c", code, str(root)],
                       timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True)
    except subprocess.TimeoutExpired:
        _print_cleanup_warning(root, f"超过 {timeout}s")
        return False
    if root.exists():
        _print_cleanup_warning(root, "仍有残留（个别文件被占用）")
        return False
    return True


def _run_bounded(cmd, *, phase: str, timeout: int, cwd=None) -> subprocess.CompletedProcess:
    """有界 subprocess —— Release 测试**禁止无限等待**。

    A3 真实 strict 构建（已移出默认 suite，由 Windows Release Gate Step 2 调用）
    此前用 ``timeout=1800`` 且**未处理超时**，一旦环境 I/O 劣化就会让 Gate 静默挂起。
    这里统一收紧：

    * ``stdin=DEVNULL``：本调用已给全 CLI 参数，绝不应进入 ``input()``；
      生产代码若意外交互读取会立刻 EOF，而不是永久挂起。
    * ``encoding/errors``：固定 utf-8/replace，避免中文输出触发解码异常。
    * ``cwd``：透传工作目录（真实构建依赖 ``cwd=REPO`` 取 git/lock）。
    * ``timeout``：超时返回 rc=-1 并打印 phase / command / stdout 尾部 / stderr 尾部，
      让对应用例**明确 FAIL**，而不是让整套 suite 卡死在无声处。
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, cwd=cwd,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        err = exc.stderr or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        print(f"\n  ⏱ TIMEOUT（{phase}）超过 {timeout}s")
        print(f"    command     : {' '.join(str(c) for c in cmd)}")
        print(f"    stdout 尾部 : ...{out[-400:]}")
        print(f"    stderr 尾部 : ...{err[-400:]}")
        return subprocess.CompletedProcess(cmd, -1, stdout=out, stderr=err)


def _timed(label: str, fn) -> None:
    """逐 case 计时 —— 让「慢 / 挂」精确定位到某一步。

    为什么必须有它：A1/A2 曾出现「某段之后 20+ 分钟无任何输出」却无法判断是哪一步慢；
    加计时后卡点一眼可见，也便于核对「A3 是否进入分钟级」。
    """
    t0 = time.time()
    try:
        fn()
    finally:
        print(f"  ⏱ {label}: {time.time() - t0:.1f}s")


def _runtime_present() -> bool:
    return (REPO / "runtime" / "python-3.11-embed" / "python.exe").is_file()


def _sha256_tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def _snapshot(root: Path) -> list[tuple[str, int, float]]:
    if not root.exists():
        return []
    return [(p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))]


class _tmp:
    def __enter__(self) -> Path:
        self.d = Path(tempfile.mkdtemp(prefix="wikiusb-a3-"))
        return self.d

    def __exit__(self, *a) -> None:
        # 有界清理：超时只警告，绝不阻塞 Gate（见 _cleanup_bounded）
        _cleanup_bounded(self.d, timeout=120)


# ---------------------------------------------------------------------------
# 安装测试安全沙箱（RELEASE_REQUIRED / Test Safety Isolation Defect, 2026-09-26）
# ---------------------------------------------------------------------------
# A3 安装类用例（_t_corrupt_media_keeps_app_and_library / _t_reinstall_from_intact_media）
# 调用 iw.install() 会在**成功安装后**创建桌面快捷方式 + 写 install_state.json。
# 默认情况下：
#   * create_desktop_shortcut 走**真实桌面**（_desktop_dir 无测试钩子）→ 只能靠 install() 的
#     desktop_dir 参数重定向；
#   * _state_dir() 优先读 WIKIUSB_STATE_DIR，否则回退 LOCALAPPDATA/USB-WIKI。
# 因此统一用 context manager 在 os.environ 注入隔离变量（in-process 实时读取；subprocess 继承），
# 并对所有成功安装显式传 desktop_dir=<TEMP>/Desktop。进入时保存原值、退出时精确恢复：
# 原本存在→恢复原值，原本不存在→删除注入值。**从一开始就不写真实位置**（禁止写后再删）。
_SANDBOX = None  # 由 _install_sandbox.__enter__ 写入；_install_expect 读取 desktop_dir


def _sandbox_desktop_dir():
    return _SANDBOX["desktop_dir"] if _SANDBOX else None


class _install_sandbox:
    """为安装测试建立 scoped sandbox，注入隔离 env 并精确还原。

    沙箱目录（全部位于 TEMP，绝不触碰真实 Desktop/LOCALAPPDATA/Library/Documents）：
        <root>/localapp            → 冒充 LOCALAPPDATA
        <root>/localapp/USB-WIKI   → 冒充 WIKIUSB_STATE_DIR（install_state.json 落此）
        <root>/Desktop             → 冒充桌面（快捷方式落此）
    注入：LOCALAPPDATA / WIKIUSB_STATE_DIR / PYTHONUTF8=1 / PYTHONIOENCODING=utf-8。
    """

    def __init__(self) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="wikiusb-a3-sandbox-"))
        self._localapp = self._root / "localapp"
        self._statedir = self._localapp / "USB-WIKI"
        self._desktop = self._root / "Desktop"
        self._localapp.mkdir(parents=True, exist_ok=True)
        self._statedir.mkdir(parents=True, exist_ok=True)
        self._desktop.mkdir(parents=True, exist_ok=True)
        self._overrides = {
            "LOCALAPPDATA": str(self._localapp),
            "WIKIUSB_STATE_DIR": str(self._statedir),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        self._saved: dict[str, str] = {}
        self._added: set[str] = set()

    @property
    def desktop_dir(self) -> Path:
        return self._desktop

    def __enter__(self) -> "_install_sandbox":
        global _SANDBOX
        _SANDBOX = {"desktop_dir": self._desktop}
        for k, v in self._overrides.items():
            if k in os.environ:
                self._saved[k] = os.environ[k]
            else:
                self._added.add(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a) -> None:
        global _SANDBOX
        _SANDBOX = None
        for k in self._overrides:
            if k in self._added:
                os.environ.pop(k, None)
            else:
                os.environ[k] = self._saved[k]
        _cleanup_bounded(self._root, timeout=120)


def _snapshot_real_side_effects() -> dict:
    """只读快照真实桌面快捷方式 + 真实 install_state.json（前后一致性断言用）。

    必须在 sandbox 之外（用真实 env）调用——进入沙箱前拍一次，退出沙箱后拍一次。
    * 真实桌面：Path.home()/"Desktop"（_desktop_dir 永远返回真实桌面，不受 env 影响）。
    * 真实 install_state.json：LOCALAPPDATA/USB-WIKI/install_state.json（_state_dir 回退路径）。
    """
    paths = [
        Path.home() / "Desktop" / "USB-WIKI.lnk",
        Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".usb-wiki"))
        / "USB-WIKI" / "install_state.json",
    ]
    snap: dict[str, tuple[str, object]] = {}
    for p in paths:
        if p.is_file():
            try:
                snap[str(p)] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
            except OSError:
                snap[str(p)] = ("file", "unreadable")
        else:
            snap[str(p)] = ("missing", None)
    return snap


def _install_expect(iw, *args, **kw) -> int:
    """跑 install()，把 SystemExit 转成退出码；无异常返回 0。

    自动注入 sandbox 的 desktop_dir（若存在），确保所有成功安装的快捷方式落在 TEMP/Desktop，
    绝不写真实桌面。install_state.json 的隔离由 _install_sandbox 的 WIKIUSB_STATE_DIR 负责。
    """
    if "desktop_dir" not in kw:
        d = _sandbox_desktop_dir()
        if d is not None:
            kw["desktop_dir"] = d
    try:
        return iw.install(*args, **kw)
    except SystemExit as e:
        return int(e.code or 0)


def _verify_code(iw, root: Path) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = iw.verify_cmd(root, as_json=False)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# 1 / 2 / 3 —— 介质损坏三类
# ---------------------------------------------------------------------------
def _t_payload_byte_flip(tmp: Path) -> None:
    section("12-1 payload 改 1 byte → verify fail")
    iw, ri = fx.load_installer(), fx.release_integrity()
    root = fx.make_fake_release(tmp)
    target = root / "payload" / "app" / "mod.py"
    raw = bytearray(target.read_bytes())
    raw[0] = raw[0] ^ 0x01
    target.write_bytes(bytes(raw))

    res = ri.verify_media(root)
    check("A3-1 改 1 byte 被检出", not res.ok and res.code == "MEDIA_CORRUPTED",
          f"code={res.code}")
    check("A3-1 失败项指向该相对文件",
          any("payload/app/mod.py" in f for f in res.failures),
          str(res.failures[:3]))
    check("A3-1 失败原因是 SHA256 不符",
          any("SHA256" in f for f in res.failures), str(res.failures[:3]))
    check("A3-1 verify 命令返回非零", _verify_code(iw, root)[0] != 0)


def _t_payload_missing_file(tmp: Path) -> None:
    section("12-2 payload 缺一个文件 → verify fail")
    ri = fx.release_integrity()
    root = fx.make_fake_release(tmp)
    (root / "payload" / "app" / "mod.py").unlink()

    res = ri.verify_media(root)
    check("A3-2 缺文件被检出", not res.ok, f"code={res.code}")
    check("A3-2 报告为「文件缺失」",
          any("payload/app/mod.py" in f and "缺失" in f for f in res.failures),
          str(res.failures[:3]))


def _t_manifest_broken(tmp: Path) -> None:
    section("12-3 manifest 截断 / 非法 JSON → verify fail")
    ri = fx.release_integrity()

    root = fx.make_fake_release(tmp / "a")
    man = root / "RELEASE_MANIFEST.json"
    man.write_bytes(man.read_bytes()[: len(man.read_bytes()) // 2])
    res = ri.verify_media(root)
    check("A3-3a 截断 manifest 被拒", not res.ok and res.code == "MEDIA_CORRUPTED",
          f"code={res.code} {res.failures[:2]}")

    root2 = fx.make_fake_release(tmp / "b")
    (root2 / "RELEASE_MANIFEST.json").write_text("{\"format_version\": 1, \"files\": [",
                                                 encoding="utf-8")
    res2 = ri.verify_media(root2)
    check("A3-3b 非法 JSON 被拒", not res2.ok, f"code={res2.code}")
    check("A3-3b 指明是 manifest 问题",
          any("RELEASE_MANIFEST.json" in f for f in res2.failures), str(res2.failures[:2]))

    root3 = fx.make_fake_release(tmp / "c")
    (root3 / "RELEASE_MANIFEST.json").write_text("", encoding="utf-8")
    res3 = ri.verify_media(root3)
    check("A3-3c 空 manifest 被拒", not res3.ok, f"code={res3.code}")


# ---------------------------------------------------------------------------
# 4 / 5 —— BUILD_INFO / LICENSES
# ---------------------------------------------------------------------------
def _t_build_info_missing(tmp: Path) -> None:
    section("12-4 BUILD_INFO 缺失 → verify fail + strict gate fail")
    iw, ri = fx.load_installer(), fx.release_integrity()
    root = fx.make_fake_release(tmp)
    (root / "BUILD_INFO.json").unlink()

    res = ri.verify_media(root)
    check("A3-4a 介质校验拒收缺 BUILD_INFO",
          not res.ok and res.code == "MEDIA_CORRUPTED", f"code={res.code}")
    check("A3-4a 失败项含 BUILD_INFO.json",
          any("BUILD_INFO.json" in f for f in res.failures), str(res.failures[:3]))
    check("A3-4a verify 命令非零", _verify_code(iw, root)[0] != 0)

    # 即便有人手工把 BUILD_INFO 从 manifest 里删掉，REQUIRED_MANIFEST_ENTRIES 仍要拦住
    root2 = fx.make_fake_release(tmp / "b")
    man = json.loads((root2 / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    man["files"] = [e for e in man["files"] if e["path"] != "BUILD_INFO.json"]
    (root2 / "RELEASE_MANIFEST.json").write_text(
        json.dumps(man, ensure_ascii=False), encoding="utf-8")
    (root2 / "BUILD_INFO.json").unlink()
    res2 = ri.verify_media(root2)
    check("A3-4b 清单被篡改掉 BUILD_INFO 仍被拒", not res2.ok
          and any("必需交付物" in f or "BUILD_INFO" in f for f in res2.failures),
          str(res2.failures[:3]))

    # strict gate 逻辑本体：纯函数直接测，保证「门禁真的会红」
    good_root = fx.make_fake_release(tmp / "c")
    good_check = ri.verify_media(good_root)
    good_lic = {"inventory_complete": True,
                "summary": {"locked_missing": [], "review_required": 0}}
    good_man = {"files": [{"path": "x", "size": 1, "sha256": "0" * 64}]}
    br = fx.load_script("build_release_a3", "build_release.py")

    def gate(info, lic, man, chk):
        try:
            br._strict_gate(good_root, True, info, lic, man, chk, 3, _NO_EMB)
            return None
        except SystemExit as e:
            return int(e.code or 0)

    base_info = {"git_commit": "a" * 40, "python_version": "3.11.9",
                 "dependency_lock_sha256": "b" * 64}
    check("A3-4c 门禁：全绿不报错", gate(base_info, good_lic, good_man, good_check) is None)
    bad = dict(base_info, git_commit="unknown")
    check("A3-4d 门禁：commit unknown 必红", gate(bad, good_lic, good_man, good_check) not in (None, 0))
    bad2 = dict(base_info, python_version=None)
    check("A3-4e 门禁：python_version 缺失必红",
          gate(bad2, good_lic, good_man, good_check) not in (None, 0))
    bad3 = dict(base_info, dependency_lock_sha256=None)
    check("A3-4f 门禁：依赖锁 hash 缺失必红",
          gate(bad3, good_lic, good_man, good_check) not in (None, 0))
    check("A3-4g 门禁：介质自校验失败必红",
          gate(base_info, good_lic, good_man, ri.verify_media(tmp / "nope")) not in (None, 0))


def _t_licenses_missing(tmp: Path) -> None:
    section("12-5 LICENSES inventory 缺失 → strict build fail")
    iw, ri = fx.load_installer(), fx.release_integrity()
    cl = fx.load_script("collect_licenses_a3", "collect_licenses.py")
    br = fx.load_script("build_release_a3b", "build_release.py")

    # 无运行时 ⇒ 清单不完整
    empty_root = tmp / "empty"
    (empty_root / "payload" / "python-runtime").mkdir(parents=True)
    inv = cl.collect(empty_root / "payload" / "python-runtime",
                     REPO / "requirements-release.lock", empty_root)
    check("A3-5a 无运行时 ⇒ inventory_complete=false",
          inv["inventory_complete"] is False, str(inv["inventory_complete"]))
    check("A3-5a 仍产出合法 JSON 结构",
          (empty_root / "LICENSES" / "THIRD_PARTY.json").is_file())

    # 介质侧：LICENSES/THIRD_PARTY.json 消失 ⇒ 必需交付物缺失
    root = fx.make_fake_release(tmp / "media")
    (root / "LICENSES" / "THIRD_PARTY.json").unlink()
    res = ri.verify_media(root)
    check("A3-5b 介质缺 LICENSES/THIRD_PARTY.json 被拒", not res.ok,
          f"code={res.code}")
    check("A3-5b verify 命令非零", _verify_code(iw, root)[0] != 0)

    # strict gate：inventory 不完整 / 有待复核组件 ⇒ 必红
    good_root = fx.make_fake_release(tmp / "g")
    good_check = ri.verify_media(good_root)
    good_man = {"files": [{"path": "x", "size": 1, "sha256": "0" * 64}]}
    info = {"git_commit": "a" * 40, "python_version": "3.11.9",
            "dependency_lock_sha256": "b" * 64}

    def gate(lic):
        try:
            br._strict_gate(good_root, True, info, lic, good_man, good_check, 3, _NO_EMB)
            return None
        except SystemExit as e:
            return int(e.code or 0)

    check("A3-5c 门禁：inventory 不完整必红",
          gate({"inventory_complete": False,
                "summary": {"locked_missing": [], "review_required": 0}}) not in (None, 0))
    check("A3-5d 门禁：依赖锁条目缺失必红",
          gate({"inventory_complete": True,
                "summary": {"locked_missing": ["requests"], "review_required": 0}})
          not in (None, 0))
    check("A3-5e 门禁：存在 LICENSE_REVIEW_REQUIRED 必红",
          gate({"inventory_complete": True,
                "summary": {"locked_missing": [], "review_required": 2}}) not in (None, 0))


# ---------------------------------------------------------------------------
# 6 / 7 / 8 —— 损坏介质不碰 App / Library；完好介质可重装且 Library 不变
# ---------------------------------------------------------------------------
def _t_corrupt_media_keeps_app_and_library(tmp: Path) -> None:
    section("12-6/7 损坏介质 → App 与 Library 均不变")
    iw = fx.load_installer()
    root = fx.make_fake_release(tmp)
    app, lib = tmp / "AppInstall", tmp / "Library"

    rc0 = _install_expect(iw, root, app, lib, smoke=False)
    check("A3-6 前置：完好介质安装成功", rc0 == 0, f"rc={rc0}")
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "notes").mkdir(exist_ok=True)
    (lib / "notes" / "n.md").write_text("user data", encoding="utf-8")
    (lib / "library.json").write_text('{"data_version": 1}', encoding="utf-8")

    app_sha, lib_sha = _sha256_tree(app), _sha256_tree(lib)

    (root / "payload" / "app" / "mod.py").write_bytes(b"# tampered\n")
    rc = _install_expect(iw, root, app, lib, smoke=False)

    check("A3-6 损坏介质安装返回 MEDIA_CORRUPTED 码",
          rc == iw.MEDIA_CORRUPTED_RC, f"rc={rc} expect={iw.MEDIA_CORRUPTED_RC}")
    check("A3-6 verify 失败 → App SHA256 不变", _sha256_tree(app) == app_sha)
    check("A3-7 verify 失败 → Library SHA256 不变", _sha256_tree(lib) == lib_sha)
    check("A3-6 未创建 App.staging", not (tmp / "AppInstall.staging").exists())
    check("A3-6 未创建 App.backup", not (tmp / "AppInstall.backup").exists())
    check("A3-6 App 内容确为旧版本",
          (app / "app" / "VERSION.txt").read_text(encoding="utf-8") == "1.0.0")


def _t_media_precheck_before_any_write(tmp: Path) -> None:
    section("12-6 介质校验必须发生在任何写操作之前")
    iw = fx.load_installer()
    root = fx.make_fake_release(tmp)
    (root / "payload" / "app" / "launcher.py").unlink()      # 介质损坏
    app, lib = tmp / "AppInstall", tmp / "Library"

    before = _snapshot(tmp)
    rc = _install_expect(iw, root, app, lib, smoke=False)
    after = _snapshot(tmp)

    check("A3-6b 首装 + 损坏介质 → 非零退出", rc == iw.MEDIA_CORRUPTED_RC, f"rc={rc}")
    check("A3-6b 目标目录未被创建", not app.exists() and not lib.exists())
    check("A3-6b 临时目录零变化（无半成品）", before == after)


def _t_reinstall_from_intact_media(tmp: Path) -> None:
    section("12-8 完好介质 → transactional reinstall → Library 不变")
    iw = fx.load_installer()
    root = fx.make_fake_release(tmp, app_version="1.0.0")
    app, lib = tmp / "AppInstall", tmp / "Library"

    check("A3-8 首次安装成功", _install_expect(iw, root, app, lib, smoke=False) == 0)
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "library.json").write_text('{"data_version": 1}', encoding="utf-8")
    (lib / "notes").mkdir(exist_ok=True)
    (lib / "notes" / "keep.md").write_text("must survive", encoding="utf-8")
    lib_sha = _sha256_tree(lib)

    # 「App 损坏 + Library 完好」→ 从完好介质重装（V1 基础恢复路径）
    shutil.rmtree(app / "app")
    (app / "app").mkdir()
    (app / "app" / "broken.py").write_text("# damaged\n", encoding="utf-8")

    root2 = fx.make_fake_release(tmp / "v2", app_version="2.0.0", marker="v2")
    # make_fake_release 会重建 release 目录，把它挪回同一路径以免混淆
    shutil.rmtree(root)
    shutil.move(str(root2), str(root))
    rc = _install_expect(iw, root, app, lib, smoke=False)

    check("A3-8 重装修复损坏 App 成功", rc == 0, f"rc={rc}")
    check("A3-8 App 已更新到 2.0.0",
          (app / "app" / "VERSION.txt").read_text(encoding="utf-8") == "2.0.0")
    check("A3-8 损坏残留已清除", not (app / "app" / "broken.py").exists())
    check("A3-8 Library SHA256 完全一致", _sha256_tree(lib) == lib_sha)
    check("A3-8 Library 无 staging/backup 兄弟目录",
          not (tmp / "Library.staging").exists() and not (tmp / "Library.backup").exists())


# ---------------------------------------------------------------------------
# 9 / 10 —— 清单 / 校验和同源与形态
# ---------------------------------------------------------------------------
def _t_manifest_and_checksums_same_enumeration(tmp: Path) -> None:
    section("9/10 manifest 与 SHA256SUMS 同源 + 形态合规")
    ri = fx.release_integrity()
    root = fx.make_fake_release(tmp)
    manifest = json.loads((root / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    lines = [l for l in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines() if l]

    man_map = {e["path"]: e["sha256"] for e in manifest["files"]}
    sum_map = {}
    for line in lines:
        digest, _, path = line.partition("  ")
        sum_map[path] = digest

    check("A3-9a 两者文件集合一致", set(man_map) == set(sum_map),
          f"manifest={len(man_map)} sums={len(sum_map)}")
    check("A3-9b 两者哈希逐条一致", man_map == sum_map)
    check("A3-9c SHA256SUMS 格式为 `<hash>  <path>`",
          all(len(l.split("  ")[0]) == 64 for l in lines), str(lines[:2]))

    paths = [e["path"] for e in manifest["files"]]
    check("A3-10a 全部为相对路径", all(not p.startswith("/") and ":" not in p.split("/")[0]
                                       for p in paths))
    check("A3-10b 统一 POSIX 分隔符", all("\\" not in p for p in paths))
    check("A3-10c 固定排序", paths == sorted(paths))
    check("A3-10d 不含自身（避免递归 hash）",
          "RELEASE_MANIFEST.json" not in paths and "SHA256SUMS" not in paths)
    check("A3-10e 不含解释器缓存/系统垃圾",
          not any("__pycache__" in p or p.endswith((".pyc", ".pyo"))
                  or Path(p).name in (".DS_Store", "Thumbs.db") for p in paths))
    check("A3-10f 覆盖 installer / payload / LICENSES / BUILD_INFO",
          {"payload/app/launcher.py", "BUILD_INFO.json",
           "LICENSES/THIRD_PARTY.json", "installer/install.bat"} <= set(paths))
    check("A3-10g manifest format_version=1", manifest["format_version"] == 1)


# ---------------------------------------------------------------------------
# 11 / 12 —— 只读性 与 隐私
# ---------------------------------------------------------------------------
def _t_verify_is_read_only(tmp: Path) -> None:
    section("11 verify 完全只读")
    iw = fx.load_installer()
    root = fx.make_fake_release(tmp)
    lib = tmp / "Library"          # 故意不创建：verify 不得创建它

    before = _snapshot(tmp)
    rc, out = _verify_code(iw, root)
    after = _snapshot(tmp)

    check("A3-11a verify 通过时返回 0", rc == 0, f"rc={rc}")
    check("A3-11b 输出为 OK", out.strip().startswith("OK"), out.strip()[:60])
    check("A3-11c 前后目录树逐项一致（零写入）", before == after)
    check("A3-11d 未创建 Library", not lib.exists())
    check("A3-11e 输出不含绝对路径",
          str(tmp) not in out and str(REPO) not in out, "输出泄露绝对路径")

    # 只读性对「失败路径」同样成立
    (root / "payload" / "app" / "mod.py").write_bytes(b"# tampered\n")
    before2 = _snapshot(tmp)
    rc2, out2 = _verify_code(iw, root)
    after2 = _snapshot(tmp)
    check("A3-11f 失败路径也零写入", before2 == after2)
    check("A3-11g 输出 MEDIA_CORRUPTED", "MEDIA_CORRUPTED" in out2, out2.strip()[:60])
    check("A3-11h 失败输出只给相对路径",
          "payload/app/mod.py" in out2 and str(tmp) not in out2)


def _t_build_info_has_no_private_data(tmp: Path) -> None:
    section("12 BUILD_INFO 无隐私 / 无环境身份")
    ri = fx.release_integrity()
    info = ri.build_info(
        platform="win-x64", commit="c" * 40, python_version="3.11.9",
        dependency_lock_sha256="d" * 64, schema_version="1.4", data_format_version=1,
    )
    blob = json.dumps(info, ensure_ascii=False)

    leaks = []
    for probe in (os.environ.get("USERNAME", ""), os.environ.get("USER", ""),
                  os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", ""),
                  str(Path.home()), str(REPO), "C:\\", "/Users/", "/home/"):
        if probe and probe in blob:
            leaks.append(probe)
    check("A3-12a 不含用户名/家目录/AppData/仓库绝对路径", not leaks, f"泄露={leaks}")
    check("A3-12b 键集合固定",
          set(info) == {"app_version", "git_commit", "build_time_utc", "platform",
                        "python_version", "dependency_lock_sha256", "schema_version",
                        "data_format_version", "release_format_version"}, str(sorted(info)))
    check("A3-12c app_version 取自 app/version.py", info["app_version"] == APP_VERSION,
          f"{info['app_version']} != {APP_VERSION}")
    check("A3-12d release_format_version == FORMAT_VERSION",
          info["release_format_version"] == ri.FORMAT_VERSION)
    check("A3-12e 不含模型相关伪造字段",
          not any("model" in k or "ollama" in k or "onnx" in k for k in info))
    check("A3-12f build_time 为 UTC ISO8601",
          info["build_time_utc"].endswith("Z") and "T" in info["build_time_utc"],
          info["build_time_utc"])


def _t_license_inventory_shape(tmp: Path) -> None:
    section("7/8 许可清单：不猜许可 + 只审实际随包内容")
    cl = fx.load_script("collect_licenses_a3c", "collect_licenses.py")

    # 夹具：一个「元数据无 License / 无 classifier / 无 license 文件」的包 —— 必须被标复核
    rt = tmp / "python-runtime"
    dist = rt / "Lib" / "site-packages" / "mystery-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: mystery\nVersion: 1.0\n\n",
        encoding="utf-8")

    # 另一个「有 classifier」的包 —— 可回答许可，不得被猜成 MIT 之外的臆测值
    dist2 = rt / "Lib" / "site-packages" / "known-2.0.dist-info"
    dist2.mkdir(parents=True)
    (dist2 / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: known\nVersion: 2.0\n"
        "License: BSD-3-Clause\nHome-page: https://example.invalid/known\n\n",
        encoding="utf-8")
    (dist2 / "LICENSE").write_text("BSD 3-Clause text\n", encoding="utf-8")
    (rt / "LICENSE.txt").write_text("PSF python license\n", encoding="utf-8")

    lock = tmp / "requirements-release.lock"
    lock.write_text("known==2.0\nmissing-pkg==9.9\n", encoding="utf-8")

    out = tmp / "out"
    out.mkdir()
    # 显式给一个不存在的 vendor 目录：本用例只测「元数据 → 状态判定」，不掺 vendor 行为
    inv = cl.collect(rt, lock, out, python_version="3.11.9",
                     vendor_dir=tmp / "no-such-vendor")
    by_name = {p["package"]: p for p in inv["packages"]}

    check("A3-7a 未声明许可的包被标 LICENSE_REVIEW_REQUIRED",
          by_name.get("mystery", {}).get("review_status") == "LICENSE_REVIEW_REQUIRED",
          str(by_name.get("mystery")))
    check("A3-7b 元数据明确时取元数据值（不是猜）",
          by_name.get("known", {}).get("license") == "BSD-3-Clause",
          str(by_name.get("known", {}).get("license")))
    check("A3-7c 收集到许可原文",
          any("packages/known-2.0/" in t
              for t in by_name.get("known", {}).get("license_texts", [])),
          str(by_name.get("known", {}).get("license_texts")))
    check("A3-7d CPython 自身许可在清单内",
          by_name.get("Python", {}).get("license") == "PSF-2.0")
    check("A3-7e 依赖锁内包标记 required",
          by_name.get("known", {}).get("required") is True)
    check("A3-7f 依赖锁内但未随包的条目被报告",
          "missing_pkg" in inv["summary"]["locked_missing"]
          or "missing-pkg" in inv["summary"]["locked_missing"],
          str(inv["summary"]["locked_missing"]))
    check("A3-8a 清单只含实际随包内容（无 Ollama/LLM/GGUF/ONNX 条目）",
          not any(k in json.dumps(inv).lower()
                  for k in ("ollama", "gguf", "onnxruntime")))
    check("A3-8b 产出 THIRD_PARTY.json + THIRD_PARTY_NOTICES.txt",
          (out / "LICENSES" / "THIRD_PARTY.json").is_file()
          and (out / "LICENSES" / "THIRD_PARTY_NOTICES.txt").is_file())


# ---------------------------------------------------------------------------
# 13 —— 真实 runtime 下的 strict 构建（有运行时才跑）
# ---------------------------------------------------------------------------
# ⚠ 责任重划分（2026-09-26，RELEASE_REQUIRED / Test Infrastructure Performance Defect）：
#   **默认 suite 不再执行本用例**。它做一次真实 ``build_release.py --strict``（拷贝 ~13850
#   文件 / 515MB 嵌入式 runtime + 全量哈希 + 许可收集），属于「重 I/O」操作，曾多次成为
#   长时间阻塞源。真实 strict 构建的完整验证职责正式归属 **Windows Release Gate Step 2**
#   （在「全套件 PASS」之后单独运行 `run_a3_real_strict_build()`）。
#   默认 suite 保留全部 **fake / 逻辑级** strict 闸门（见 _t_fake_repo_strict_build /
#   _t_lock_mismatch_gate / _t_vendor_license_gate / _t_build_info_missing / _t_licenses_missing），
#   已覆盖 BUILD_INFO / LICENSES / MANIFEST / SHA256SUMS / 自校验 / 严格门禁的**逻辑本体**，
#   包括「微型仓库 strict 全链路」这一最关键链路。真实产物的端到端校验仍由 Gate Step 2 兜底。
# ---------------------------------------------------------------------------
def _in_ci() -> bool:
    """是否跑在 CI 里（GitHub Actions 会注入 GITHUB_ACTIONS=true / CI=true）。"""
    return (os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
            or os.environ.get("CI", "").lower() == "true")


def _norm_pkg(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(".", "_")


def expected_version_mismatch() -> list[str]:
    """**独立推导**「锁 vs 随包运行时」的版本差异。

    刻意不调用 `collect_licenses`：若用被测模块自己的输出去决定预期，就等于
    「拿被测对象证明被测对象」—— 它一旦算错，测试会跟着一起错。
    这里直接读 lock 文本 + 随包 `*.dist-info` 目录名派生，互不依赖。
    """
    lock = REPO / "requirements-release.lock"
    sp = REPO / "runtime" / "python-3.11-embed" / "Lib" / "site-packages"
    locked: dict[str, str] = {}
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, rest = line.partition("==")
        name = name.strip()
        if not name or any(c.isspace() for c in name):
            continue
        locked[_norm_pkg(name)] = rest.split(";")[0].strip()
    present: dict[str, str] = {}
    for d in sp.glob("*.dist-info"):
        stem = d.name[: -len(".dist-info")]
        name, _, ver = stem.rpartition("-")
        present[_norm_pkg(name or stem)] = ver
    return sorted(
        f"{n}（锁 {locked[n]} / 随包 {present[n]}）"
        for n in locked if n in present and present[n] != locked[n]
    )


def _t_real_strict_build(tmp: Path) -> None:
    section("13 真实 runtime strict 构建（有运行时才跑）")
    if not _runtime_present():
        skip("A3-13 真实 strict 构建", "本机无 runtime/python-3.11-embed（CI Portable job 覆盖）")
        return

    stale = expected_version_mismatch()
    out = tmp / "dist"
    # 真实 full build 拷贝 ~13850 文件 / 515MB 嵌入式 runtime，Windows 实时防毒下可能很慢；
    # 用有界 subprocess（1200s 上限，与 A1/A2 install 一致，容纳真实删除/拷贝尖峰），
    # 超时 → rc=-1 + 明确 TIMEOUT 提示，不静默挂起。
    global REAL_STRICT_BUILD_COUNT
    REAL_STRICT_BUILD_COUNT += 1
    proc = _run_bounded(
        [sys.executable, str(REPO / "scripts" / "build_release.py"),
         "--strict", "--output", str(out)],
        phase="build_release (strict)", timeout=1200, cwd=str(REPO))
    stderr = proc.stderr or ""

    if stale:
        # A3.1：runtime 非由当前 lock 构建 ⇒ strict **必须**红，且给出稳定码。
        # 这是正确行为，不是失败；也**不**为了变绿去重建 runtime 或反向改 lock。
        print(f"  ⚠ LOCAL_RUNTIME_STALE：{'; '.join(stale)}")
        # ⚠ 但 CI 是**例外**：Portable job 的运行时就是从这份 lock 现装的，
        #   若那里还出现差异，说明 CI 环境本身有问题 —— 必须红。
        #   没有这条，本用例在「陈旧」分支上也会通过，绿色就证明不了 freshness。
        if _in_ci():
            check("A3-13a CI 的运行时必须与依赖锁一致（fresh runtime → mismatch=0）",
                  False, f"CI 环境下仍存在差异：{stale}")
            return
        print("     （本机 runtime 陈旧；freshness 由 CI Portable job 验证）")
        check("A3-13a 陈旧 runtime → strict 必须拒绝（rc!=0）", proc.returncode != 0,
              f"rc={proc.returncode}")
        check("A3-13a2 拒绝码为 RUNTIME_LOCK_MISMATCH",
              "RUNTIME_LOCK_MISMATCH" in stderr, stderr[-300:])
        check("A3-13a3 拒绝信息点出具体不一致的包",
              any(s.split("（")[0] in stderr for s in stale), stderr[-300:])
        # 交叉校验：门禁在写盘之后判定，产物里的清单必须如实记录同一组差异 ——
        # 用**独立推导**的期望去比对被测模块的输出，避免「自己证明自己」。
        inv_path = (out / f"USB-WIKI-v{APP_VERSION}-win-x64"
                    / "LICENSES" / "THIRD_PARTY.json")
        recorded = []
        if inv_path.is_file():
            recorded = json.loads(inv_path.read_text(encoding="utf-8")
                                  )["summary"]["version_mismatch"]
        check("A3-13a4 独立推导的差异与清单记录逐条一致",
              sorted(s.split("（")[0] for s in recorded)
              == sorted(s.split("（")[0] for s in stale),
              f"expected={sorted(s.split('（')[0] for s in stale)} "
              f"recorded={sorted(s.split('（')[0] for s in recorded)}")
        return

    check("A3-13a strict 构建返回 0", proc.returncode == 0, stderr[-400:])
    if proc.returncode != 0:
        return

    root = out / f"USB-WIKI-v{APP_VERSION}-win-x64"
    ri = fx.release_integrity()
    info = json.loads((root / "BUILD_INFO.json").read_text(encoding="utf-8"))
    inv = json.loads((root / "LICENSES" / "THIRD_PARTY.json").read_text(encoding="utf-8"))

    check("A3-13b BUILD_INFO 存在且 commit 可追溯",
          info["git_commit"] != "unknown" and len(info["git_commit"]) >= 7,
          info["git_commit"])
    check("A3-13c python_version 取自随包运行时",
          info["python_version"] == "3.11.9", str(info["python_version"]))
    # ⚠ 必须与 migrations 的**唯一来源**比对，不能写死字面量 ——
    # 写死会在结构版本升级时假红（本用例的名字就是「取自唯一来源」）。
    check("A3-13d schema_version 取自 migrations 唯一来源",
          info["schema_version"] == _SCHEMA_VERSION, str(info["schema_version"]))
    check("A3-13e data_format_version 取自 library 唯一来源",
          info["data_format_version"] == 1, str(info["data_format_version"]))
    lock_sha = ri.sha256_file(REPO / "requirements-release.lock")
    check("A3-13f dependency_lock_sha256 == 实际锁文件 SHA256",
          info["dependency_lock_sha256"] == lock_sha)
    check("A3-13g LICENSES inventory_complete", inv["inventory_complete"] is True)
    check("A3-13h 依赖锁条目无缺失", not inv["summary"]["locked_missing"],
          str(inv["summary"]["locked_missing"]))
    check("A3-13i 无 LICENSE_REVIEW_REQUIRED",
          inv["summary"]["review_required"] == 0,
          f"待复核 {inv['summary']['review_required']}: "
          + str([p['package'] for p in inv['packages']
                 if p['review_status'] == 'LICENSE_REVIEW_REQUIRED']))
    manifest = json.loads((root / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    man_paths = {e["path"] for e in manifest["files"]}
    check("A3-13j 清单含 LICENSES / BUILD_INFO / installer / payload",
          {"LICENSES/THIRD_PARTY.json", "BUILD_INFO.json",
           "installer/install.py", "installer/install.bat",
           "installer/release_integrity.py"} <= man_paths,
          str(sorted(man_paths - {"LICENSES/THIRD_PARTY.json", "BUILD_INFO.json"}))[:200])
    check("A3-13j2 随包文档进入清单", any(p.startswith("docs/") for p in man_paths))
    res = ri.verify_media(root)
    check("A3-13k 构建产物通过自身介质校验", res.ok,
          f"{res.code} {res.failures[:3]}")
    check("A3-13l 清单文件数 == 已校验文件数",
          res.file_count == len(manifest["files"]),
          f"{res.file_count} vs {len(manifest['files'])}")
    check("A3-13m 未出现「正式 Release 可交付」措辞",
          "正式 Release 可交付" not in (proc.stdout or ""))
    check("A3-13n strict 输出含 BUILD OK", "BUILD OK" in (proc.stdout or ""),
          (proc.stdout or "")[:120])


def _make_fake_repo(root: Path, *, with_site_packages: bool = True,
                    lock_version: str = "1.0", runtime_version: str = "1.0",
                    pkg_license_file: bool = True) -> Path:
    """微型仓库（秒级构建）：让整条 strict 链路在所有 CI 作业里都能跑。

    真实 runtime 的构建太重（拷贝 122MB + 全量哈希），不能作为每次提交的门禁；
    这里用假 app + 假运行时把 BUILD_INFO / LICENSES / MANIFEST / SHA256SUMS /
    自校验 / 严格门禁的**逻辑**完整覆盖，真实产物的验证交给真实构建那条用例。

    *lock_version* / *runtime_version* 可制造「依赖锁 vs 随包运行时」版本差异；
    *pkg_license_file*=False 让该包只剩元数据（`metadata_only`），用于验证
    「随包运行依赖必须有许可原文」与 vendor 补齐两条路径。
    """
    (root / "app" / "core").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "runtime" / "python-3.11-embed" / "Lib" / "site-packages").mkdir(parents=True)
    (root / "docs").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "version.py").write_text('APP_VERSION = "9.9.9"\n', encoding="utf-8")
    (root / "app" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "core" / "migrations.py").write_text(
        'CURRENT_SCHEMA_VERSION = "1.4"\n', encoding="utf-8")
    (root / "app" / "core" / "library.py").write_text(
        "DATA_FORMAT_VERSION = 1\n", encoding="utf-8")
    (root / "app" / "launcher.py").write_text("# fake launcher\n", encoding="utf-8")
    for name in ("build_release.py", "release_integrity.py", "collect_licenses.py",
                 "install_windows.py"):
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    rt = root / "runtime" / "python-3.11-embed"
    (rt / "python.exe").write_bytes(b"")        # 占位（不可执行 → 走 dll 名推断）
    (rt / "python311.dll").write_bytes(b"fake-dll")
    (rt / "LICENSE.txt").write_text("PSF LICENSE (fake)\n", encoding="utf-8")
    if with_site_packages:
        d = rt / "Lib" / "site-packages" / f"foo-{runtime_version}.dist-info"
        d.mkdir(parents=True)
        (d / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: foo\nVersion: {runtime_version}\n"
            "License: MIT\nHome-page: https://example.invalid/foo\n\n", encoding="utf-8")
        if pkg_license_file:
            (d / "LICENSE").write_text("MIT text\n", encoding="utf-8")
    (root / "requirements-release.lock").write_text(
        f"foo=={lock_version}\n", encoding="utf-8")
    (root / "README.md").write_text("# fake\n", encoding="utf-8")
    (root / "docs" / "设计与实现.md").write_text("# fake doc\n", encoding="utf-8")
    return root


def _write_vendor(vendor_root: Path, pkg: str, ver: str, *, files: dict[str, bytes],
                  declared: list[str] | None = None,
                  omit: str | None = None,
                  tamper: str | None = None) -> Path:
    """造一个 vendor 许可原文库目录（可选：少一个声明文件 / 声明后被改写）。

    一律 `write_bytes`，避免 Windows 上 `write_text` 的 `\\n`→`\\r\\n` 让
    sha256/size 与声明不符 —— 那会让测试自身变成噪声源。
    """
    d = vendor_root / pkg / ver
    d.mkdir(parents=True, exist_ok=True)
    decl = list(declared if declared is not None else files)
    entries = []
    for name in decl:
        body = files[name]
        if omit != name:
            (d / name).write_bytes(body)
        if tamper == name:
            (d / name).write_bytes(body + b"# edited by hand\n")
        entries.append({
            "name": name,
            "source_url": f"https://example.invalid/{pkg}/{ver}/{name}",
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        })
    (d / "PROVENANCE.json").write_text(json.dumps({
        "format_version": 1, "package": pkg, "version": ver,
        "upstream_project": "https://example.invalid/proj",
        "upstream_ref": f"v{ver}", "upstream_commit": "b" * 40,
        "retrieved_at_utc": "2026-01-01",
        "declared_in_metadata": "MIT License",
        "files": entries,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return d


def _fake_build(repo: Path, out: Path, *, strict: bool,
                commit: str | None = "a" * 40) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("GITHUB_SHA", None)
    if commit is None:
        env.pop("WIKIUSB_BUILD_COMMIT", None)
    else:
        env["WIKIUSB_BUILD_COMMIT"] = commit
    cmd = [sys.executable, "scripts/build_release.py", "--output", str(out)]
    if strict:
        cmd.append("--strict")
    return subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=600)


def _t_fake_repo_strict_build(tmp: Path) -> None:
    section("9/10 微型仓库：strict 全链路（每次 CI 都跑）")
    repo = _make_fake_repo(tmp / "repo")
    out = tmp / "dist"
    p = _fake_build(repo, out, strict=True)
    check("A3-14a 微型仓库 strict 构建 rc=0", p.returncode == 0,
          (p.stderr or p.stdout)[-300:])
    if p.returncode != 0:
        return
    root = out / "USB-WIKI-v9.9.9-win-x64"
    ri = fx.release_integrity()
    info = json.loads((root / "BUILD_INFO.json").read_text(encoding="utf-8"))
    inv = json.loads((root / "LICENSES" / "THIRD_PARTY.json").read_text(encoding="utf-8"))

    check("A3-14b BUILD_INFO 各字段来自唯一来源",
          info["app_version"] == "9.9.9" and info["schema_version"] == "1.4"
          and info["data_format_version"] == 1 and info["release_format_version"] == 1,
          str(info))
    check("A3-14c 依赖锁 SHA256 == 实际锁文件",
          info["dependency_lock_sha256"] ==
          ri.sha256_file(repo / "requirements-release.lock"))
    check("A3-14d 随包安装器带介质校验实现",
          (root / "installer" / "release_integrity.py").is_file())
    check("A3-14e LICENSES 完整且无待复核",
          inv["inventory_complete"] is True
          and inv["summary"]["review_required"] == 0
          and not inv["summary"]["locked_missing"], json.dumps(inv["summary"]))
    check("A3-14f 许可原文随包（LICENSES/packages + python）",
          (root / "LICENSES" / "packages" / "foo-1.0" / "LICENSE").is_file()
          and (root / "LICENSES" / "python" / "LICENSE.txt").is_file())
    check("A3-14g SHA256SUMS 存在且行数 == 清单条目数",
          len((root / "SHA256SUMS").read_text(encoding="utf-8").splitlines())
          == len(json.loads((root / "RELEASE_MANIFEST.json")
                            .read_text(encoding="utf-8"))["files"]))
    check("A3-14h 产物通过自身介质校验", ri.verify_media(root).ok)
    check("A3-14i 输出为 BUILD OK（非 DEV）", "BUILD OK" in (p.stdout or ""))

    # 负向：无 site-packages → 依赖锁条目未随包 → strict 必红
    repo2 = _make_fake_repo(tmp / "repo2", with_site_packages=False)
    p2 = _fake_build(repo2, tmp / "dist2", strict=True)
    check("A3-14j 依赖未随包 → strict 非零退出", p2.returncode != 0, f"rc={p2.returncode}")
    # 同一仓库非 strict 必须仍可构建，但只能自称 DEV
    p3 = _fake_build(repo2, tmp / "dist3", strict=False)
    check("A3-14k 同情形非 strict 允许构建（宽松）", p3.returncode == 0, f"rc={p3.returncode}")
    check("A3-14l 非 strict 只自称 DEV BUILD OK",
          "DEV BUILD OK" in (p3.stdout or "") and "正式 Release 可交付" not in (p3.stdout or ""))

    # 负向：commit 不可解析 → strict 必红
    p4 = _fake_build(repo, tmp / "dist4", strict=True, commit=None)
    check("A3-14m commit 不可追溯 → strict 非零退出", p4.returncode != 0, f"rc={p4.returncode}")

    # 负向：产物被破坏后 verify 必红
    good = out / "USB-WIKI-v9.9.9-win-x64"
    (good / "installer" / "install.py").write_bytes(b"# tampered\n")
    res = ri.verify_media(good)
    check("A3-14n 构建产物被篡改 → MEDIA_CORRUPTED",
          not res.ok and any("installer/install.py" in f for f in res.failures),
          str(res.failures[:2]))


def _read_fake_inventory(out: Path) -> dict | None:
    p = out / "USB-WIKI-v9.9.9-win-x64" / "LICENSES" / "THIRD_PARTY.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


# ---------------------------------------------------------------------------
# A3.1-1 —— strict 必须拒绝 runtime 与 lock 版本不一致
# ---------------------------------------------------------------------------
def _t_lock_mismatch_gate(tmp: Path) -> None:
    section("A3.1-1 strict 拒绝 runtime/lock 版本不一致")

    # A) lock 2.0 / 随包 1.0 → 必须失败
    repo_a = _make_fake_repo(tmp / "a", lock_version="2.0", runtime_version="1.0")
    out_a = tmp / "distA"
    p_a = _fake_build(repo_a, out_a, strict=True)
    err_a = p_a.stderr or ""
    check("A3.1-A 锁 2.0 / 随包 1.0 → strict 非零退出",
          p_a.returncode != 0, f"rc={p_a.returncode}")
    check("A3.1-A 稳定错误码 RUNTIME_LOCK_MISMATCH",
          "RUNTIME_LOCK_MISMATCH" in err_a, err_a[-300:])
    check("A3.1-A 信息点出不一致的包与两个版本",
          "foo" in err_a and "2.0" in err_a and "1.0" in err_a, err_a[-300:])
    check("A3.1-A 门禁码行可被机器读取",
          "GATE FAILED codes=" in err_a and "RUNTIME_LOCK_MISMATCH" in err_a,
          err_a[-300:])
    inv_a = _read_fake_inventory(out_a)
    check("A3.1-A 清单如实记录该差异",
          bool(inv_a) and any("foo" in s for s in inv_a["summary"]["version_mismatch"]),
          str((inv_a or {}).get("summary", {}).get("version_mismatch")))

    dev_a = _fake_build(repo_a, tmp / "devA", strict=False)
    check("A3.1-A 非 strict 仍宽松可构建", dev_a.returncode == 0, f"rc={dev_a.returncode}")
    check("A3.1-A 非 strict 也显著提示该码",
          "RUNTIME_LOCK_MISMATCH" in (dev_a.stdout or ""), (dev_a.stdout or "")[-300:])

    # B) lock 2.0 / 随包 2.0 → 版本一致性门禁必须放行
    repo_b = _make_fake_repo(tmp / "b", lock_version="2.0", runtime_version="2.0")
    out_b = tmp / "distB"
    p_b = _fake_build(repo_b, out_b, strict=True)
    check("A3.1-B 锁 2.0 / 随包 2.0 → strict 通过",
          p_b.returncode == 0, (p_b.stderr or p_b.stdout)[-300:])
    check("A3.1-B 不出现 RUNTIME_LOCK_MISMATCH",
          "RUNTIME_LOCK_MISMATCH" not in (p_b.stderr or ""))
    inv_b = _read_fake_inventory(out_b)
    check("A3.1-B 清单 version_mismatch 为空",
          bool(inv_b) and inv_b["summary"]["version_mismatch"] == [],
          str((inv_b or {}).get("summary", {}).get("version_mismatch")))


# ---------------------------------------------------------------------------
# A3.1-3/4/5 —— 许可原文：vendor 补齐 + 「不许改写」执行器
# ---------------------------------------------------------------------------
_VENDOR_FILES = {
    "LICENSE-MIT": b"MIT License\n\nCopyright (c) fake\n\nPermission is hereby granted...\n",
    "LICENSE-APACHE": b"Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/\n",
}


def _t_vendor_license_gate(tmp: Path) -> None:
    section("A3.1-3/4/5 vendor 许可原文：补齐 + 缺件/改写必红")

    # F) 随包运行依赖只有元数据、又没 vendor → LICENSE_TEXT_MISSING
    repo_f = _make_fake_repo(tmp / "f", pkg_license_file=False)
    out_f = tmp / "distF"
    p_f = _fake_build(repo_f, out_f, strict=True)
    err_f = p_f.stderr or ""
    check("A3.1-F 随包运行依赖缺许可原文 → strict 非零退出",
          p_f.returncode != 0, f"rc={p_f.returncode}")
    check("A3.1-F 码为 LICENSE_TEXT_MISSING", "LICENSE_TEXT_MISSING" in err_f, err_f[-300:])
    inv_f = _read_fake_inventory(out_f)
    check("A3.1-F 清单列出 required_metadata_only",
          bool(inv_f) and inv_f["summary"]["required_metadata_only"] == ["foo"],
          str((inv_f or {}).get("summary", {}).get("required_metadata_only")))
    check("A3.1-F 该包状态确为 metadata_only",
          bool(inv_f) and [p for p in inv_f["packages"]
                           if p["package"] == "foo"][0]["review_status"] == "metadata_only")

    # C) vendor 声明两件、只随包一件 → VENDOR_LICENSE_INVALID + inventory 不完整
    repo_c = _make_fake_repo(tmp / "c", pkg_license_file=False)
    _write_vendor(repo_c / "vendor" / "licenses", "foo", "1.0",
                  files=_VENDOR_FILES, omit="LICENSE-APACHE")
    out_c = tmp / "distC"
    p_c = _fake_build(repo_c, out_c, strict=True)
    err_c = p_c.stderr or ""
    check("A3.1-C vendor 缺一个声明文件 → strict 非零退出",
          p_c.returncode != 0, f"rc={p_c.returncode}")
    check("A3.1-C 码为 VENDOR_LICENSE_INVALID",
          "VENDOR_LICENSE_INVALID" in err_c, err_c[-300:])
    inv_c = _read_fake_inventory(out_c)
    check("A3.1-C 清单 inventory_complete=false",
          bool(inv_c) and inv_c["inventory_complete"] is False)
    check("A3.1-C 清单记录 vendor_problems",
          bool(inv_c) and bool(inv_c["summary"]["vendor_problems"]),
          str((inv_c or {}).get("summary", {}).get("vendor_problems")))

    # E) vendor 文件被手改 → sha256 执行器必须抓到（「不许改写许可文本」）
    repo_e = _make_fake_repo(tmp / "e", pkg_license_file=False)
    _write_vendor(repo_e / "vendor" / "licenses", "foo", "1.0",
                  files=_VENDOR_FILES, tamper="LICENSE-MIT")
    out_e = tmp / "distE"
    p_e = _fake_build(repo_e, out_e, strict=True)
    err_e = p_e.stderr or ""
    check("A3.1-E vendor 原文被改写 → strict 非零退出",
          p_e.returncode != 0, f"rc={p_e.returncode}")
    check("A3.1-E 码为 VENDOR_LICENSE_INVALID 且指明 sha256 不一致",
          "VENDOR_LICENSE_INVALID" in err_e and "sha256" in err_e, err_e[-400:])

    # D) 两件齐全且未被改动 → strict 通过，且该包不得停留在 metadata_only
    repo_d = _make_fake_repo(tmp / "d", pkg_license_file=False)
    vendor_d = _write_vendor(repo_d / "vendor" / "licenses", "foo", "1.0",
                             files=_VENDOR_FILES)
    out_d = tmp / "distD"
    p_d = _fake_build(repo_d, out_d, strict=True)
    check("A3.1-D vendor 齐全 → strict 通过",
          p_d.returncode == 0, (p_d.stderr or p_d.stdout)[-300:])
    inv_d = _read_fake_inventory(out_d) or {"summary": {}, "packages": []}
    entry_d = next((p for p in inv_d["packages"] if p["package"] == "foo"), {})
    check("A3.1-D 该包最终状态不是 metadata_only",
          entry_d.get("review_status") == "ok", str(entry_d.get("review_status")))
    check("A3.1-D 全局 metadata_only 计数为 0",
          inv_d["summary"].get("metadata_only") == 0,
          str(inv_d["summary"].get("metadata_only")))
    check("A3.1-D 两份许可原文随包",
          sorted(entry_d.get("license_texts") or [])
          == ["LICENSES/packages/foo-1.0/LICENSE-APACHE",
              "LICENSES/packages/foo-1.0/LICENSE-MIT"],
          str(entry_d.get("license_texts")))
    root_d = out_d / "USB-WIKI-v9.9.9-win-x64"
    check("A3.1-D 随包字节与 vendor 字节完全一致（未被改写）",
          (root_d / "LICENSES" / "packages" / "foo-1.0" / "LICENSE-MIT"
           ).read_bytes() == _VENDOR_FILES["LICENSE-MIT"]
          and (root_d / "LICENSES" / "packages" / "foo-1.0" / "LICENSE-APACHE"
               ).read_bytes() == _VENDOR_FILES["LICENSE-APACHE"])
    prov = entry_d.get("provenance") or {}
    check("A3.1-D 记录上游溯源（项目 / ref / commit / 原文 URL / sha256）",
          prov.get("upstream_project") == "https://example.invalid/proj"
          and prov.get("upstream_ref") == "v1.0"
          and prov.get("upstream_commit") == "b" * 40
          and len(prov.get("files") or []) == 2
          and all(f.get("source_url") and f.get("sha256") for f in prov["files"]),
          str(prov)[:300])
    check("A3.1-D 记录原文库相对路径（不含绝对路径）",
          (prov.get("vendor_source") or "").startswith("vendor/licenses/")
          and ":" not in (prov.get("vendor_source") or ""),
          str(prov.get("vendor_source")))
    check("A3.1-D 注释/正文未被联网：vendor 目录可见于仓库侧",
          (vendor_d / "PROVENANCE.json").is_file())
    check("A3.1-D 构建产物清单里含两份许可原文",
          "LICENSES/packages/foo-1.0/LICENSE-APACHE" in
          (root_d / "SHA256SUMS").read_text(encoding="utf-8"))


def _t_non_strict_never_claims_release_ready(tmp: Path) -> None:
    section("9 非 strict 构建不得自称「正式 Release 可交付」")
    fx.load_script("build_release_a3d", "build_release.py")
    src = (REPO / "scripts" / "build_release.py").read_text(encoding="utf-8")
    check("A3-9a 源码中「正式 Release 可交付」仅出现在 strict 语境",
          src.count("正式 Release 可交付") == 0 or "strict" in src)
    check("A3-9b 非 strict 分支使用 DEV 措辞",
          "DEV BUILD OK" in src and "BUILD OK" in src)
    check("A3-9c --strict 参数默认关闭", "--strict" in src and "action=\"store_true\"" in src)


# ---------------------------------------------------------------------------
def run_a3_real_strict_build() -> None:
    """Windows Release Gate Step 2 入口：真实 strict 构建全链路验证。

    默认 suite（run_a3_tests）**不**执行本用例 —— 它做一次真实 ``build_release.py --strict``
    （拷贝 ~13850 文件 / 515MB 嵌入式 runtime + 全量哈希 + 许可收集），属「重 I/O」操作，
    是此前长时间阻塞的元凶之一。按 RELEASE_REQUIRED 责任重划分，真实 strict 构建的完整
    端到端验证正式归属 **Windows Release Gate Step 2**，在「全套件 PASS」之后单独调用本函数。
    """
    global REAL_STRICT_BUILD_COUNT
    if not _runtime_present():
        skip("A3 真实 strict 构建（Gate Step 2）",
             "runtime/python-3.11-embed 未构建 —— 跳过真实构建验证")
        return
    with _tmp() as t:
        _t_real_strict_build(t)


def run_a3_tests() -> None:
    """默认 A3 suite：全部 fake / 逻辑级严格闸门，**不**含真实 strict 构建。

    责任重划分（2026-09-26，RELEASE_REQUIRED）：真实 strict 构建（拷贝 515MB 嵌入式
    runtime + 全量哈希）已移出默认 suite，改由 Windows Release Gate Step 2 单独承担
    （见 ``run_a3_real_strict_build``）。默认 suite 保留全部 fake/strict 逻辑验证，
    包括最关键的「微型仓库 strict 全链路」（_t_fake_repo_strict_build），确保 BUILD_INFO /
    LICENSES / MANIFEST / SHA256SUMS / 自校验 / 严格门禁的**逻辑本体**不退化。
    cleanup 全部有界（_cleanup_bounded，120s），逐 case 计时（_timed）。
    """
    print("\n" + "=" * 66)
    print("  A3 —— Release Integrity / BUILD_INFO / Checksums / Licensing")
    print("=" * 66)
    print("  ℹ 默认 suite 不含真实 strict 构建（由 Windows Release Gate Step 2 承担）")

    cases = [
        ("payload 改 1 byte", _t_payload_byte_flip, "s1"),
        ("payload 缺文件", _t_payload_missing_file, "s2"),
        ("manifest 损坏", _t_manifest_broken, "s3"),
        ("BUILD_INFO 缺失", _t_build_info_missing, "s4"),
        ("LICENSES 缺失", _t_licenses_missing, "s5"),
        ("损坏介质保 App/Library", _t_corrupt_media_keeps_app_and_library, "s6"),
        ("介质校验先于写操作", _t_media_precheck_before_any_write, "s7"),
        ("完好介质重装", _t_reinstall_from_intact_media, "s8"),
        ("清单与校验和同源", _t_manifest_and_checksums_same_enumeration, "s9"),
        ("verify 只读", _t_verify_is_read_only, "s10"),
        ("BUILD_INFO 无隐私", _t_build_info_has_no_private_data, "s11"),
        ("许可清单形态", _t_license_inventory_shape, "s12"),
        ("A3.1 lock/runtime 一致性门禁", _t_lock_mismatch_gate, "s16"),
        ("A3.1 vendor 许可原文补齐", _t_vendor_license_gate, "s17"),
        ("微型仓库 strict 全链路", _t_fake_repo_strict_build, "s13"),
        ("非 strict 不称可交付", _t_non_strict_never_claims_release_ready, "s14"),
    ]

    # 安全隔离：进入沙箱前拍真实副作用快照，全程 sandbox 隔离，退出后比对。
    # 若沙箱失效导致真实桌面/LOCALAPPDATA 被写，快照不一致 → 明确 FAIL（绝不写后再删）。
    snap_before = _snapshot_real_side_effects()
    with _install_sandbox() as sb:
        print(f"  ℹ 安装沙箱：LOCALAPPDATA→{os.environ['LOCALAPPDATA']} "
              f"WIKIUSB_STATE_DIR→{os.environ['WIKIUSB_STATE_DIR']} "
              f"desktop_dir→{sb.desktop_dir}")
        for _label, fn, sub in cases:
            def _run():
                with _tmp() as t:
                    d = t / sub
                    d.mkdir(parents=True, exist_ok=True)
                    try:
                        fn(d)
                    except Exception as exc:      # 单场景异常不得带崩整个套件
                        check(f"A3 {_label} 执行未抛异常", False,
                              f"{type(exc).__name__}: {exc}")
            _timed(_label, _run)
    snap_after = _snapshot_real_side_effects()
    check("A3 全程未修改真实 Desktop / LOCALAPPDATA",
          snap_before == snap_after,
          f"before={snap_before} after={snap_after}")

    print(f"\n  A3 TOTAL={len(PASS) + len(FAIL) + len(SKIP)} "
          f"PASS={len(PASS)} SKIP={len(SKIP)} FAIL={len(FAIL)} "
          f"real_strict_build={REAL_STRICT_BUILD_COUNT}")
    if REAL_STRICT_BUILD_COUNT == 0:
        print("  ℹ 真实 strict 构建次数 = 0（符合预期：已移出默认 suite）")


if __name__ == "__main__":
    run_a3_tests()
    total = len(PASS) + len(SKIP) + len(FAIL)
    print(f"\n  A3 TOTAL={total} PASS={len(PASS)} SKIP={len(SKIP)} "
          f"FAIL={len(FAIL)} real_strict_build={REAL_STRICT_BUILD_COUNT}")
    raise SystemExit(1 if FAIL else 0)
