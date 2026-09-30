# 离线机器绑定授权层 · 设计说明

> 面向 V1 的**可选**授权能力：完全离线、Ed25519 签名、一机一码、换机即失效。
> 本文说明**设计、边界与运维**；代码实现见 `app/core/license.py`、`app/api/license.py`。

---

## 1. 目标与约束

| 目标 | 落地方式 |
|---|---|
| 完全离线，不请求任何网络 | 客户端无任何网络调用；只用内置公钥做本地验签 |
| Ed25519 数字签名 | 授权方私钥签名，客户端公钥验签（`cryptography` 库，随嵌入式运行时自带） |
| 客户端**只**内置公钥 | `app/core/license_pubkey.py` 仅含公钥常量；私钥只存在于 `tools/license_generator/` |
| 私钥不进发布包 | 构建只拷 `app/` `runtime/` + 文档白名单，`tools/` 从不随包；测试逐项断言 |
| 一机一码 | 激活码内写死签发机的 `device_hash`，校验时重算比对 |
| 激活失败不污染真实目录 | 失败路径**不写盘**；状态目录可被 `WIKIUSB_STATE_DIR` 重定向 |

---

## 2. 信任模型

```
       授权方（你）                              客户机
 ┌──────────────────────┐                ┌──────────────────────────┐
 │ private_ed25519.pem  │  签发激活码     │  PUBLIC_KEY_B64（内置）    │
 │  （绝不外发）         │ ─────────────► │  verify(signature, payload)│
 └──────────────────────┘                └──────────────────────────┘
```

* **只有授权方能签发**：没有私钥就无法伪造能通过验签的激活码。
* **客户端不可自签**：客户端代码里没有私钥，也没有签名函数（签名只在 `tools/`）。
* **改了就用不了**：激活码里 6 个字段任一被改，规范化字节变化 → 签名不匹配 → 拒绝。

---

## 3. 激活码格式

`USBWIKI-1-` + `base64url(JSON)`，JSON 含 7 个字段：

```json
{
  "product":     "USB-WIKI",
  "device_hash": "<64 位十六进制设备指纹>",
  "customer_id": "CUST-0001",
  "features":    ["pro"],
  "issued_at":   "2026-09-29T00:00:00Z",
  "expires_at":  "2027-09-29T23:59:59Z",   // null ⇒ 永久授权
  "signature":   "<Ed25519(前 6 字段规范化 JSON)，base64url>"
}
```

* 规范化：`json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 的 UTF-8 字节。
  该规则由 `app/core/license.py::canonical_payload_bytes` **唯一定义**，签发工具 import 它复用，杜绝两端漂移。
* 前缀带格式版本（`-1-`）：将来改格式换前缀，老码自然判为 `MALFORMED`。

---

## 4. 设备指纹（`device_hash`）

* 分量（Windows）：`MachineGuid`（注册表，`winreg` 读）+ 系统盘卷序列号（`ctypes` 调 `GetVolumeInformationW`）+ 架构。
  **全部零子进程**——刻意避开 `wmic` / `platform.system()`，否则会在 `pythonw` 下闪黑框（本项目刚修完的黑框回归）。
* Linux/macOS：`/etc/machine-id` 或 `/var/lib/dbus/machine-id`。
* 规范化后 `SHA-256` → 64 位十六进制；界面上按 8 位分组展示（**绝不显示原始序列号**，满足需求 6）。
* 测试钩子：`WIKIUSB_DEVICE_SEED` 存在时**整体替换**为 `{"seed": <值>}`，可确定性模拟不同机器。

---

## 5. 状态存储

* 位置：`%LOCALAPPDATA%\USB-WIKI\license.json`（与安装记录 `install_state.json` 同目录）。
* 覆盖：`WIKIUSB_STATE_DIR`（测试隔离）；未设 `LOCALAPPDATA` 时退回 `~/.usb-wiki/USB-WIKI`。
* **绝不写程序目录**（需求 9）。原子写（`atomic_io`）。
* 记录内容：激活码原文 + 解析出的字段快照 + 激活时间 + 公钥指纹。

---

## 6. 启动校验（需求 7）

`AppContext.boot()` 最后一步 `license.status()`：重算本机指纹 → 读 `license.json` → 验签 + 比对 product/device/有效期 →
结果存入 `ctx.license`，随 `/api/status` 下发。**只读、绝不阻断启动**（授权不足是功能策略，不是启动故障）；
任何异常只记 `debug`。因此对既有启动行为是**纯增量**。

---

## 7. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/license/status` | 当前状态（设备码 + 客户信息 + 有效期） |
| GET | `/api/license/device-code` | 首启生成设备码（供抄给授权方） |
| POST | `/api/license/activate` | `{code}` 输入激活码完成激活 |
| POST | `/api/license/clear` | 清除本机激活 |

