"""Test Harness Safety Fuse 回归（Case A~E）—— 事故后新增的**安全门**测试。

真实事故（2026-09-21）：`reset_workspace()` 在真实开发 Library 上运行，
删除了 `notes/*.md`、`snapshots/*`、`cache.db`（均在 .gitignore 内，git 无法恢复）。

本文件只验证一件事：**破坏性测试永远碰不到真实 Library**。
Case A/B 断言「真实路径被拒 + 文件仍在」，Case C 断言「隔离条件齐备才放行」，
Case D/E 断言「缺 sentinel / 缺 TEST_MODE 一律拒绝」。
"""
from __future__ import annotations

import os
from pathlib import Path

from tests import test_env


def run(ctx, check, section, skip) -> None:      # noqa: ARG001
    section("Test Harness Safety Fuse（破坏性测试只能碰隔离 Library）")

    saved = {k: os.environ.get(k) for k in ("WIKIUSB_LIBRARY", "WIKIUSB_TEST_MODE")}

    def _restore_env():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---------------------------------------------------------------- Case A
    # Library = repo/data（真实开发库）→ 必须拒绝，且**真实文件不被触碰**
    # 注意：本用例不往真实库写任何东西，只用已存在的真实笔记做「仍存在」断言。
    real_notes = sorted((test_env.REPO_DATA / "notes").glob("*.md"))
    probe = real_notes[0] if real_notes else None
    os.environ["WIKIUSB_TEST_MODE"] = "1"                 # 即便显式开了 TEST_MODE
    os.environ["WIKIUSB_LIBRARY"] = str(test_env.REPO_DATA)
    refused = False
    try:
        test_env.assert_test_library_safe()
    except RuntimeError as exc:
        refused = test_env.REFUSE_MSG in str(exc)
    check("Case A repo/data 被硬拒绝（即使 TEST_MODE=1）", refused)
    check("Case A 拒绝后真实库笔记仍存在（未被触碰）",
          probe is not None and probe.exists(), f"notes={len(real_notes)}")

    # ---------------------------------------------------------------- Case B
    # Library = 用户默认 Documents/USB-WIKI-Data → 同样拒绝
    docs_lib = (Path.home() / "Documents" / "USB-WIKI-Data").resolve()
    os.environ["WIKIUSB_LIBRARY"] = str(docs_lib)
    refused_b = False
    try:
        test_env.assert_test_library_safe()
    except RuntimeError as exc:
        refused_b = test_env.REFUSE_MSG in str(exc)
    check("Case B 默认生产 Library（Documents/USB-WIKI-Data）被硬拒绝", refused_b)

    # ---------------------------------------------------------------- Case C
    # TEMP/usb-wiki-test-xxx + TEST_MODE=1 + sentinel → 允许
    lib = test_env.create_isolated_library(prefix="usb-wiki-test-safety-")
    (lib / "notes" / "probe.md").write_text("---\ntitle: p\n---\n\nx\n", encoding="utf-8")
    os.environ["WIKIUSB_LIBRARY"] = str(lib)
    os.environ["WIKIUSB_TEST_MODE"] = "1"
    allowed = test_env.assert_test_library_safe() == lib.resolve()
    check("Case C 三重条件齐备（TEMP + sentinel + TEST_MODE）才允许", allowed)
    check("Case C 隔离库位于系统 TEMP 下",
          test_env._is_under(lib, test_env.SYSTEM_TEMP))
    (lib / "notes" / "probe.md").unlink()                  # 允许的破坏性操作
    check("Case C 隔离库内允许删除", not (lib / "notes" / "probe.md").exists())

    # ---------------------------------------------------------------- Case D
    # 临时路径但缺 sentinel → 拒绝
    (lib / test_env.SENTINEL_NAME).unlink()
    refused_d = False
    try:
        test_env.assert_test_library_safe()
    except RuntimeError as exc:
        refused_d = "sentinel" in str(exc)
    check("Case D 临时路径但缺 sentinel → 拒绝", refused_d)

    # ---------------------------------------------------------------- Case E
    # sentinel 有，但 TEST_MODE != 1 → 拒绝
    (lib / test_env.SENTINEL_NAME).write_text("sentinel\n", encoding="utf-8")
    os.environ.pop("WIKIUSB_TEST_MODE", None)
    refused_e = False
    try:
        test_env.assert_test_library_safe()
    except RuntimeError as exc:
        refused_e = "WIKIUSB_TEST_MODE" in str(exc)
    check("Case E 缺 WIKIUSB_TEST_MODE=1 → 拒绝", refused_e)

    # ---------------------------------------------------------------- 附加
    # 禁止以「路径名里有 test」当唯一保护：TEMP 下名为 test* 的目录，缺 sentinel 也必须拒绝
    import shutil as _sh
    import tempfile as _tf

    fake = Path(_tf.mkdtemp(prefix="test-")).resolve()
    os.environ["WIKIUSB_LIBRARY"] = str(fake)
    os.environ["WIKIUSB_TEST_MODE"] = "1"
    refused_f = False
    try:
        test_env.assert_test_library_safe()
    except RuntimeError:
        refused_f = True
    check("附加：仅路径含 test 不足以放行（必须有 sentinel）", refused_f)
    _sh.rmtree(fake, ignore_errors=True)

    os.environ["WIKIUSB_TEST_MODE"] = "1"
    os.environ["WIKIUSB_LIBRARY"] = str(lib)
    check("附加：恢复正常条件后再次放行", test_env.assert_test_library_safe() == lib.resolve())
    check("附加：suite 入口已把 Library 指向 TEMP（隔离生效）",
          test_env._is_under(Path(os.environ["WIKIUSB_LIBRARY"]),
                             test_env.SYSTEM_TEMP))

    _restore_env()
