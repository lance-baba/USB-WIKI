#!/usr/bin/env python3
"""生成 Ed25519 密钥对（**授权方本地一次性运行**）。

## 它做什么
在 ``tools/license_generator/keys/`` 下写两个文件：
  * ``private_ed25519.pem``  —— 私钥，**只在本机**，绝不入库、绝不进发布包
  * ``public_ed25519.pem``   —— 公钥（PEM，便于查看）

并把「可内嵌进客户端的公钥」打印出来（base64url 未填充的 32 字节原始公钥），
供你贴进 ``app/core/license_pubkey.py`` 的 ``PUBLIC_KEY_B64``。

## 为什么私钥绝不进发布包
客户端只需要**公钥**验签。私钥一旦随包分发，任何人都能伪造激活码。
本目录被 ``.gitignore`` 的 ``*.pem`` 规则排除，``scripts/build_release.py``
也只拷贝 ``app/`` ``runtime/`` 与文档白名单 —— ``tools/`` 从不进入发布产物。

## 怎么跑（最省事：用仓库自带运行时，它已含 cryptography）
    runtime\\python-3.11-embed\\python.exe tools\\license_generator\\genkey.py

若用系统 Python，需先 ``pip install cryptography``。
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS_DIR = HERE / "keys"
PRIV_PEM = KEYS_DIR / "private_ed25519.pem"
PUB_PEM = KEYS_DIR / "public_ed25519.pem"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main(argv: list[str]) -> int:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        print("[genkey] 缺少 cryptography。请用仓库自带运行时运行：\n"
              "  runtime\\python-3.11-embed\\python.exe tools\\license_generator\\genkey.py",
              file=sys.stderr)
        return 2

    force = "--force" in argv
    if PRIV_PEM.exists() and not force:
        print(f"[genkey] 私钥已存在：{PRIV_PEM}\n"
              "  拒绝覆盖（覆盖会让**已签发的所有激活码立即失效**）。\n"
              "  确要重新生成请加 --force。", file=sys.stderr)
        return 1

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    raw_pub = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    PRIV_PEM.write_bytes(priv_bytes)
    PUB_PEM.write_bytes(pub_bytes_pem)
    # 尽力收紧权限（POSIX；Windows 上无效果，靠目录选择与 .gitignore 兜底）
    try:
        import os
        os.chmod(PRIV_PEM, 0o600)
    except OSError:
        pass

    key_id = hashlib.sha256(raw_pub).hexdigest()[:16]
    print("[genkey] 已生成密钥对：")
    print(f"  私钥（保密，勿分发）  {PRIV_PEM}")
    print(f"  公钥（公开）          {PUB_PEM}")
    print(f"  公钥指纹 key_id       {key_id}")
    print()
    print("把下面两行填进 app/core/license_pubkey.py：")
    print(f'  PUBLIC_KEY_B64 = "{b64url(raw_pub)}"')
    print(f'  KEY_ID = "{key_id}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
