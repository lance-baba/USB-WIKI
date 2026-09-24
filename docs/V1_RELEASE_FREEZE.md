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

### 3c. A3.1 收口（已冻结）

- **`lock` 是发布基线，runtime 跟随 lock**。BUILD_INFO 记录的是这份 lock 的 SHA256，
  那么随包运行时就必须确实由这份 lock 构建 —— 否则「可追溯」是假的。
  **禁止**为了通过门禁反向修改 lock 去迎合旧 runtime。
- **strict 门禁新增三条硬条件**（每条带稳定错误码，便于 CI / 售后按码定位）：
  - `RUNTIME_LOCK_MISMATCH` —— 随包运行时与依赖锁版本不一致
  - `LICENSE_TEXT_MISSING` —— **随包运行依赖**（lock 内）只有元数据、缺许可原文
  - `VENDOR_LICENSE_INVALID` —— vendor 许可原文库缺件 / 被改写
  另有 `LICENSE_REVIEW_REQUIRED_REMAINING`。失败时 stderr 额外输出一行
  `[build] GATE FAILED codes=…` 供机器读取。
- **本地 runtime 陈旧导致 strict FAIL 是正确行为**（记为 `LOCAL_RUNTIME_STALE`），
  不得自动 pip install / 重建 runtime 来「修绿」；freshness 由 CI 的 fresh runtime 证明。
- **上游不随附许可原文时用 vendor 补齐**：`vendor/licenses/<pkg>/<ver>/` 放**原字节**许可文本
  + `PROVENANCE.json`（上游项目 / tag / **完整 commit** / 每个文件的 source_url 与 sha256）。
  构建时**只做本地拷贝、永不联网** —— 正式发布构建必须可复现。
- **许可文本只允许原字节**：禁止手写、翻译、重排、补全。`PROVENANCE.json` 的 sha256
  就是这条规则的执行器（改一个字节 → `VENDOR_LICENSE_INVALID`）。
- `metadata_only` **不是**发布完成态：随包运行依赖不得停留在该状态。

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

**Collections（资料集）+ Library Management Layer**：分类 `POST_V1`，**架构已冻结**
（ADR 全文见 [`docs/COLLECTIONS_ARCHITECTURE.md`](COLLECTIONS_ARCHITECTURE.md)，
含 Global Retrieval / Explicit Scope / durable metadata / Trash-Export 等 invariants）。
本轮**只落档、不实现**：不改生产代码 / DB schema / 真实 Library / 检索行为。

（`LICENSES` 汇总 / `BUILD_INFO` / `SHA256SUMS` 已在 A3 完成初版；仍**不含**模型类资源，
待 A4 决定真正捆绑什么后再纳入同一机制。）

---

## 6b. AI 能力分层（A4.1 冻结，2026-09-17）

### 三层能力，缺哪层都不影响基础使用

| 层 | 能力 | 依赖 |
| :--- | :--- | :--- |
| **Level 1 基础知识库** | 启动 / 导入 / 查看 / FTS 检索 / 返回结果 / 显示引用来源 | **永不依赖 LLM**（无 Ollama、无聊天模型、无显卡、无网络也必须可用） |
| **Level 2 本地语义检索** | 向量召回，低配 CPU 可跑，**不要求 Ollama** | 嵌入源链；不可用即退化为纯 FTS5 |
| **Level 3 生成式对话** | 自然语言总结回答 | **用户选择的**本地 Ollama 模型，或已配置的云端 API |

### Level 2

- 现状链（**不推翻**）：`local_onnx` → `ollama` → `api` → 最终整体关闭向量路（`source=none`）。
- `local_onnx` = V1 正常本地 semantic baseline。
- `local_hash` = 降级 / 灾难兜底，**以 `embedding_source = local_hash` 显式启用**
  （不自动进入：哈希向量会覆写主源签名 → 形成「降级→重建」的破坏性循环）。
- **本阶段不选具体 ONNX artifact**：型号 / 来源 / SHA256 / license / CPU 实测一律留到 A4.2。

### Level 3

- **V1 标准版不要求随盘自带 LLM。** 模型来源正式允许三类，Core 不得写死任何一种：
  `existing`（客户机已有 Ollama + 模型）· `downloadable`（客户同意后联网下载）·
  `bundled`（未来某 SKU 随盘提供）。**A4.1 只实现 `existing`**，另两类仅保留契约。
- **聊天模型没有默认值**：`ollama_chat_model` 新安装为空，Core 不得预设、也不得自动挑
  `models[0]`（本机模型可能是 embedding / vision / 资源超标的模型）。
- **就绪状态（稳定枚举）**：

  | state | 含义 | 是否允许 provider=ollama |
  | :--- | :--- | :--- |
  | `no_runtime` | 本机无可用 Ollama | ❌ |
  | `no_model` | Ollama 在线但一个模型都没有 | ❌ |
  | `selection_required` | 有模型但用户未选 | ❌ |
  | `model_missing` | 选过的模型在本机已不存在 | ❌ |
  | `ready` | 已选择且本机可用 | ✅ |

  ⚠ **Ollama 进程活着 ≠ 可以对话**：`ollama_healthy` 只表示 `/api/tags` 可访问。
  是否可对话一律看 `chat_ready`。

