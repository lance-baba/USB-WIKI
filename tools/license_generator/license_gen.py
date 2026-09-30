#!/usr/bin/env python3
"""授权方激活码生成器（**独立工具，离线运行，私钥不出本机**）。

## 工作流

    客户机器                     授权方（你）
    ────────                     ────────────
    首启 → 生成「设备码」
             │  （把设备码发给你）
             └──────────────►   license_gen.py --device-code … --customer-id …
                                 │  （把生成的激活码发回客户）
             ◄──────────────┘
    输入激活码 → 本机验签通过 → 激活完成（以后**完全离线**）

（真实场景里「发送设备码 / 回发激活码」是指人工转达，本工具不联网、无 API。）

## 举例

    标准做法（永久授权，激活后长期可用）：
      python license_gen.py --device-code "C3285E2A-3A90-A661-…" --customer-id CUST-0001

    带功能位（可选）：
      python license_gen.py --device-code "C3285E2A-…" --customer-id CUST-0002 --features pro,sync

    自检（生成临时密钥对→签发→用客户端逻辑验签，证明通路一致）：
      python license_gen.py --self-test

    ⚠ 到期授权（`--expires`）属**进阶选项，产品默认不使用** —— 需要时才加。

用仓库自带运行时跑最省事（已含 cryptography）：
    runtime\\python-3.11-embed\\python.exe tools\\license_generator\\license_gen.py <参数>

## 与客户端的一致性

激活码的**格式与规范化**由 ``app.core.license``（客户端模块）唯一定义，
本工具 import 它 —— 杜绝「签发端/校验端两套实现漂移」。签名能力只在本工具里。
"""
from __future__ import annotations

import argparse
import base64
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]                       # tools/license_generator → 仓库根
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.core import license as license_mod          # noqa: E402
from app.core import license_pubkey as pubkey_mod    # noqa: E402

DEFAULT_PRIVATE = HERE / "keys" / "private_ed25519.pem"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_private_key(path: Path):
    """从 PEM 读 Ed25519 私钥。"""
    from cryptography.hazmat.primitives import serialization
    if not path.is_file():
        raise SystemExit(
            f"[license_gen] 找不到私钥：{path}\n"
            "  请先运行 genkey.py 生成密钥对，或显式指定 --private。")
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _norm_expires(text: str | None) -> str | None:
    """``--expires`` 归一化：``YYYY-MM-DD`` ⇒ 当日 23:59:59Z；完整 ISO 原样。"""
    if not text:
        return None
    s = text.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":     # YYYY-MM-DD
        return s + "T23:59:59Z"
    if s.endswith("Z"):
        return s
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(*, product: str, device_code: str, customer_id: str,
                  features: list[str], issued_at: str | None,
                  expires_at: str | None) -> dict:
    """组装激活码的 6 字段载荷（第 7 项 signature 由 ``encode_code`` 附加）。"""
    device_hash = license_mod.normalize_device_code(device_code)
    if len(device_hash) != 64:
        raise SystemExit(
            f"[license_gen] 设备码不合法（解析出 {len(device_hash)} 位十六进制，应为 64）："
            f"{device_code!r}\n  请让客户从界面复制完整设备码。")
    issued = issued_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "product": product,
        "device_hash": device_hash,
        "customer_id": customer_id,
        "features": list(features or []),
        "issued_at": issued,
        "expires_at": expires_at,        # None ⇒ 永久授权
    }


def sign_payload(private_key, payload: dict) -> str:
    """对载荷签名并打包成激活码字符串。"""
    signature = private_key.sign(license_mod.canonical_payload_bytes(payload))
    return license_mod.encode_code(payload, signature)


def generate(private_key, **kwargs) -> tuple[str, dict]:
    payload = build_payload(**kwargs)
    return sign_payload(private_key, payload), payload


