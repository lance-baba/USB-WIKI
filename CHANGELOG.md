# 更新日志

本文件记录**对使用者有影响**的变更。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。

### 修复
- **抓取的网页可能整页乱码**：对 `Content-Type` 未声明 `charset` 的响应（如
  docs.python.org），requests 会按 RFC 2616 默认成 `ISO-8859-1`，把中文与 em dash
  变成 `â\x80\x94` 这类乱码。原写法 `resp.encoding or resp.apparent_encoding` 里
  `ISO-8859-1` 是真值，`or` 直接短路，内容嗅探结果永远取不到。
  现改为自行判定编码：HTTP 头 charset → BOM → `<meta charset>` → 字节嗅探，
  且会先验证页面自述的 charset 真能解开，否则视为说谎并回落嗅探。
  影响面：Markdown 笔记与留存的原件 HTML 都会被污染，检索与预览同时受损。
- **删除笔记后原件不会回收**：导入时留存的原件（PDF / Office / 剪藏 HTML）可达数十 MB，
  笔记删除后它们成为孤儿，在 U 盘上无声堆积。现随同步周期自动回收，
  并保留安全阀 —— 若一份笔记都没有却存在原件，判定为笔记目录异常，拒绝删除。
- 依赖清单收敛为单一来源：`setup_runtime_windows.py` 改为读取 `requirements.txt`，
  不再各自维护一份（此前两份列表已经出现漂移——`onnxruntime` 在 txt 里是硬依赖，
  在安装脚本里却是可选，照 txt 装会无条件多出约 120MB）。
- 补齐 `requirements.txt` 缺失的 `pdfminer.six` 与 `pypdf`，使公开读者可按文档完成安装。
- 新增 `.gitattributes`，强制 `*.sh` / `*.command` 使用 LF 换行，
  避免在 Linux/macOS 上克隆后报 `bad interpreter: /bin/bash^M`。

### 变更
- 文档面向外部读者重写：`docs/交付说明.md` → `docs/设计与实现.md`，
  `docs/验收测试报告.md` → `docs/测试报告.md`，并剥离内部流程措辞
  （需求文档编号、验收编号、交付/验收等流程用语）。
- 新增 `CONTRIBUTING.md`、`SECURITY.md`、本文件，以及 GitHub Actions CI
  （Linux + Windows × Python 3.11 + 3.13，故意不装 `onnxruntime` 以验证降级链）。

## [1.2] — 2026-09-14

首个公开版本。

### 新增
- **混合检索**：FTS5 trigram 词法检索 + `sqlite-vec` 向量近邻，RRF 融合排序；
  查询短于 trigram 最小粒度时自动回落到 `LIKE`，保证短词零漏召。
- **多格式导入**：20+ 种格式统一转 Markdown 入库。其中 `.docx` / `.pptx` / `.xlsx` / `.epub`
  由标准库 `zipfile` + `xml.etree` 解析，**零额外依赖**；`.pdf` 走 `pdfminer.six`
  （主，保留段落结构）→ `pypdf`（后备）。
- **原版预览**：导入时把原件留存到 `data/originals/`。笔记页提供三种视图 ——
  渲染（Markdown→HTML）/ 原版（PDF 用浏览器内置查看器、网页用沙箱 iframe）/ 源码。
  原件服务支持 HTTP Range，大 PDF 可分段加载。
- **双模 AI 网关**：本地 Ollama 优先，云端 API 兜底；全部不可用时退化为纯 FTS5 检索回答，
  而不是报错。
- **离线知识星图**：`[[Wikilink]]` 强连线 + 向量余弦弱连线，阈值可调。
- **介质安全**：启动时 WAL 残留自愈、优雅退出检查点、exFAT 双指纹增量同步
  （针对 exFAT 时间戳精度不足导致的「等长编辑漏扫」做了专门加固）。

### 设计取舍
- **不引入 OmniParse 等依赖模型的方案**：其容器镜像 GB 级且需要 GPU，与
  「U 盘便携、零安装」冲突。判据是「要不要模型」而非「支持格式多不多」——
  因此 OCR / 语音转写明确不做，但办公格式全覆盖只多花了 26MB。
- **`onnxruntime` 保持可选**：缺少模型文件时它不提供任何能力，却让体积翻倍。
  不装也能用，嵌入源会自动降级。
- **告警按「是否需要用户行动」分级**：系统自愈过程信息不打扰用户。

### 已知限制
- 扫描件 PDF、图片、音视频不支持（需 OCR / ASR 模型）。
- 「写入过程中物理拔盘」这一破坏性场景需在真实 exFAT U 盘上人工验证，
  自动化测试只覆盖了逻辑等价场景。
- `local_hash` 兜底向量的召回质量有限，只是为了让 RRF 双路在极端降级下依然成立。

[未发布]: https://github.com/lance-baba/USB-WIKI/compare/v1.2...HEAD
[1.2]: https://github.com/lance-baba/USB-WIKI/releases/tag/v1.2
