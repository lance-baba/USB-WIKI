"""内置于客户端的 **Ed25519 公钥**（唯一可信根）。

## 铁律

本模块**只允许出现公钥**。私钥永不入库、永不随包 —— 它住在授权方的
``tools/license_generator/keys/`` 里，用 ``genkey.py`` 生成、用 ``license_gen.py`` 签发。

客户端在这里内置公钥，用它对每一份激活码做 **数字签名验签**：
指纹对得上只说明「内容没被篡改」，验签通过才说明「确实是授权方签的」。

## 轮换方式

重新运行 ``tools/license_generator/genkey.py --force`` 生成新密钥对，
把打印出来的两行覆盖到下面即可。**轮换会让已签发的所有激活码立即失效**，
请按需重新签发。

## 为什么是「原始 32 字节 + base64url」而不是 PEM

原始点格式（RFC 8032）没有 ASN.1/DER 包装，是最不易被解析差异坑到的表示，
也便于人工核对长度。``KEY_ID`` 是公钥自身 SHA-256 的前 16 个十六进制字符，
用于在界面/日志里**标识是哪把钥匙在验签**（轮换后一眼能看出）。
"""

from __future__ import annotations

import base64
import hashlib

#: 授权方 Ed25519 公钥（原始 32 字节，base64url，无填充）
PUBLIC_KEY_B64 = "pH19Eop6WaQ5gsETHyBEJjADbrfwV2_B1GqeM90zAQs"

#: 公钥指纹（sha256(raw_pubkey) 的前 16 hex）—— 供展示 / 排障
KEY_ID = "045a1efb0148fef8"

#: 客户端期望的产品标识（激活码里的 product 必须等于它）
PRODUCT = "USB-WIKI"


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def public_key_bytes() -> bytes:
    """返回原始 32 字节公钥。空值即视为「未配置」——调用方应据此拒绝放行。"""
    if not PUBLIC_KEY_B64:
        return b""
    return _b64url_decode(PUBLIC_KEY_B64)


def verify_key_id() -> str:
    """用公钥字节重算指纹，与 ``KEY_ID`` 不一致说明常量被改坏了。"""
    raw = public_key_bytes()
    return hashlib.sha256(raw).hexdigest()[:16] if raw else ""
