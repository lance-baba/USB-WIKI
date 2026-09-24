# USB-WIKI · Collections & Library Management Layer（ADR / POST_V1 Architecture Freeze）

> **状态：架构冻结（Design Freeze）｜未实现｜本轮仅落档**
>
> 本文档只做**架构设计、数据契约设计、产品边界与 ADR 落档**。
> 本轮**没有**修改生产行为、DB schema、检索代码、前端生产 UI、真实 Library 数据，
> 也**没有**建立任何 migration。
>
> 一句总纲：**Collections organize knowledge. Retrieval finds knowledge.**
> （资料集负责整理，检索系统负责找答案。）

---

## 1. 产品目标

USB-WIKI 的定位**不是**「分类文件柜」，而是：

```
个人全能知识库
  + 全库搜索
  + 证据可追溯问答
  + 可选的资料组织层
```

核心产品原则：

> **「整理是可选的，找到答案是强制的。」**

因此 Collections（资料集）只能帮助用户**管理与浏览**资料，
绝不能成为默认 Retrieval 的硬边界。

目标（POST_V1）：

- 文档多了以后可按需归类、按类浏览、批量管理；
- 不归类也**完全不影响**搜索与问答；
- 用户手工创建的组织信息（资料集、成员关系、标签）**永不丢失**（durable）。

---

## 2. 非目标

明确**不做**：

- 不做「query → 自动判断分类 → 只搜这个分类」的架构（见 §4、§14）；
- 不做固定 `category` 枚举（技术/工作/生活/学习…）—— 不内置强制分类体系（见 §3、§19）；
- 不替 Retrieval 做「理解问题」：Collections 不负责召回/排序/证据/回答；
- 不为每个 Collection 建独立索引或复制文档（见 §9「不要建立分类孤岛」）；
- 不在本轮实现任何功能代码、UI、migration。

---

## 3. Terminology（三层语义必须分开）

| 概念 | 性质 | 谁决定 | 示例 | 数量关系 |
| :--- | :--- | :--- | :--- | :--- |
| **`doc_type`** | **系统来源属性** | 系统写入 | `imported` / `web_capture` / `manual` | 1 篇 = 1 个 |
| **`collections`** | **用户组织层** | 用户创建/维护 | 工程监测 / Town World / 投资研究 / 家庭资料 / AI 项目 | 1 篇 = **0 / 1 / 多个**（many-to-many） |
| **`tags`** | **细粒度主题** | 用户创建/维护 | 基坑 / 水准仪 / Three.js / Unity / ETF / Blender | 1 篇 = 多个 |

规则：

- `doc_type` **继续只表示来源类型**，**不承担内容分类职责**（现状即如此，保持不变）；
- 系统**不得预设强制分类体系**；示例资料集只能作为 onboarding 模板，且**可删除 / 可改名 / 完全可自定义**；
- 一篇文档允许 `collections = []`。

### Collections 与 Tags 的区别（避免未来做成重复功能）

- **Collection** = 人为组织的**大容器 / 项目 / 领域**（粗粒度）；
- **Tag** = 细粒度**主题标记**（细粒度）。

例：Collection `工程监测`，Tags `基坑 / 水准仪 / 沉降 / 周报`。

---

## 4. Global Retrieval 原则（默认全库，铁律）

默认检索路径必须是 **GLOBAL RETRIEVAL**：

```
所有可搜索文档
  → FTS / LIKE
  → Vector
  → Fusion / Ranking
  → Evidence
  → Answer
```

**Collections 默认不改变候选集。**

反例（产品级风险，严禁）：

```
用户问「某游戏的伤害公式」
  → 系统误判为「生活」
  → 真正答案在「Unity 项目」资料中
  → 被分类过滤掉 → 漏召回 → 错答/拒答 → 用户认为知识库不可靠
```

> **USB-WIKI 默认不得因为系统自动分类而排除任何文档。**

即使系统把某篇资料建议分为「生活」，该资料只要**文本相关，仍必须能被召回**。

---

## 5. Explicit Scope 契约

**只有用户明确表达 scope 时，才允许硬过滤。**