- **auto 解析顺序**：有效 Ollama 所选模型 → `ollama`；否则有明确配置的 API → `api`；
  否则 `offline`（**仍允许知识检索**）。
- `provider = ollama` 模式表示用户明确要求**只走本地**（内容不上云），不就绪时降级为
  `offline`，**绝不改走 `api`**。
- **旧配置兼容**：既有 `ollama_chat_model=xxx` 继续读取；存在即 `ready`，
  不存在即 `model_missing`，**不静默替换成其它模型**。
- **无可用聊天模型时**（硬要求）：不报启动失败、不阻止导入 / 搜索、不删索引、
  不改 embedding signature、不假装 AI 可用；明确说明「本地 AI 模型尚未配置」，
  问答转 `provider=offline` 的纯离线检索回答，且**不得把检索结果伪装成 LLM 生成**。

### 本阶段不决定（留 A4.2 / A4.3）

具体 ONNX embedding 型号 · ONNX artifact 来源 / SHA256 / license · 是否 bundle Ollama runtime ·
推荐聊天模型 · 2B / 4B · 默认下载模型 · 在线下载 UX · 完全离线 SKU。

---

## 7. Release Gate 与 CI 触发策略（2026-09-25 修订）

> ⚠ **本节已按新的产品决策修订**：V1 正式目标平台为 **Windows**，
> Release Gate 改为 **Windows-only、本地优先**。
> 旧的「GitHub 5-lane Full CI = 发布前置条件」口径**作废**；
> GitHub Actions 降级为 **optional independent cloud verification（可选的独立云端验证）**。

### 7.1 为什么改

1. V1 的实际交付对象是 **Windows 用户**；
2. 本地 **embedded runtime** 才是产品真实运行形态；
3. Ubuntu / Python version matrix 对当前 V1 发布价值有限；
4. 避免不必要的 GitHub Actions 云端资源消耗；
5. GitHub 普通 push 继续只作为**备份 / 回退**。

### 7.2 V1 默认 Release Gate（Windows-only，本地优先）

以下各项**全部通过**，才算 Windows Pilot Release 门槛达成：

```
Windows Embedded Runtime full test suite
  → build_release.py --strict
  → Release media self-verification
  → Windows launcher / startup smoke
  → fresh install
  → real DOCX / PDF import
  → search / Q&A / citations
  → quit / reopen
  → default uninstall preserves Library
  → reinstall restores Library
  → custom App path
  → Pilot package
```

### 7.3 GitHub Actions 的当前定位（已降级）

**不作为 V1 默认发布前置条件。** 仅在以下情况由**人工**决定使用：

- 需要**独立 clean-machine** 验证；
- **大版本发布前**主动复核；
- 本地环境**无法判断平台问题**。

硬规则：

- **不得**因普通 commit / push 自动运行；
- **不得**因为 GitHub 5-lane 未运行而阻止 Windows Pilot Release
  —— **前提是 Windows Release Gate（§7.2）全部通过**；
- workflow 文件**继续保留**：不删除、不为了本轮修改 CI 架构，
  只是把它从「默认 Release Gate」降级为可选验证。

| 动作 | 是否跑 Full CI | 说明 |
| :--- | :--- | :--- |
| 普通 `push main` | ❌ | 只作备份 / commit 历史 / 可回退点 |
| `workflow_dispatch`（手动） | ⚠️ 可选 | 独立云端验证，**非默认前置条件** |
| `push` `v*` tag | ⚠️ 可选 | 同上 |
| `pull_request` | ❌ | 已移除（本项目无 PR 协作需求） |

> 5 路矩阵（Windows Portable + Windows 3.11/3.13 + Ubuntu 3.11/3.13）
> **继续保留在 workflow 文件里**，作为可选的独立云端验证能力；
> 但它**不再是** V1 发布门槛。本轮**不修改** `.github/workflows/*`，
> 也**不触发** `workflow_dispatch`。

### 7.4 普通开发任务的默认流程

```
改代码 → 本地 targeted tests → （按改动范围决定是否跑本地全量）→
git commit → git push origin/main → 结束（不等待 GitHub Actions）
```

⚠ **禁止**把「不等 CI」理解成「不测试」：普通任务**至少**要跑本次改动对应的 targeted tests；
涉及**核心启动 / 安装 / Data Contract / security** 时，必须跑对应回归测试。

### 7.5 汇报口径

- 普通任务汇报：本地相关测试 PASS/FAIL、本地全量测试（如运行）、Commit SHA、
  是否已 push、`GitHub Full CI：本任务未要求，未运行`。
- **阶段 Gate / Release 汇报**：以 **Windows Release Gate（§7.2）各项结果**为准；
  只有**额外**跑了云端验证时，才再附 5 路 CI 结果。

### 不在本策略内（不要顺手做）

新 CI 架构 / Fast CI / nightly CI / PR gate / branch protection / Release workflow /
GitHub Release / tag 自动发布 / artifact 上传。
