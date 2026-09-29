#!/usr/bin/env python3
"""离线机器绑定授权层 —— 单元测试（需求 13 全部场景，**全离线**）。

覆盖：
  1. 正确激活通过
  2. 错误设备码失败              （签名有效，但 device_hash 与当前机器不符）
  3. 篡改激活码失败              （改字段 / 换签名 → Ed25519 验签不通过）
  4. 过期激活码失败
  5. 复制到模拟新设备失败        （文案必须逐字为「此激活码不属于当前设备。」）
  6. 公钥验证通过                （正解通过；换把钥匙验签必须失败）
  7. 发布物不包含私钥            （app/ 与 dist/ 扫描；build 脚本不含 tools/）

运行（**必须用仓库自带运行时**，它含 cryptography）：
    runtime\\python-3.11-embed\\python.exe tests\\test_license_offline.py

隔离声明：
  * ``WIKIUSB_STATE_DIR`` 指向临时目录 → 绝不写真实 ``%LOCALAPPDATA%`` 或桌面；
  * ``WIKIUSB_DEVICE_SEED`` 固定 → 用确定性「假设备」而非真实硬件。

本文件**不接入** ``tests/test_suite.py``（不改动既有 Release Gate 行为）；独立运行。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "license_generator"))

# ── 隔离保险丝（必须在 import app.core.license 之前设置）────────────────────
_TMP_STATE = tempfile.mkdtemp(prefix="usb-wiki-lic-test-")
os.environ["WIKIUSB_STATE_DIR"] = _TMP_STATE
os.environ["WIKIUSB_DEVICE_SEED"] = "test-device-A"
os.environ.setdefault("WIKIUSB_TEST_MODE", "1")

try:
    import cryptography  # noqa: F401
except ImportError:  # pragma: no cover
    print("此测试需要 cryptography。请用仓库自带运行时运行：")
    print("  runtime\\python-3.11-embed\\python.exe tests\\test_license_offline.py")
    raise SystemExit(2)

from cryptography.hazmat.primitives import serialization            # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from app.core import license as lic                                 # noqa: E402
from app.core import license_pubkey as pk                           # noqa: E402
import license_gen                                                  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name if cond else f"{name} :: {detail}")
    print(("  [OK] " if cond else "  [FAIL] ") + name + ("" if cond else f"  [{detail}]"))
    return cond


def section(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


def set_device(seed: str) -> None:
    os.environ["WIKIUSB_DEVICE_SEED"] = seed


def ephemeral_key() -> tuple[Ed25519PrivateKey, str]:
    """临时 Ed25519 密钥对 → (私钥, 公钥 base64url)。测试自带密钥，不依赖生产私钥。"""
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, lic.b64url_encode(raw)


def gen(priv, *, device_hash: str, customer_id: str = "CUST", features=None,
        issued_at=None, expires_at=None) -> str:
    code, _ = license_gen.generate(
        priv, product="USB-WIKI", device_code=device_hash, customer_id=customer_id,
        features=features or [], issued_at=issued_at, expires_at=expires_at)
    return code


def main() -> int:
    priv, pub_b64 = ephemeral_key()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 真实用户授权文件路径（用于「无污染」断言；本测试不应触碰它）
    real_local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    real_lic = Path(real_local) / "USB-WIKI" / "license.json"
    real_existed_before = real_lic.exists()

    lf = lic.license_file()

    # ---------------------------------------------------------------- 1
    section("1. 正确激活通过")
    set_device("dev-A")
    dh_a = lic.device_hash()
    code_ok = gen(priv, device_hash=dh_a, customer_id="CUST-0001",
                  features=["pro"], expires_at=future)
    res = lic.verify_code(code_ok, public_key_b64=pub_b64)
    check("验签通过（OK）", res["ok"] and res["status"] == lic.ST_OK, res["status"])
    check("载荷字段完整", all(k in (res["payload"] or {})
                              for k in lic.REQUIRED_FIELDS), str(res["payload"]))
    act = lic.activate(code_ok, public_key_b64=pub_b64)
    check("激活成功并落盘", act["ok"] and lf.is_file(), str(act))
    st = lic.status(public_key_b64=pub_b64)
    check("状态=已激活", st["activated"] and st["status"] == lic.ST_OK, st["status"])
    check("客户信息回读一致", st["customer_id"] == "CUST-0001", str(st))

    # ---------------------------------------------------------------- 2
    section("2. 错误设备码失败")
    set_device("dev-X")
    code_x = gen(priv, device_hash=lic.device_hash(), customer_id="CUST-X")
    set_device("dev-Y")                     # 同一枚码，换到另一台机器上校验
    res = lic.verify_code(code_x, public_key_b64=pub_b64)
    check("设备不符（DEVICE_MISMATCH）",
          res["status"] == lic.ST_DEVICE_MISMATCH, res["status"])
    lic.clear_activation()                  # 清空后尝试「在错误设备上激活」
    bad = lic.activate(code_x, public_key_b64=pub_b64)
    check("错误设备激活被拒", (not bad["ok"]) and bad["status"] == lic.ST_DEVICE_MISMATCH,
          bad["status"])
    check("激活失败不落盘（需求 12）", not lf.is_file(), "不应创建 license.json")

    # ---------------------------------------------------------------- 3
    section("3. 篡改激活码失败")
    set_device("dev-A")
    good = gen(priv, device_hash=lic.device_hash(), customer_id="CUST-0001")
    payload, sig = lic.decode_code(good)
    payload["customer_id"] = "HACKED"       # 改字段，但**沿用旧签名**
    tampered = lic.encode_code(payload, sig)
    res = lic.verify_code(tampered, public_key_b64=pub_b64,
                          device_hash_value=lic.device_hash())
    check("字段篡改 → 签名无效（BAD_SIGNATURE）",
          res["status"] == lic.ST_BAD_SIGNATURE, res["status"])
    res2 = lic.verify_code(good[:-4] + "ZZZZ", public_key_b64=pub_b64)
    check("码尾破坏 → 拒绝（非 OK）", not res2["ok"], res2["status"])

    # ---------------------------------------------------------------- 4
    section("4. 过期激活码失败")
    set_device("dev-A")
    code_exp = gen(priv, device_hash=lic.device_hash(), expires_at=past)
    res = lic.verify_code(code_exp, public_key_b64=pub_b64)
    check("已过期（EXPIRED）", res["status"] == lic.ST_EXPIRED, res["status"])
    res_ok = lic.verify_code(
        gen(priv, device_hash=lic.device_hash(), expires_at=future), public_key_b64=pub_b64)
    check("未过期仍有效（OK）", res_ok["status"] == lic.ST_OK, res_ok["status"])
    res_perp = lic.verify_code(
        gen(priv, device_hash=lic.device_hash(), expires_at=None), public_key_b64=pub_b64)
    check("永久授权有效（OK）", res_perp["status"] == lic.ST_OK, res_perp["status"])

    # ---------------------------------------------------------------- 5
    section("5. 复制到模拟新设备失败")
    set_device("dev-A")
    lic.clear_activation()
    code_move = gen(priv, device_hash=lic.device_hash(), customer_id="CUST-MOVE")
    act = lic.activate(code_move, public_key_b64=pub_b64)
    check("机器 A 上激活成功", act["ok"] and lf.is_file(), str(act))
    # 模拟「把整个目录（含 license.json）复制到另一台电脑」：状态目录不变，设备变了
    set_device("dev-B")
    st = lic.status(public_key_b64=pub_b64)
    check("新设备上激活失效", not st["activated"], str(st))
    check("提示文案逐字一致",
          st["message"] == "此激活码不属于当前设备。", st["message"])
    check("状态码=DEVICE_MISMATCH", st["status"] == lic.ST_DEVICE_MISMATCH, st["status"])

    # ---------------------------------------------------------------- 6
    section("6. 公钥验证通过")
    raw_pub = pk.public_key_bytes()
    check("内置公钥为 32 字节", len(raw_pub) == 32, f"len={len(raw_pub)}")
    check("内置公钥指纹自洽", pk.verify_key_id() == pk.KEY_ID, pk.verify_key_id())
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(raw_pub)
        check("内置公钥可被加载为 Ed25519", True)
    except Exception as exc:  # noqa: BLE001
        check("内置公钥可被加载为 Ed25519", False, str(exc))
    set_device("dev-A")
    code_v = gen(priv, device_hash=lic.device_hash())
    good_res = lic.verify_code(code_v, public_key_b64=pub_b64)
    check("正确公钥验签通过", good_res["status"] == lic.ST_OK, good_res["status"])
    # 用**内置（生产）公钥**验同一枚码：不是它签的 → 必须失败
    wrong = lic.verify_code(code_v)
    check("用错公钥验签失败", wrong["status"] == lic.ST_BAD_SIGNATURE, wrong["status"])

    # ---------------------------------------------------------------- 无污染
    section("激活失败不污染真实目录")
    check("真实 LOCALAPPDATA 授权文件未被创建/改动",
          real_lic.exists() == real_existed_before,
          f"before={real_existed_before} after={real_lic.exists()}")
    check("状态目录被重定向到临时区",
          str(lic.state_dir()).startswith(_TMP_STATE), str(lic.state_dir()))

    # ---------------------------------------------------------------- 7
    section("7. 发布物不包含私钥")
    priv_markers = (b"-----BEGIN PRIVATE KEY-----",
                    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
                    b"-----BEGIN OPENSSH PRIVATE KEY-----")

    def _has_marker(p: Path) -> bool:
        try:
            data = p.read_bytes()
        except OSError:
            return False
        return any(m in data for m in priv_markers)

    # (a) app/ 是随包客户端代码 —— 绝不能含私钥
    app_hits = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and _has_marker(p)]
    check("app/ 内无私钥", not app_hits, "; ".join(app_hits[:3]))

    # (b) 客户端只内置公钥：license_pubkey.py 不含私钥标记，且确有公钥常量
    lpk = ROOT / "app" / "core" / "license_pubkey.py"
    check("license_pubkey.py 只有公钥（无私钥标记）", not _has_marker(lpk))
    check("license_pubkey.py 含 PUBLIC_KEY_B64", "PUBLIC_KEY_B64" in lpk.read_text("utf-8"))

    # (c) 真正的「私钥文件」= 带 .pem/.key 后缀**且内容含私钥标记**。
    #     ⚠ 不能只看后缀：certifi 的 cacert.pem 是**公共 CA 证书包**（公钥性质），
    #       按后缀一刀切会误报。判据必须是「内容里真有 PRIVATE KEY 头」。
    key_files = [p for p in ROOT.rglob("*")
                 if p.is_file() and p.suffix in (".pem", ".key")
                 and "__pycache__" not in p.parts and _has_marker(p)]
    bad_loc = [str(p.relative_to(ROOT)) for p in key_files
               if p.relative_to(ROOT).parts[:2] != ("tools", "license_generator")]
    check("私钥只出现在 tools/license_generator/", not bad_loc, "; ".join(bad_loc[:3]))

    # (d) 构建脚本绝不把 tools/ 拷进发布物
    build_src = (ROOT / "scripts" / "build_release.py").read_text("utf-8")
    check('build_release.py 不引用 tools/', 'REPO / "tools"' not in build_src
          and "REPO/'tools'" not in build_src)

    # (e) 已有的发布构建产物（若在）不得含私钥（按内容标记判，不看后缀）
    dist = ROOT / "dist"
    priv_name = (ROOT / "tools" / "license_generator" / "keys" / "private_ed25519.pem").name
    if dist.is_dir():
        leaked = []
        for p in dist.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(dist)
            if p.name == priv_name:                       # 私钥文件同名即泄漏
                leaked.append(str(rel))
                continue
            # 只读「客户端代码 + 安装器 + 小文本」候选，避免扫数百 MB 的 runtime 二进制
            if ("payload" in rel.parts and "app" in rel.parts) or \
               ("installer" in rel.parts) or p.suffix in (".json", ".txt", ".md"):
                if _has_marker(p):
                    leaked.append(str(rel))
        check("发布产物 dist/ 内无私钥", not leaked, "; ".join(leaked[:3]))
    else:
        SKIP.append("dist/ 不存在，跳过发布产物扫描")
        print("  [SKIP] dist/ 不存在，跳过发布产物扫描")

    # ---------------------------------------------------------------- 收尾
    lic.clear_activation()
    try:
        shutil.rmtree(_TMP_STATE, ignore_errors=True)
    except OSError:
        pass

    print("\n" + "=" * 64)
    print(f"TOTAL={len(PASS) + len(FAIL) + len(SKIP)}  PASS={len(PASS)}  "
          f"FAIL={len(FAIL)}  SKIP={len(SKIP)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
