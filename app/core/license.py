"""离线机器绑定授权层 —— 客户端核心（**只验签，不签发**）。

## 一句话模型

    授权方持有**私钥**（tools/license_generator/keys/，永不随包）；
    客户端内置**公钥**（app/core/license_pubkey.py）。
    激活码 = 6 个明文字段 + 对它们的 Ed25519 签名。
    客户端每次启动：重算本机指纹 → 用公钥验签 → 比对 product / device_hash / 有效期。

## 为什么「复制到另一台电脑就失效」

激活码里写入的是**签发时那台机器**的 ``device_hash``（本机硬件指纹的 SHA-256）。
校验时把当前机器的指纹算出来逐字比对：换一台电脑 → 指纹不同 →
``DEVICE_MISMATCH`` → 提示「此激活码不属于当前设备。」。全程不联网、无 API。

## 铁律

* **完全离线**：本模块不 import 任何网络库，不发起任何请求。
* **客户端无密**：只内置公钥；签名能力（``sign_*``）只存在于 tools/ 工具里。
* **失败不落盘**：只有验签**通过**才写激活文件；任何失败路径都不碰磁盘
  （需求：激活失败不得污染真实目录）。
* **状态存用户目录**：``%LOCALAPPDATA%\\USB-WIKI\\license.json``
  （``WIKIUSB_STATE_DIR`` 可覆盖，供测试隔离），**绝不写程序目录**。
* **cryptography 懒加载**：运行时缺它也只降级为「无法验证」，绝不让 import 崩掉应用。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import atomic_io
from . import license_pubkey as pubkey_mod

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 激活码前缀（内含格式版本；将来改格式即换前缀，老码自然被识别为 MALFORMED）
CODE_PREFIX = "USBWIKI-1-"

#: 用户状态目录名（与 scripts/install_windows.py 保持一致）
STATE_DIRNAME = "USB-WIKI"
LICENSE_FILENAME = "license.json"

# 状态码 --------------------------------------------------------------------
ST_NO_LICENSE = "NO_LICENSE"
ST_OK = "OK"
ST_MALFORMED = "MALFORMED"
ST_BAD_SIGNATURE = "BAD_SIGNATURE"
ST_PRODUCT_MISMATCH = "PRODUCT_MISMATCH"
ST_DEVICE_MISMATCH = "DEVICE_MISMATCH"
ST_EXPIRED = "EXPIRED"
ST_CRYPTO_UNAVAILABLE = "CRYPTO_UNAVAILABLE"

#: 面向用户的中文提示（**需求 8 的文案必须逐字一致**）
MESSAGES = {
    ST_NO_LICENSE: "尚未激活。",
    ST_OK: "已激活。",
    ST_MALFORMED: "激活码格式无法识别，请核对后重新输入。",
    ST_BAD_SIGNATURE: "激活码无效或已被篡改。",
    ST_PRODUCT_MISMATCH: "该激活码不属于本产品。",
    ST_DEVICE_MISMATCH: "此激活码不属于当前设备。",
    ST_EXPIRED: "此激活码已过期。",
    ST_CRYPTO_UNAVAILABLE: "本机缺少签名校验组件，暂时无法验证授权。",
}

#: 激活码里必须包含的字段（需求 5）——签发与校验两侧的共同契约
REQUIRED_FIELDS = ("product", "device_hash", "customer_id",
                   "features", "issued_at", "expires_at")


class LicenseFormatError(ValueError):
    """激活码结构非法（前缀 / base64 / JSON / 缺字段）。"""


# ---------------------------------------------------------------------------
# base64url（无填充）
# ---------------------------------------------------------------------------
def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ---------------------------------------------------------------------------
# 设备指纹（SHA-256，绝不暴露原始序列号）
# ---------------------------------------------------------------------------
def _windows_machine_guid() -> str:
    """读 HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid。

    ⚠ 用 ``winreg`` 而非 ``wmic``：后者是子进程，在 pythonw 下会闪黑框
    （本项目刚修完的黑框回归）。``winreg`` 纯本地 API，零窗口、零延迟。
    Windows 安装期生成、重装系统才变 —— 正是我们要的「稳定且随机」。
    """
    try:
        import winreg  # type: ignore
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip()
    except Exception:  # noqa: BLE001 - 读不到就不作为指纹分量
        return ""


def _windows_volume_serial() -> str:
    """读系统盘卷序列号（``GetVolumeInformationW``，ctypes，无子进程）。"""
    try:
        import ctypes
        from ctypes import wintypes
        root = (os.environ.get("SystemDrive") or "C:") + "\\"
        fn = ctypes.windll.kernel32.GetVolumeInformationW
        fn.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
        ]
        fn.restype = wintypes.BOOL
        serial = wintypes.DWORD(0)
        ok = fn(root, None, 0, ctypes.byref(serial), None, None, None, 0)
        return f"{serial.value:08X}" if ok else ""
    except Exception:  # noqa: BLE001
        return ""


def _posix_machine_id() -> str:
    try:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            f = Path(p)
            if f.is_file():
                return f.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        pass
    return ""


def device_components() -> dict:
    """采集用于派生设备指纹的**分量字典**。

    * 生产：Windows 取 ``MachineGuid`` + 卷序列号 + 架构；Linux/macOS 取 machine-id。
    * 测试：``WIKIUSB_DEVICE_SEED`` 存在时**完全替换**为 ``{"seed": <值>}``，
      让测试能确定性地模拟「同一台」「另一台」机器，且**不碰真实硬件**。
    """
    seed = os.environ.get("WIKIUSB_DEVICE_SEED")
    if seed is not None and seed != "":
        return {"seed": seed}

    comps: dict[str, str] = {"platform": sys.platform}
    if os.name == "nt":
        guid = _windows_machine_guid()
        if guid:
            comps["machine_guid"] = guid
        serial = _windows_volume_serial()
        if serial:
            comps["volume_serial"] = serial
    else:
        mid = _posix_machine_id()
        if mid:
            comps["machine_id"] = mid
    arch = os.environ.get("PROCESSOR_ARCHITECTURE") or os.environ.get("PROCESSOR_ARCHITEW6432")
    if arch:
        comps["arch"] = arch
    return comps


def device_hash(components: dict | None = None) -> str:
    """把分量字典规范化为 SHA-256 十六进制（64 字符，小写）。

    规范化用 ``sort_keys + 紧凑分隔符``，保证「同样分量 ⇒ 同样指纹」，
    与签发端、校验端逐字节一致。
    """
    comps = device_components() if components is None else components
    canonical = json.dumps(comps, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def device_code(hash_value: str | None = None) -> str:
    """面向用户的**设备码**：把 64-hex 指纹按 8 位分组、大写，便于抄写/核对。"""
    h = (hash_value or device_hash()).upper()
    return "-".join(h[i:i + 8] for i in range(0, len(h), 8))


def normalize_device_code(text: str) -> str:
    """把用户抄来的设备码（可能带空格/横线/小写）还原成 64-hex 小写。"""
    keep = [c for c in (text or "").lower() if c in "0123456789abcdef"]
    return "".join(keep)


# ---------------------------------------------------------------------------
# 激活码编解码（签发端与校验端共用的**唯一**格式实现）
# ---------------------------------------------------------------------------
def canonical_payload_bytes(payload: dict) -> bytes:
    """被签名的规范化字节（两端必须逐字节一致）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def encode_code(payload: dict, signature: bytes) -> str:
    """把 6 字段载荷 + 签名打包成激活码字符串（签发端用）。"""
    obj = dict(payload)
    obj["signature"] = b64url_encode(signature)
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return CODE_PREFIX + b64url_encode(blob)