| 场景 | 是否硬过滤 |
| :--- | :--- |
| 用户普通提问 | ❌ 全局（GLOBAL） |
| 用户进入「工程监测」资料集 | ❌ 仍全局（浏览 ≠ 限制检索） |
| 用户进入资料集**并主动选择「仅搜索当前资料集」** | ✅ 允许 `collection_id` 作为 retrieval filter |

这个概念必须严格区分：

- **`USER-EXPLICIT SCOPE`** —— 用户显式选择，**允许**硬过滤；
- **`SYSTEM-INFERRED SCOPE`** —— AI / 关键词 / 分类器推断，**禁止**硬过滤。

未来 UI 若提供「仅搜索当前资料集」，必须是**显式开关**，且默认关闭。

---

## 6. 系统自动判断只能做 Suggestion

未来可以有 **Auto Collection Suggestion**：

```
导入资料后 → 建议加入：工程监测 / 基坑
用户选择：接受 / 修改 / 忽略
```

系统建议**不得**：

- 自动隐藏资料；
- 自动限制 Retrieval；
- 静默改变搜索范围。

**自动分类错误时，核心搜索能力必须完全不受影响。**

---

## 7. Many-to-many 模型（正式冻结）

> **Document ↔ Collection = many-to-many**

例：

```
「Unity 城市旅游游戏方案」
  ↳ Collection: 游戏开发
  ↳ Collection: 城市文旅项目
  ↳ Collection: Unity
```

**不强迫用户选择唯一分类。**

概念结构：

```
Document A
  ↳ Collection X
  ↳ Collection Y
  ↳ Tag 1
  ↳ Tag 2
```

底层文档**仍只有一份**。

---

## 8. Durable metadata 原则

用户创建的以下信息属于 **NON-RECONSTRUCTABLE USER STATE**：

- Collection、Collection 名称
- Collection membership
- Tags
- 用户手工修改的分类

因此**必须进入 durable Library**，不得只存在于：

- ❌ `cache.db`（可重建 / 可删）
- ❌ 其它 derived SQLite
- ❌ browser localStorage

依据 `docs/DATA_CONTRACT_V1.md`：

- §2 分类 **A** = 不可丢失的用户数据（`notes/ originals/ assets/ snapshots/`）；
- §2 分类 **B** = 可从 A 重建的派生数据（`cache.db`）；
- §2 工程硬规则：**「一个字段如果不能从 Library 中重新生成，就不能只存在可重建数据库里」**；
- §7 已明确：用户人工行为（含**人工分类**）必须进入 Markdown frontmatter **或 `metadata/` 下的开放持久格式**；
- §9 恢复流程已包含扫描 `notes / originals / assets / metadata`。

**结论：Collections 属于 A 类 durable 数据。**

### 8.1 现状审计：`metadata/` 目录

| 检查项 | 结果 |
| :--- | :--- |
| `app/core/paths.py` 是否定义 `METADATA_DIR` | ❌ **没有** |
| `<Library>/metadata/` 是否存在 | ❌ **不存在** |
| Data Contract 是否已把 `metadata/` 写成合法 durable 层 | ✅ **是**（§7 与 §9） |

→ `metadata/` 是**契约已预留、实现尚未建立**的位置，本轮**只设计、不建立**。

---

## 9. Storage 方案比较

| 方案 | 做法 | 优点 | 缺点 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **A. Library metadata sidecar** | `metadata/` 下存 collections / membership / tags（开放格式，如 JSON） | 不动 `notes/*.md`（不改 hash、不制造 diff、不影响同步与恢复）；集中、便于批量与导出 | 需运行时解析；rename/move 时依赖文档身份映射（见 §10） | ✅ **推荐** |
| **B. Markdown frontmatter** | 给所有 `notes/*.md` 加 `collections:` / `tags:` | 自描述、随文件走、Obsidian 可见 | **批量改写用户 Markdown**：改变文件 hash、制造大量 diff、影响原始内容稳定性、增加恢复/同步复杂度 | ⚠️ 可作为**可选**补充，但**不得默认批量写回** |
| **C. cache.db / localStorage** | 存进现有索引或浏览器 | 实现最省事 | 违反 Data Contract（可重建即可删，用户人工状态会丢） | ❌ **禁止** |