全部返回 HTTP 200，业务结果在 `data.ok` / `data.status` / `message`。

### 状态码

| 码 | 含义 | 用户文案 |
|---|---|---|
| `OK` | 已激活 | 已激活。 |
| `NO_LICENSE` | 无激活记录 | 尚未激活。 |
| `MALFORMED` | 结构非法 | 激活码格式无法识别，请核对后重新输入。 |
| `BAD_SIGNATURE` | 验签失败 / 被篡改 | 激活码无效或已被篡改。 |
| `PRODUCT_MISMATCH` | 产品不符 | 该激活码不属于本产品。 |
| `DEVICE_MISMATCH` | **换机** | **此激活码不属于当前设备。**（需求 8 逐字文案） |
| `EXPIRED` | 过期 | 此激活码已过期。 |
| `CRYPTO_UNAVAILABLE` | 缺验签组件 | 本机缺少签名校验组件，暂时无法验证授权。 |

---

## 8. 前端

设置页新增「产品授权」卡片：状态徽标 / 设备码（含复制）/ 激活码输入框 / 激活 + 清除按钮；
顶栏新增「授权」芯片，随 `/api/status` 轮询实时更新。

---

## 9. 运维：签发一枚激活码

见 `tools/license_generator/README.md`。速览：

```bat
rem 一次性：生成密钥对（把打印的公钥填进 app/core/license_pubkey.py）
runtime\python-3.11-embed\python.exe tools\license_generator\genkey.py

rem 签发（永久 / 到期）
runtime\python-3.11-embed\python.exe tools\license_generator\license_gen.py ^
    --device-code "<客户设备码>" --customer-id CUST-0001 --perpetual
```

---

## 10. 测试

`tests/test_license_offline.py`（standalone，**不接入** `test_suite.py`，不改动既有 Gate）：

```bat
runtime\python-3.11-embed\python.exe tests\test_license_offline.py
```

覆盖：正确激活 / 错误设备 / 篡改 / 过期 / 换机失效 / 公钥验证 / 发布物无私钥 / 失败不污染真实目录。
测试自带**临时密钥对**（不依赖生产私钥，fresh checkout 也能跑），并用内容标记而非后缀识别真正的私钥文件。

---

## 11. 边界与后续（明确不在本次范围）

* 门禁边界（**受限模式**）：未激活时**允许**笔记浏览、本地检索、拖入文件入库、粘贴文字入库；
  **禁止**网页抓取入库（`/api/capture/url`）与 AI 问答（`/api/chat/completions`）。
  实现见 `app/api/_gate.py`。原则：**绝不用"锁死用户自己的数据"来施压** —— 已录入/将录入的笔记永远可读可写可搜。
* 激活成功后**立即解锁**（`/api/license/activate` 会刷新 `ctx.license`），无需重启。
* 未做：吊销列表（离线场景无法实时吊销，只能靠吊销时下发新公钥并要求重签）、按功能位放行的功能开关、批量签发 UI。
* 密钥轮换：`genkey.py --force` 后需把新公钥覆盖进 `app/core/license_pubkey.py`——轮换会让**已签发激活码全部失效**，需重签。
