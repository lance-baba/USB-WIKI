# USB-WIKI Data Contract V1

> 本文档定义**用户资料（Library）与程序（App）的边界与协议**。
> 任何修改持久化逻辑的变更，都必须先对照本文档。

## 1. 什么是 USB-WIKI Library

两个**完全不同**的东西：

| | USB-WIKI App | USB-WIKI Library |
| :--- | :--- | :--- |
| 内容 | 程序目录 / Python 运行时 / 依赖 / 模型 | 用户资料 |
| 可否删除 | **可以整个删除** | **不可以丢** |

终局目标：**只要 Library 文件夹还在**，即使程序、运行时、Ollama、模型、
SQLite、FTS、向量索引、配置全部丢失，最新版程序都能从它恢复出一个可工作的知识库。

## 2. 数据分类（A / B / C / D）

| 分类 | 含义 | 当前位置 |
| :--- | :--- | :--- |
| **A** | 不可丢失的用户数据 | `notes/` `originals/` `assets/` `snapshots/` |
| **B** | 可从 A 重建的派生数据 | `cache.db`（documents/chunks/FTS/vector/parent/doc_meta/sys_meta） |
| **C** | 软件运行配置 | `config.ini`（含 API Key，明文本地允许） |
| **D** | 临时数据 | `wiki-usb.log`、`temp_chrome_profile/` |

### 工程硬规则

> 一个字段如果不能从 Library 中重新生成，就**不能只存在可重建数据库里**。

**审计结论（2026-09-16）**：当前 `cache.db` 的所有表均为衍生数据，
**不存在「用户不可重建信息只存在 cache.db」**的情况 ✅

## 3. 目录结构

```
<LIBRARY_ROOT>/
├─ library.json        ← 格式识别与兼容性协议（最小信息）
├─ notes/              ← A：开放 Markdown（即使程序消失，记事本/Obsidian 也能读）
├─ originals/          ← A：用户上传原件（PDF/DOCX/EPUB… 转换成功也不删）
├─ assets/             ← A：网页归档资产（图片/CSS/字体 —— 网页未来可能消失）
├─ snapshots/          ← A：降级抓取的原始快照
├─ cache.db            ← B：可整库删除后从 notes 重建
└─ wiki-usb.log        ← D：可删
```

`<LIBRARY_ROOT>` 默认是 `<程序目录>/data`（向后兼容），可通过环境变量
`WIKIUSB_LIBRARY` 指向任意位置（另一块盘 / 另一台电脑）。

**禁止强行把资料搬到"好看"的目录结构** —— 边界靠抽象建立，不靠搬家。

## 4. 三个版本号（严格独立，禁止互相派生）

| 版本 | 当前值 | 含义 | 升级频率 |
| :--- | :--- | :--- | :--- |
| `APP_VERSION` | 1.3.0 | 用户拿到的软件版本，三段式 | 正常 |
| `SCHEMA_VERSION` | 1.4 | cache.db 索引结构版本 | 可较频繁（坏了删了重建） |
| `DATA_FORMAT_VERSION` | 1 | **永久用户资料**格式版本 | **极少改变** |

任何 `X = Y` 的赋值都是设计错误。
只修 UI Bug（1.3.0 → 1.3.1）时，SCHEMA 与 DATA_FORMAT **都必须不动**。

## 5. library.json（manifest）

```json
{
  "format": "usb-wiki-library",
  "data_version": 1,
  "created_by": "1.3.0",
  "created_at": "2026-09-16T14:30:00",
  "library_id": "0123456789abcdef"
}
```

**禁止**包含：API key / token / 绝对路径 / cache 状态 / 模型路径 / Ollama 状态 /
端口 / 机器名。它只是识别资料库格式与兼容性的最小协议，不是数据库、不是配置。

## 6. 路径规则

Library 内的**永久 metadata** 只允许**相对路径**：

```
✅ notes/hello.md
❌ C:\Users\xxx\...\notes\hello.md     （资料库可能整盘搬移）
❌ D:\USB-WIKI-Data\notes\a.md
```

实测当前语料与 `documents.rel_path` 均**无绝对路径** ✅

## 7. metadata 规则

用户人工行为（手工标签 / 收藏 / 自定义标题 / 备注 / 人工分类 / 重要标记）
**不能只写进 cache.db**，必须进入：

* Markdown frontmatter（首选），或
* `metadata/` 下的开放持久格式

## 8. 兼容规则

| 情形 | 行为 |
| :--- | :--- |
| 新程序 + 旧 DATA_FORMAT | 兼容则直接读；需升级则：**检测 → 备份需改的 metadata → migration → 验证 → 最后才更新 data_version** |
| **旧程序 + 新 DATA_FORMAT** | **拒绝写入**：「此资料库由更新版本的 USB-WIKI 创建，请升级程序后使用」—— 禁止旧程序降级资料 |
| 非本格式的目录 | 拒绝不擅动（用户可能指错了目录） |

**DATA migration ≠ DB migration**：Schema 坏了最坏情况删了重建；
**Data migration 动的是用户永久资料，绝不能随便删/重建** —— 所以
`DATA_FORMAT_VERSION` 必须极少改变。

## 9. 恢复规则（Core 层能力）

```
最新版程序
  → 指向 Library（library.json）
  → 检查 DATA_FORMAT_VERSION
  → 扫描 notes / originals / assets / metadata
  → 创建全新 cache.db
  → 建 FTS / chunks / parent blocks
  → 重新 embedding → 建 vector index
  → 恢复可用状态
```

Core 层已具备此能力（`_idx.rebuild_all`），本轮未做 UI。

## 10. 禁止的行为

* ❌ 任何写入 Library 的操作前不校验路径（必须过 `file_guard.safe_join`）
* ❌ 永久 metadata 保存绝对路径
* ❌ 把用户人工数据只写进 cache.db
* ❌ 旧程序打开新 DATA_FORMAT 时尝试降级
* ❌ library.json 里放 API key / 机器状态
* ❌ 因为「转换成功」而删除原件
* ❌ 静默覆盖同名文件（必须加唯一后缀）