**最终推荐：方案 A（Library metadata sidecar）优先**，
并把 B 作为「未来可选、且必须用户显式触发」的补充。

> 不能为了分类而复制正文。

---

## 10. Document Identity 审计（先审计，不新造 ID）

按本轮要求：先审计现有系统到底有哪些稳定身份，**不新造第二套 ID 系统**。

实测（`app/core/chunker.py` / `app/core/indexer.py` / `app/core/db.py`）：

| 候选身份 | 定义 / 生成 | 抵御 rename/move | 抵御内容编辑 | 现状 |
| :--- | :--- | :--- | :--- | :--- |
| **`doc_id`** | `chunker.doc_id_for(rel_path)` = `sha1(rel_path)[:16]` | ❌ **变化** | ✅ 稳定 | `documents` 主键；`chunk_id` / `parent_id` 全部派生自它 |
| **`rel_path`** | 相对 `data/` 的 POSIX 路径（如 `notes/a.md`），`documents` 表 UNIQUE | ❌ 变化 | ✅ 稳定 | 人类可读，UI / API 广泛使用 |
| **`documents.sha1`** | ⚠️ **实为 `prefix_hash`**（前 4KB 快速指纹，exFAT 等长编辑第二指纹） | ✅ 稳定 | ❌ 内容变即变 | 字段名 `sha1` 有误导性（存的是前缀指纹，不是全文哈希） |
| `chunk_id` / `parent_id` | `doc_id:c<n>` / `doc_id:p<n>` | ❌ 变化 | — | 派生索引，不承载用户状态 |
| `library.json.library_id` | 库级标识 | — | — | **库**身份，非**文档**身份 |

### 审计结论

> **现有身份中，没有任何一个能同时抵抗 rename/move 与内容编辑。**

- `doc_id` 与 `rel_path` 都是**路径派生** → 文档改名/移动即失效；
- `documents.sha1`（前缀指纹）抗 rename 但**不抗内容编辑**；
- 因此 **Collection membership 现阶段引用 `doc_id`（现有最可用身份）**，
  但 rename/move 会导致成员关系失效 ——
  **「需要 rename/move 稳定的文档身份」正式列为未来 migration requirement（Phase 1 前置项）**，
  本轮**不改 schema**。

---

## 11. Future Retrieval API（scope 契约）

现状（`app/core/search.py`）：

```python
def hybrid_search(
    db: Database,
    embedder,
    query: str,
    top_k_parents: int = 5,
    candidates: int = 20,
    debug: dict | None = None,
) -> SearchResult:
```

未来契约（**仅设计**）：

```python
scope = {"mode": "global"}                                  # 默认
scope = {"mode": "collection", "collection_ids": [...]}     # 仅用户显式选择
```

关键规则：

- **没有显式 scope ⇒ global**；
- AI / keyword / classifier 推断**不得**偷偷把 `global` 改成 `collection-only`；
- 未来若有自动意图判断，**最多作为 `boost` / `suggestion`**，不能作为 exclusion filter。

### Ranking 与 Collection

POST_V1 第一阶段：**Collection 不参与 ranking**（只做管理 / 浏览 / 显式 scope）。
未来如需要 `collection affinity`，**只能作为弱 boost**，
不得因为「不属于匹配 Collection」而 drop candidate。

---

## 12. Library Management Layer

Collections 与文档管理**统一设计**，不做成孤立功能。

未来导航：

```
Library
├── 全部资料
├── 资料集
│   ├── Collection A
│   ├── Collection B
│   └── ...
├── 标签
├── 未整理
└── 回收站
```

资料列表未来支持：

- 多选
- 加入资料集 / 移出资料集
- 添加标签
- 批量改标签
- 导出
- 移入回收站

### 未整理资料

允许 `collections = []`，UI 可展示为「未整理」。
但「未整理」**只是管理视图**：

- 未整理文档**必须**参与全库搜索；
- **必须**参与全库问答；
- **必须**参与 citation / evidence；
- **不得**成为二等资料。

---

## 13. Trash / Delete / Export

### Delete

未来删除**不得**默认 `unlink` durable note。

正确产品语义：

```
Delete → Move to Trash
```