def _self_test() -> int:
    """临时密钥对 → 签发 → 用客户端 ``verify_code`` 验签（含设备/过期断言）。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    pub_b64 = _b64url(raw_pub)

    device_hash = license_mod.device_hash({"seed": "self-test"})
    code, _ = generate(
        priv, product="USB-WIKI", device_code=device_hash, customer_id="SELF-TEST",
        features=["pro"], issued_at=None, expires_at=None)
    res = license_mod.verify_code(code, public_key_b64=pub_b64,
                                  device_hash_value=device_hash)
    ok1 = res["ok"]
    # 篡改一个字符 → 必须失败
    tampered = code[:-3] + ("AAA" if not code.endswith("AAA") else "BBB")
    res_bad = license_mod.verify_code(tampered, public_key_b64=pub_b64,
                                      device_hash_value=device_hash)
    ok2 = not res_bad["ok"]
    # 换一台设备 → 必须 DEVICE_MISMATCH
    res_dev = license_mod.verify_code(code, public_key_b64=pub_b64,
                                      device_hash_value=license_mod.device_hash({"seed": "other"}))
    ok3 = res_dev["status"] == license_mod.ST_DEVICE_MISMATCH

    print(f"[self-test] 签发→验签通过        {'OK' if ok1 else 'FAIL'} ({res['status']})")
    print(f"[self-test] 篡改后验签失败       {'OK' if ok2 else 'FAIL'} ({res_bad['status']})")
    print(f"[self-test] 换设备被拒           {'OK' if ok3 else 'FAIL'} ({res_dev['status']})")
    print(f"[self-test] 示例激活码           {code[:60]}…")
    return 0 if (ok1 and ok2 and ok3) else 1


def _emit(priv, *, product, device_code, customer_id, features,
          issued_at, expires_at, out) -> int:
    """签发并把结果打印到屏幕（可选写入文件）。CLI 与交互模式共用。"""
    code, payload = generate(
        priv, product=product, device_code=device_code, customer_id=customer_id,
        features=features, issued_at=issued_at, expires_at=expires_at)
    print("─" * 68)
    print("USB-WIKI 激活码（发给客户，让他们粘贴进「设置 → 产品授权」）")
    print("─" * 68)
    print(code)
    print("─" * 68)
    print(f"  产品     {payload['product']}")
    print(f"  客户     {payload['customer_id']}")
    print(f"  设备指纹 {payload['device_hash'][:16]}…")
    print(f"  功能位   {payload['features'] or '（无）'}")
    print(f"  签发时间 {payload['issued_at']}")
    print(f"  到期时间 {payload['expires_at'] or '永久'}")
    print(f"  签名公钥 {pubkey_mod.KEY_ID}")
    if out:
        Path(out).write_text(code + "\n", encoding="utf-8")
        print(f"\n已写入：{out}")
    return 0


def _interactive(private_path: Path, product: str) -> int:
    """双击/无参数运行时的引导式签发（逐项提问，免记命令行参数）。"""
    print("=" * 68)
    print("  Wiki-USB 离线授权 · 签发激活码")
    print("=" * 68)
    print("  流程：客户在程序「设置 → 产品授权」里复制「设备码」→ 发给你 →")
    print("        你在下面粘贴设备码 → 生成激活码 → 发回客户激活。")
    print()
    try:
        device = input("  ① 设备码（客户提供的整段）: ").strip()
        if not device:
            print("\n[已取消] 未输入设备码。")
            return 2
        customer = input("  ② 客户编号（只给你自己对账，客户界面不显示）: ").strip()
        if not customer:
            print("\n[已取消] 未输入客户编号。")
            return 2
        feat = input("  ③ 功能位（逗号分隔，可留空）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[已取消]")
        return 2

    features = [x.strip() for x in feat.split(",") if x.strip()]
    priv = load_private_key(private_path)
    print()
    # 授权模式固定为**永久**（激活后长期可用）；到期授权仅保留在 CLI 的 --expires 进阶用法里。
    return _emit(priv, product=product, device_code=device, customer_id=customer,
                 features=features, issued_at=None, expires_at=None, out=None)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="USB-WIKI 离线激活码生成器（Ed25519 签名，私钥不出本机）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device-code", help="客户提供的设备码（可带横线/大小写）")
    ap.add_argument("--customer-id", help="客户标识（写入激活码，便于对账）")
    ap.add_argument("--features", default="", help="功能位，逗号分隔，如 pro,sync")
    ap.add_argument("--product", default=pubkey_mod.PRODUCT, help="产品标识")
    ap.add_argument("--issued", default=None, help="签发时间（ISO8601，默认当前 UTC）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--perpetual", action="store_true",
                      help="永久授权（**默认**，一般不需要指定）")
    mode.add_argument("--expires", default=None,
                      help="（进阶，通常不用）到期时间：YYYY-MM-DD 或完整 ISO8601")
    ap.add_argument("--private", default=str(DEFAULT_PRIVATE), help="私钥 PEM 路径")
    ap.add_argument("--out", default=None, help="把激活码写入文件（默认打印到屏幕）")
    ap.add_argument("--self-test", action="store_true", help="跑内置自检后退出")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    # 无参数且处于交互终端（双击 .bat）→ 走引导式签发
    if not args.device_code and not args.customer_id and sys.stdin.isatty():
        return _interactive(Path(args.private), args.product)

    if not args.device_code or not args.customer_id:
        ap.error("必须提供 --device-code 与 --customer-id（或直接双击运行进入引导模式）")

    # 不指定模式即**永久授权**（产品默认：激活后长期可用）。到期授权仅作为进阶选项保留。
    features = [x.strip() for x in str(args.features or "").split(",") if x.strip()]
    expires_at = _norm_expires(args.expires) if args.expires else None
    priv = load_private_key(Path(args.private))
    return _emit(priv, product=args.product, device_code=args.device_code,
                 customer_id=args.customer_id, features=features,
                 issued_at=args.issued, expires_at=expires_at, out=args.out)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
