# V1 Release Freeze

> 本文档**只记录已经确定的边界**。模型相关的产品决策仍保持未冻结状态，
> 见文末「仍未冻结（严禁在本文档内重新定死）」。任何对未冻结项的改动都需回到决策流程，
> 不得借本文档偷偷定死。

生效时间：2026-09-17（用户宣布 V1 Release Freeze 生效）

---

## 1. 总纪律

- 只允许动手 `BLOCKER` / `RELEASE_REQUIRED`；其余一律 `POST_V1`。
- 每发现新问题**必须先分类**，未经分类不得实施。
- 严禁「顺便可以优化」——判断标准：**不做它，会不会影响陌生用户安装 / 使用 / 恢复 / 安全？**
  不会 → `POST_V1`。
- 路线：`Windows Portable/离线发布 → Release Pipeline → RC 真机破坏测试 → 陌生用户安装测试 → V1 Release`。
  每阶段完成后须先汇报，不得自动推进下一阶段。
- 安全教训：**降级 / 兜底分支必须与主路径同受安全闸门约束**（headless Chrome fallback 曾可在安全拒绝后照抓内网）。

---

## 2. Distribution Layer A1（已冻结）

1. **发布产物是构建产物**：`scripts/build_release.py` 生成 `dist/USB-WIKI-v{APP_VERSION}-{platform}/`；
   `dist/` 不进 Git；大型 runtime / 模型 / Ollama 二进制不进 Git。
2. **SSD 默认目录**：
   - App：`%LOCALAPPDATA%\USB-WIKI\App`（App 根由 `paths.py` 用 `__file__` 推导，
     **不新增 `WIKIUSB_APP_ROOT`**）
   - Library：`%USERPROFILE%\Documents\USB-WIKI-Data`
     - Windows 优先经 Known Folder API 取真实 Documents（覆盖 OneDrive 重定向 / 企业策略 / 迁移），
       失败 fallback `%USERPROFILE%\Documents`
     - 经已有环境变量 `WIKIUSB_LIBRARY` 重定向；**安装器只设进程级 env，绝不写系统全局环境变量**
3. **客户安装零 pip**：发布时随包交付预构建 **Python Embedded Runtime**，整体拷贝到 SSD；
   无 `get-pip` / `pip install` / 联网下载；**wheelhouse 不是 V1 客户安装路径**。
4. **Library 是绝对保护边界**：App / Runtime 可覆盖；**Library 绝不覆盖 / 删除 / 重命名 / 移动 /
   不当 staging / 不参与 rollback**。已存在则接管。

---

## 3. Distribution Layer A2（已冻结）

- **安装必须事务化**：
  `payload 校验 → 复制到 staging → 验证 staging → 旧 App 备份 → staging 交换为正式 App
  → (可选) 安装后 smoke → 清理 backup`。
  推荐同父目录：`App` / `App.staging` / `App.backup`（命名可调整）。
- **任意阶段失败必须 rollback**：payload copy / runtime copy / launcher 生成 / staging 验证 /
  swap / 安装后 smoke 任一失败 → 恢复旧 App；**首次安装失败则清理半成品，绝不留下可误启动的 App**。
- **Library 全程不碰**：安装 / 更新 / rollback 前后 `Library SHA256` 完全一致；Library 不参与任何程序侧事务。
- **用户启动入口**：`App\启动-Windows.bat` 默认 `runtime\python.exe app\launcher.py`
  （**不含 `--no-browser`**，自动开浏览器）。`--no-browser` 仅用于测试 / CI / 调试，不得写死进用户入口。
- **构建 strict gate**：`python scripts/build_release.py --strict` 在缺
  `app` / `嵌入式 runtime` / `installer` 任一时**非零退出**，不产生「看起来成功但不可安装」的发布包；
  普通开发模式仍可允许缺 runtime（告警跳过）。

---

## 3b. Distribution Layer A3（已冻结）