- 回收站属于 **durable user state**；
- 必须支持**恢复**与**永久删除**；
- **永久删除必须由用户显式触发**；
- 严禁后台因 `orphan` / `unreferenced` / `unused` / `classification cleanup` 自动删除用户 durable data。

延续现有铁律：

> Derived data may be rebuilt/deleted.
> Durable user data must never be auto-deleted merely because it looks orphaned.
> （与本项目「永不自动删除用户原件 / orphan originals 只报告不删除」一致。）

### Export

未来至少考虑：

| 粒度 | 内容 |
| :--- | :--- |
| 单篇导出 | `.md` + 原件 |
| 批量导出 | ZIP |
| Collection 导出 | 该资料集内所有资料 + 必要 metadata |

**导出不得改变 Library。**

---

## 14. Auto Classification Suggestion（边界）

见 §6。补充边界：

- 建议是**可拒绝的**（接受 / 修改 / 忽略）；
- 建议错误时，默认检索**完全不受影响**；
- 建议**不得**自动写入 membership（需用户确认，或明确提供「自动应用」开关且默认关闭）。

---

## 15. Migration 风险

| 风险 | 说明 | 缓解（未来实施时） |
| :--- | :--- | :--- |
| **rename/move 导致 membership 失效** | `doc_id` 由路径派生（§10） | 引入 rename/move 稳定身份；或提供「重新关联」修复入口；**列为 Phase 1 前置 migration requirement** |
| **sidecar 与 notes 两处真相不一致** | 文件被外部编辑器改名/删除 | 扫描时以 `notes/` 为准，membership 中失效项**只标记不删除** |
| **旧程序遇到新 `metadata/`** | 可能误判/降级 | 按 Data Contract §8：旧程序对新 DATA_FORMAT **拒绝写入**；未知目录/文件应忽略而非降级 |
| **`DATA_FORMAT_VERSION` 是否 bump** | 新增 durable 目录属用户资料格式变化 | 建议引入 `metadata/` 时按 §8 流程（检测 → 备份 → migration → 验证 → 更新 version）处理；**本轮不执行** |
| **导出/恢复要带上 metadata** | 否则资料集丢失 | 备份/恢复/导出必须包含 `metadata/` |

---

## 16. POST_V1 分阶段实施路线（仅规划，不执行）

| 阶段 | 内容 | 可独立发布 |
| :--- | :--- | :--- |
| **Phase 1** | Collections metadata + CRUD；文档加入 / 移出 Collection；全部资料 / Collection 浏览（**前置：解决 §10 的 rename/move 稳定身份**） | ✅ |
| **Phase 2** | 显式 Collection scope search（`USER-EXPLICIT SCOPE`） | ✅ |
| **Phase 3** | Tags + 批量管理（多选、批量改标签） | ✅ |
| **Phase 4** | Trash + restore + export | ✅ |
| **Phase 5** | Auto Classification Suggestion（**仅建议**，不影响默认 Retrieval） | ✅ |

每一阶段可独立发布；**本轮不开始 Phase 1**。

---

## 17. 不可违反的 Invariants（验收标准）

1. **Default retrieval is GLOBAL.**
2. **Collections never exclude documents unless the user explicitly selects a scope.**
3. **Automatic classification may suggest, but never silently restrict retrieval.**
4. **Unclassified documents remain fully searchable.**
5. **A document may belong to multiple Collections.**
6. **Collections are durable user metadata, not derived cache.**
7. **No duplicated document copies per Collection.**
8. **No per-Collection physical search database / index.**
9. **Deleting a Collection must not delete its documents.**
10. **Durable user data is never automatically deleted because it appears unused / orphaned.**

---

## 18. 本轮变更范围声明

| 项 | 本轮是否发生 |
| :--- | :--- |
| 修改生产代码（search / chunker / embedder / indexer / API / UI） | ❌ 否 |
| 修改 DB schema / 新建 migration | ❌ 否 |
| 修改真实 Library（notes / originals / assets / cache.db） | ❌ 否 |
| 新增/修改文档（本文件 + 冻结文档里一条 POST_V1 指针） | ✅ 是 |
| 实现任何 Collection / Tag / Trash / Export 功能 | ❌ 否 |
