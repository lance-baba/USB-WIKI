"""发布完整性校验器（``setup_runtime_windows.py --verify``）自身的回归测试。

为什么必须测它：一个**永远返回「可交付」的校验器**比没有校验器更危险 ——
它会给人虚假的安全感。所以这里全部用**负向构造**：现场搭一个假发布包，
故意抽掉某一样东西，断言校验器必须报失败并指名道姓。

素材全部代码现场合成到临时目录，不碰真实仓库结构。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setup_runtime_windows as S  # noqa: E402

_ROOT_FILES = S.VERIFY_ROOT_FILES
_LAUNCHERS = S.VERIFY_LAUNCHERS
_DIRS = S.VERIFY_DIRS
_APP_PY = S.VERIFY_APP_PY
_API = S.VERIFY_API_MODULES
_WEB = S.VERIFY_WEB_ASSETS


def _build(root: Path, *, with_api: bool = True, web_cdn: bool = False,
           with_runtime: bool = False, with_docs: int = 4) -> None:
    """构造一个「看起来合格」的假发布包骨架。"""
    for rel in _ROOT_FILES + _LAUNCHERS + _APP_PY:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    for d in _DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    if with_api:
        for rel in _API:
            (root / rel).write_text("x", encoding="utf-8")
    for rel in _WEB:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # 只有 html 注入外链，用于验证「离线自包含」会被拦下
        p.write_text('<script src="https://cdn.example.com/x.js"></script>'
                     if (web_cdn and rel.endswith(".html")) else "x",
                     encoding="utf-8")
    for i in range(with_docs):
        (root / "docs" / f"d{i}.md").write_text("x", encoding="utf-8")
    if with_runtime:
        d = root / "runtime" / "python-3.11-embed"
        (d / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
        (d / "python.exe").write_bytes(b"x")
        (d / "python311._pth").write_text("import site", encoding="utf-8")


def _run(root: Path, *, as_json: bool = False) -> tuple[int, str]:
    """把真仓库根临时换成 *root*，跑一次 verify 拿回 (退出码, 输出)。"""
    saved = (S.BASE, S.EMBED_DIR, S.EMBED_PY)
    S.BASE = root
    S.EMBED_DIR = root / "runtime" / "python-3.11-embed"
    S.EMBED_PY = S.EMBED_DIR / "python.exe"
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.verify(as_json=as_json)
        return rc, buf.getvalue()
    finally:
        S.BASE, S.EMBED_DIR, S.EMBED_PY = saved


def _case(**kw) -> tuple[int, str]:
    tmp = Path(tempfile.mkdtemp(prefix="wikiusb_release_"))
    try:
        root = tmp / "release"
        _build(root, **kw)
        return _run(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(check) -> None:
    print("\n── 发布完整性校验器 --verify " + "─" * 42)

    # ---------- 基线：合格包必须放行 ----------
    # 注意：不断言「无警告」—— 运行测试本身会产生 __pycache__，
    # 那是 --clean 的职责（警告项），不应阻断 deliverable。
    rc, out = _case(with_runtime=True)
    check("合格发布包返回「可交付」（退出码 0）", rc == 0, out[-300:])
    rc, out = _case()  # 有意不给 runtime —— 非 Windows 平台属正常
    if os.name == "nt":
        check("Windows 平台缺运行时必须拦下", rc == 1 and "python.exe" in out, out[-300:])
    else:
        check("非 Windows 缺运行时按宿主 Python 放行", rc == 0, out[-300:])

    # ---------- 负向：缺 API 域模块（server 拆分后缺一个即整组 API 全挂）----------
    rc, out = _case(with_api=False, with_runtime=True)
    check("缺 app/api 域模块被拦截", rc == 1 and "app/api/system.py" in out, out[-300:])

    # ---------- 负向：前端残留 CDN 外链（Portable 的底线）----------
    rc, out = _case(web_cdn=True, with_runtime=True)
    check("前端残留 CDN 外链被拦截", rc == 1 and "外链引用" in out, out[-300:])

    # ---------- 负向：缺前端拆分产物 ----------
    tmp = Path(tempfile.mkdtemp(prefix="wikiusb_release_"))
    try:
        root = tmp / "release"
        _build(root, with_runtime=True)
        (root / "app" / "web" / "app.js").unlink()
        rc, out = _run(root)
        check("缺前端拆分产物 app.js 被拦截", rc == 1 and "app/web/app.js" in out, out[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- JSON 模式：结构必须可被 CI 消费 ----------
    tmp = Path(tempfile.mkdtemp(prefix="wikiusb_release_"))
    try:
        root = tmp / "release"
        _build(root, with_runtime=True)
        rc, out = _run(root, as_json=True)
        try:
            doc = json.loads(out)
            ok = (isinstance(doc.get("deliverable"), bool)
                  and isinstance(doc.get("checks"), list)
                  and isinstance(doc.get("missing"), list)
                  and len(doc["checks"]) >= 10)
            check("--verify --json 输出合法且字段齐备", ok, out[:200])
        except json.JSONDecodeError as exc:
            check("--verify --json 输出合法且字段齐备", False, f"{exc}: {out[:200]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 真实仓库自检：当前源码树本身应当可交付 ----------
    rc, out = _run(S.BASE, as_json=True)
    try:
        doc = json.loads(out)
        check("当前仓库自检 deliverable=True", rc == 0 and doc["deliverable"] is True,
              str(doc.get("missing"))[:300])
    except json.JSONDecodeError:
        check("当前仓库自检 deliverable=True", False, out[:200])


if __name__ == "__main__":
    failed = 0

    def _check(name: str, cond: bool, detail: str = "") -> bool:
        global failed
        print(("  ✅ " if cond else "  ❌ ") + name + (f"  <- {detail}" if detail and not cond else ""))
        if not cond:
            failed += 1
        return cond

    run(_check)
    print(f"\n{'❌ 有失败' if failed else '✅ 全部通过'}（{failed} 项失败）")
    raise SystemExit(1 if failed else 0)