def decode_code(code: str) -> tuple[dict, bytes]:
    """解析激活码 → ``(6 字段载荷, 签名字节)``；结构非法抛 ``LicenseFormatError``。"""
    raw = "".join((code or "").split())          # 去掉换行/空格（用户可能粘贴带格式）
    if not raw.startswith(CODE_PREFIX):
        raise LicenseFormatError("缺少前缀")
    try:
        blob = b64url_decode(raw[len(CODE_PREFIX):])
        obj = json.loads(blob.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LicenseFormatError(f"载荷无法解析：{exc}") from exc
    if not isinstance(obj, dict) or "signature" not in obj:
        raise LicenseFormatError("缺少签名字段")
    sig = b64url_decode(str(obj.pop("signature")))
    missing = [f for f in REQUIRED_FIELDS if f not in obj]
    if missing:
        raise LicenseFormatError("缺少字段：" + ", ".join(missing))
    return obj, sig


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def _parse_iso8601(text: str) -> datetime | None:
    """解析 ``YYYY-MM-DDTHH:MM:SSZ``（或带 +00:00 / 无时区）。失败返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 验签（**客户端唯一的密码学入口**）
# ---------------------------------------------------------------------------
def _verify_signature(public_key_b64: str | None, payload: dict, signature: bytes) -> bool:
    """用公钥验 Ed25519 签名。缺 cryptography → 抛 ``RuntimeError``（由上层转状态码）。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = (pubkey_mod.public_key_bytes() if public_key_b64 is None
           else b64url_decode(public_key_b64))
    if not raw:
        raise RuntimeError("公钥未配置")
    key = Ed25519PublicKey.from_public_bytes(raw)
    try:
        key.verify(signature, canonical_payload_bytes(payload))
        return True
    except InvalidSignature:
        return False


def verify_code(code: str, *, public_key_b64: str | None = None,
                product: str | None = None,
                device_hash_value: str | None = None,
                now: datetime | None = None) -> dict:
    """校验一份激活码，返回 ``{"ok", "status", "message", "payload"}``。

    参数全部可注入（供测试确定性地模拟不同设备 / 时间 / 公钥），
    默认走真实设备指纹、真实当前时间、内置公钥。
    """
    def _res(status: str, payload: dict | None = None) -> dict:
        return {"ok": status == ST_OK, "status": status,
                "message": MESSAGES.get(status, status), "payload": payload}

    try:
        payload, signature = decode_code(code)
    except LicenseFormatError:
        return _res(ST_MALFORMED)

    try:
        sig_ok = _verify_signature(public_key_b64, payload, signature)
    except ImportError:
        return _res(ST_CRYPTO_UNAVAILABLE)
    except Exception:  # noqa: BLE001 - 公钥损坏等
        return _res(ST_CRYPTO_UNAVAILABLE)
    if not sig_ok:
        return _res(ST_BAD_SIGNATURE)

    exp_product = product if product is not None else pubkey_mod.PRODUCT
    if str(payload.get("product") or "") != exp_product:
        return _res(ST_PRODUCT_MISMATCH)

    current = (device_hash_value if device_hash_value is not None else device_hash())
    if normalize_device_code(str(payload.get("device_hash") or "")) != current.lower():
        return _res(ST_DEVICE_MISMATCH)

    expires_at = payload.get("expires_at")
    if expires_at:                                   # None/"", "" ⇒ 永久授权
        exp_dt = _parse_iso8601(str(expires_at))
        if exp_dt is None:
            return _res(ST_MALFORMED)
        if (now or _now_utc()) > exp_dt:
            return _res(ST_EXPIRED)

    return _res(ST_OK, payload)


# ---------------------------------------------------------------------------
# 激活状态存储（用户状态目录，原子写）
# ---------------------------------------------------------------------------
def state_dir() -> Path:
    """用户级状态目录：``WIKIUSB_STATE_DIR`` 覆盖 → 否则 ``%LOCALAPPDATA%\\USB-WIKI``。

    与 ``scripts/install_windows.py`` 的 ``_state_dir`` 采用**同一约定**，
    激活文件因此与安装记录（``install_state.json``）同处一目录、便于整体迁移。
    """
    override = (os.environ.get("WIKIUSB_STATE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else (Path.home() / ".usb-wiki")
    return base / STATE_DIRNAME


def license_file() -> Path:
    return state_dir() / LICENSE_FILENAME


def save_activation(record: dict) -> Path:
    """原子写入激活记录（**仅在验签通过后调用**）。"""
    path = license_file()
    text = json.dumps(record, ensure_ascii=False, indent=2)
    return atomic_io.atomic_write_text(path, text + os.linesep)


def load_activation() -> dict | None:
    """读取激活记录；不存在 / 损坏都返回 None（不抛）。"""
    f = license_file()
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_activation() -> bool:
    """删除本机激活记录。返回是否确有删除。"""
    f = license_file()
    if not f.is_file():
        return False
    try:
        f.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 高层：状态 / 激活 / 清除（供 API 与启动校验直接调用）
# ---------------------------------------------------------------------------
def status(public_key_b64: str | None = None, now: datetime | None = None) -> dict:
    """汇总当前授权状态（**只读**，不写任何文件）。

    含设备码（供用户抄给授权方）、状态码/提示、以及激活时的客户信息。
    """
    dh = device_hash()
    out: dict = {
        "activated": False,
        "status": ST_NO_LICENSE,
        "message": MESSAGES[ST_NO_LICENSE],
        "device_code": device_code(dh),
        "device_hash_id": dh[:16],          # 只露前 16 位，够核对不够反推
        "key_id": pubkey_mod.KEY_ID,
        "product": pubkey_mod.PRODUCT,
        "customer_id": None,
        "features": [],
        "issued_at": None,
        "expires_at": None,
        "perpetual": None,
    }
    record = load_activation()
    if not record:
        return out

    code = str(record.get("activation_code") or "")
    result = verify_code(code, public_key_b64=public_key_b64, now=now)
    out["status"] = result["status"]
    out["message"] = result["message"]
    out["activated"] = bool(result["ok"])
    payload = result.get("payload") or {}
    if payload:
        out["customer_id"] = payload.get("customer_id")
        out["features"] = list(payload.get("features") or [])
        out["issued_at"] = payload.get("issued_at")
        out["expires_at"] = payload.get("expires_at")
        out["perpetual"] = not bool(payload.get("expires_at"))
    return out


def activate(code: str, public_key_b64: str | None = None,
             now: datetime | None = None) -> dict:
    """校验并（成功时）落盘激活。**失败绝不写盘**（需求 12）。"""
    cleaned = "".join((code or "").split())
    if not cleaned:
        return {"ok": False, "status": ST_MALFORMED,
                "message": MESSAGES[ST_MALFORMED], "payload": None}

    result = verify_code(cleaned, public_key_b64=public_key_b64, now=now)
    if not result["ok"]:
        return result

    payload = result["payload"]
    record = {
        "activation_code": cleaned,
        "product": payload.get("product"),
        "customer_id": payload.get("customer_id"),
        "device_hash": payload.get("device_hash"),
        "features": list(payload.get("features") or []),
        "issued_at": payload.get("issued_at"),
        "expires_at": payload.get("expires_at"),
        "activated_at": _iso(now or _now_utc()),
        "key_id": pubkey_mod.KEY_ID,
    }
    try:
        save_activation(record)
    except OSError as exc:  # noqa: BLE001 - 磁盘不可写不能假装成功
        return {"ok": False, "status": ST_MALFORMED,
                "message": f"激活信息保存失败：{exc}", "payload": payload}
    return result


def clear() -> dict:
    """清除本机激活（回到未激活态）。"""
    removed = clear_activation()
    return {"ok": True, "removed": removed,
            "status": ST_NO_LICENSE,
            "message": "已清除本机激活。" if removed else "本机原本未激活。"}


#: 上下文/诊断用的便捷别名
def evaluate() -> dict:
    return status()