- **发布介质必须自证完整**：`RELEASE_MANIFEST.json`（相对路径 / POSIX 分隔符 / 固定排序 /
  含 size + sha256 / **不含自身**）与传统 `SHA256SUMS` 由**同一套枚举**产出，不允许两套实现漂移。
- **`BUILD_INFO.json` 字段一律取已有唯一来源**，禁止再次硬编码：
  `app_version ← app/version.py`；`schema_version ← app/core/migrations.py::CURRENT_SCHEMA_VERSION`；
  `data_format_version ← app/core/library.py::DATA_FORMAT_VERSION`；
  `dependency_lock_sha256 ← requirements-release.lock 实际 SHA256`；
  `release_format_version ← scripts/release_integrity.py::FORMAT_VERSION`。
  **绝不含**用户名 / 绝对路径 / HOME / LOCALAPPDATA / IP / 机器标识 / key。
- **安装前先验介质**：`install` 必须在创建 staging、删除或重命名任何旧 App **之前**
  校验 存在性 / size / SHA256；缺文件 / hash 不符 / manifest 损坏 → `MEDIA_CORRUPTED`（rc=4）立即中止。
  **严禁**「发现一个坏文件 → 先覆盖一半 App → 再报错」。
- **`verify` 只读命令**：只校验 U 盘介质，不建 Library、不改 App、不联网；
  输出 `OK` 或 `MEDIA_CORRUPTED` + 失败的**相对**文件（不输出客户隐私路径）。
- **SHA256 只为 Integrity**（介质损坏检测），**不做 Authenticity**：不签名、不加密、
  不做激活 / DRM / license key / updater。Code Signing 留到 Release / RC Gate 再判断。
- **第三方许可清单 `LICENSES/`** 只审**本 Release 实际随包分发**的内容；
  当前无 bundled Ollama / LLM / GGUF / ONNX model，就不替它们收 license。
  许可**不允许猜**：元数据无法可靠确认时标 `LICENSE_REVIEW_REQUIRED`，正式 V1 前必须清零。
- **strict 门禁**（`--strict`）成功条件：app / 嵌入式 runtime / installer / BUILD_INFO /
  LICENSES inventory 完整 / RELEASE_MANIFEST / SHA256SUMS / 最终介质自校验，全通过才退出 0。
  非 strict 可宽松，但**不得**输出「正式 Release 可交付」字样。
- **基础恢复路径**：损坏 App + 完好 Library → 从完好介质重装 App → Library SHA256 不变。
  **不新建 Repair Engine**；cache/index 修复沿用既有 Data Contract + rebuild 能力。

---

## 4. 当前允许推进的分类

| 分类 | 含义 | 处理 |
| :--- | :--- | :--- |
| `BLOCKER` | 阻断陌生用户安装 / 启动 / 恢复的缺陷 | 立即修 |
| `RELEASE_REQUIRED` | 发布前必须完成（含负向验证） | 完成并验证后可合入 |
| `POST_V1` | 有价值的增强，但不影响当前发布门槛 | 记录，发布后再做 |

---

## 5. 仍未冻结（严禁在本文档内重新定死）

以下模型相关产品决策**保持开放**，Core 不得硬编码任何来源，A1/A2 也未替它们做决定：

- 是否 U 盘 **bundled LLM**
- 模型规模 **2B / 4B**
- **Ollama bundled vs system**
- 是否允许**在线 `ollama pull`**
- **ONNX 最终策略**（默认是否捆绑 / 哪个嵌入模型 / 是否预装 LLM / 完全离线版）

---

## 6. 明确不在 A1 / A2 / A3 范围（冻结前不做）

Repair Engine / Ollama 安装 / 模型下载 / GGUF / ONNX 最终策略 / 模型推荐 / OCR / Reranker /
新 UI / 云同步 / 激活 / updater / 代码签名 / 私钥 / ZIP / GitHub Release / Tag / RC 测试。

（`LICENSES` 汇总 / `BUILD_INFO` / `SHA256SUMS` 已在 A3 完成初版；仍**不含**模型类资源，
待 A4 决定真正捆绑什么后再纳入同一机制。）
