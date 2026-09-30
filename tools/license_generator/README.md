# USB-WIKI 离线授权 · 签发工具

本目录是**授权方专用**的离线激活码生成器。它签发激活码，**不参与**客户端的运行。

> ⚠ **私钥安全**：`keys/private_ed25519.pem` 是唯一能伪造激活码的东西。
> 它只在本机、已被 `.gitignore` 排除、且**永远不会进入发布包**（`build_release.py`
> 只拷贝 `app/` `runtime/` 与文档白名单，`tools/` 从不随包）。请自行备份到安全介质。

---

## 一、一次性：生成密钥对

用仓库自带运行时跑最省事（它已含 `cryptography`）：

```bat
runtime\python-3.11-embed\python.exe tools\license_generator\genkey.py
```

它会：
- 写 `keys/private_ed25519.pem`（私钥，保密）与 `keys/public_ed25519.pem`（公钥）；
- 打印两行，贴进 `app/core/license_pubkey.py`：

```python
PUBLIC_KEY_B64 = "……"
KEY_ID = "……"
```

> 轮换密钥对用 `genkey.py --force`。**轮换会让已签发的所有激活码立即失效。**

---

## 二、给一台机器签发激活码

> 💡 **最省事**：直接到项目**主目录双击 `签发激活码.bat`** —— 会进入**引导模式**，逐项提示你输入
> 设备码 / 客户号 / 授权模式，无需记任何参数。下面是命令行等价用法。

1. 让客户打开 **设置 → 产品授权**，复制上面的**设备码**（形如 `C3285E2A-3A90-A661-…`）发给你。
2. 你签发：

```bat
rem 标准做法：永久授权（激活后长期可用，默认就是它）
runtime\python-3.11-embed\python.exe tools\license_generator\license_gen.py ^
    --device-code "C3285E2A-3A90-A661-…" --customer-id CUST-0001

rem 需要功能位时（可选）
runtime\python-3.11-embed\python.exe tools\license_generator\license_gen.py ^
    --device-code "C3285E2A-…" --customer-id CUST-0002 --features pro,sync
```

3. 把打印出的**激活码**发给客户，客户粘贴进「设置 → 产品授权 → 输入激活码」即完成激活。
   之后**完全离线**，客户端只用内置公钥验签。

### 参数

| 参数 | 说明 |
|---|---|
| `--device-code` | **必填**，客户提供的设备码（带横线/大小写均可） |
| `--customer-id` | **必填**，客户标识（**只给你自己对账**，客户界面不显示） |
| `--perpetual` | 永久授权 —— **默认行为，一般不需要写** |
| `--expires` | ⚠ 进阶选项，产品默认**不使用**（到期时间 `YYYY-MM-DD` 或完整 ISO8601） |
| `--features` | 功能位，逗号分隔，如 `pro,sync` |
| `--product` | 产品标识，默认 `USB-WIKI` |
| `--private` | 私钥路径，默认 `keys/private_ed25519.pem` |
| `--out` | 把激活码写入文件（默认只打印） |
| `--self-test` | 内置自检：临时密钥对→签发→客户端逻辑验签 |

---

## 三、自检

```bat
runtime\python-3.11-embed\python.exe tools\license_generator\license_gen.py --self-test
```

证明「签发端格式」与「客户端校验」逐字节一致（签发通过 / 篡改失败 / 换设备被拒）。

---

## 四、激活码长什么样

一枚激活码 = `USBWIKI-1-` + base64url(JSON)，JSON 内含 7 个字段：

```json
{
  "product": "USB-WIKI",
  "device_hash": "<64 位十六进制设备指纹>",
  "customer_id": "CUST-0001",
  "features": ["pro"],
  "issued_at": "2026-09-29T00:00:00Z",
  "expires_at": "2027-09-29T23:59:59Z",
  "signature": "<Ed25519 签名，base64url>"
}
```

`signature` 是对前 6 个字段（规范化 JSON）的 Ed25519 签名。客户端用内置公钥验签，
并逐字比对 `device_hash` 与本机指纹 —— 换一台电脑即 `DEVICE_MISMATCH`。

---

## 五、更换 / 轮换密钥

密钥用久了要换（疑似泄露、产品更名、改用你自己新生成的密钥）时：

```bat
rem 1) 备份旧密钥（轮换不可逆，旧码将全部失效）
xcopy /E /I /Y tools\license_generator\keys  "%USERPROFILE%\.usb-wiki-license\keys-backup" >nul

rem 2) 生成新密钥对（--force 才允许覆盖已有私钥）
runtime\python-3.11-embed\python.exe tools\license_generator\genkey.py --force

rem 3) 把打印出的两行覆盖进 app/core/license_pubkey.py，然后重启程序
```

⚠ **轮换 = 一次性吊销全部已签发激活码**：客户端换了公钥，旧码一律验签失败
（显示「激活码无效或已被篡改。」），**所有客户必须重新激活**。轮换前先通知客户。

> 完整操作手册（含"密钥存哪 / 怎么把密钥挪到仓库外 / 各种提示怎么办"）见
> `docs/授权使用与密钥管理.md`。

## 六、把密钥存到别处

工具默认读 `keys/private_ed25519.pem`；改用别处用 `--private` 显式指定：

```bat
runtime\python-3.11-embed\python.exe tools\license_generator\license_gen.py ^
    --private "%USERPROFILE%\.usb-wiki-license\keys\private_ed25519.pem" ^
    --device-code "…" --customer-id CUST-0003
```
