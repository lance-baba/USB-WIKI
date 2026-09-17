# Wiki-USB · 随身第二大脑（独立版 v1.2）

> 承载于 U 盘 / 移动存储的 **Local-First 便携式个人知识库与混合 RAG 问答引擎**。
> 数据资产 100% 属于用户：文章与笔记以标准 Markdown 落在本地文件系统，SQLite 只做高性能索引缓存。

[![tests](https://github.com/lance-baba/USB-WIKI/actions/workflows/tests.yml/badge.svg)](https://github.com/lance-baba/USB-WIKI/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue)
![license](https://img.shields.io/badge/license-MIT-green)

📖 **文档**：[项目架构说明](docs/项目架构说明.md) ·
[设计与实现（含缺陷根因与体积实测）](docs/设计与实现.md) ·
[测试报告](docs/测试报告.md) ·
[更新日志](CHANGELOG.md) ·
[贡献指南](CONTRIBUTING.md) ·
[行为准则](CODE_OF_CONDUCT.md) ·
[安全策略](SECURITY.md)

---

## 1. 快速开始

### Windows（真正的零安装）
```text
1) 首次准备（需联网一次，约 1~3 分钟）
   python setup_runtime_windows.py

2) 之后任意电脑插入 U 盘，双击：
   启动-Windows.bat
```
浏览器会自动打开 `http://127.0.0.1:28765`。关闭控制台窗口或点击界面右上角「安全退出」即可安全拔盘。

> **发布包体积**：核心运行时 **122MB**（自包含 Python 解释器与全部依赖）。
> 最重的一项 `babel`（32MB）来自 `trafilatura` 依赖链上游的 `courlan` ——
> 它在 import 期就硬引用 `babel.Locale`，实测移除即崩，无法裁剪。
> 可选能力 `onnxruntime`（+约 120MB）已改为按需安装：`python setup_runtime_windows.py --with-onnx`。
> 未启用时嵌入源按 `local_onnx → ollama → api → local_hash` 逐级降级。

> **已实测**：`runtime/python-3.11-embed/python.exe tests/test_suite.py`
> 会输出一行机器可读的结果（`TOTAL=… PASS=… SKIP=… FAIL=…`）。
> 测试数量不再手写在文档里（它曾漂移过 48 项，且本身会随网络环境浮动）——完整状态见 GitHub Actions，本地跑 `python tests/test_suite.py` 会输出
`TOTAL=… PASS=… SKIP=… FAIL=…` 这一行稳定格式。
> （Python 3.11.9 + SQLite 3.45.1 + FTS5 + sqlite-vec 全部在自包含运行时内就绪）。

### macOS / Linux（极简环境引导）
```bash
chmod +x 启动-macOS.command 启动-Linux.sh
./启动-macOS.command          # macOS 双击亦可
./启动-Linux.sh               # Linux
```
需要宿主 Python 3.10+。脚本会自动检测依赖，缺失时给出单行终端安装提示。

### 命令行参数
```
--host 127.0.0.1     监听地址
--port 28765         起始端口（被占用时自动 +1 避让，最多 10 次）
--no-browser         不自动打开浏览器
--no-sync            不启动外部文件增量同步
```

---

## 2. 往知识库里添加内容（三种方式）

| 方式 | 操作 | 说明 |
| :--- | :--- | :--- |
| **① 拖文件进界面** | 「剪藏」页 → 把文件拖进虚线框（或点击选择文件） | **自动转成 Markdown 入库**，支持 20+ 种格式，见下表 |
| **② 抓网页** | 「剪藏」页 → 粘贴文章 URL → 「抓取入库」 | 静态博客优先，动态页面自动降级为快照 + 引导粘贴 |
| **③ 丢进文件夹** | 把 `.md` / `.txt` 直接复制到 `data/notes/` | 15 秒内自动索引。**配合 Obsidian 最顺**：把 `data/notes` 作为仓库打开即可 |

### 支持的格式（全部转成 Markdown 后建索引）

| 类别 | 格式 | 转换方式 |
| :--- | :--- | :--- |
| 文本与笔记 | `.txt` `.log` `.ini` `.conf` `.env` `.md` `.rst` `.org` | 直读（自动识别 UTF-8 / GB18030 / Big5） |
| 办公文档 | `.docx` `.docm` `.pptx` `.pptm` `.xlsx` `.xlsm` | 标准库解 OOXML：标题层级 → `#`，表格 → Markdown 表，PPT 按页拆分 |
| PDF | `.pdf` | `pdfminer.six` 主用（保留段落结构）+ `pypdf` 后备（**扫描件需 OCR，不支持**） |
| 网页与邮件 | `.html` `.htm` `.xhtml` `.mhtml` `.eml` | trafilatura 抽取正文 |
| 数据与配置 | `.csv` `.tsv` `.json` `.jsonl` `.yaml` `.yml` `.toml` `.xml` | CSV→表格，JSON/YAML→代码块，XML→层级大纲 |
| 代码 | `.py` `.js` `.ts` `.java` `.go` `.rs` `.c` `.cpp` `.sh` `.sql` 等 50+ | 包成对应语言的代码块 |
| 电子书 | `.epub` | 按 spine 顺序抽取全部章节 |
| 字幕 | `.srt` `.vtt` `.ass` `.lrc` | 去掉时间轴只留台词 |
| RTF | `.rtf` | 有损提取可见文字 |

> **明确不支持**：旧版二进制 Office（`.doc` `.ppt` `.xls`）、图片、音视频 ——
> 这些都需要 OCR / 语音转写**模型**，单是模型就会让发布包从 122MB 涨到 GB 级，
> 与本项目「U 盘便携、零安装」的定位冲突。请先用 Office/WPS 另存为新格式。
> 完整的方案取舍（哪些格式必须用模型、哪些用标准库就能解）见[设计与实现](docs/设计与实现.md)。

### 原版预览（笔记页三种视图）

> **离线存档**：剪藏网页时会把该页的 CSS / 图片 / 字体一起抓进本地资源池
> （`data/assets/`，按 URL 哈希去重），因此「原版」**断网也能完整还原版式**，
> 浏览过程不会向任何外部站点发请求。体积护栏：单资源 ≤ 512KB、单页 ≤ 5MB，
> 可在 `config.ini` 的 `[CRAWLER]` 调整；超限的图片以占位代替并保留原始尺寸。
> 脚本不下载（预览沙箱本就禁用脚本）。

导入时会**把原件留存到 `data/originals/`**，「笔记」页右上角可在三种视图间切换：

| 视图 | 内容 | 用途 |
| :--- | :--- | :--- |
| **渲染** | Markdown 渲染成 HTML（标题层级 / 表格 / 列表 / 代码块） | 日常阅读，告别「一整块等宽文本」 |
| **原版** | PDF → 浏览器**内置 PDF 查看器**；网页 → 沙箱 iframe 还原版式；图片 → 直接显示 | 还原原始观感。网页按 `sandbox=""` 隔离，脚本/表单/弹窗全部禁用 |
| **源码** | 原始 Markdown 文本 | 检查转换结果、手工修正 |

- 原件服务支持 **HTTP Range**（`206 Partial Content`），大 PDF 可分段加载、快速翻页。
- 中文文件名按 **RFC 6266** 双字段编码（ASCII 回退名 + `filename*=UTF-8''…`），
  不会因 HTTP 头只能 latin-1 编码而失败。
- 纯 Markdown / 纯文本导入不留存原件（笔记即原件），此时「原版」按钮自动置灰。
- 原件与笔记同 stem，笔记被重命名后仍能反查到位。

> 真相源是 `data/notes/*.md`，`data/cache.db` 只是可随时重建的索引。
> 你也可以直接用 Obsidian / Typora / VS Code 读写这些 Markdown 文件，系统会自动增量同步。

## 3. 目录结构

```
Wiki-USB/
├── 启动-Windows.bat            # Windows 入口（UTF-8 编码锁死 + 内置环境静默呼出）
├── 启动-macOS.command          # macOS 入口（权限与依赖检测）
├── 启动-Linux.sh               # Linux 入口
├── setup_runtime_windows.py    # 嵌入式运行时安装器 / 体检器 / 发布校验器 / 垃圾清理
├── config.ini                  # 主配置（明文；首次运行自动生成）
├── requirements.txt
├── runtime/                    # [仅 Windows 发布版] 嵌入式 Python 3.11 + 预编译依赖
│   ├── python-3.11-embed/
│   │   ├── python.exe
│   │   ├── python311._pth      # 关键补丁：解除 import site 屏蔽 + 相对依赖目录
│   │   └── Lib/site-packages/  # sqlite_vec / onnxruntime / trafilatura / lxml / pyyaml
│   └── models/                 # 本地 ONNX 嵌入模型（可选）
├── app/
│   ├── launcher.py             # 自愈启动器：端口避让 / WAL 自愈 / 优雅退出钩子
│   ├── server.py               # 标准库 HTTP 服务与路由分发（含 SSE 传输层）
│   ├── api/                    # 业务 API 域模块（薄分发，协议零变更）
│   │   ├── system.py           # /api/status · /api/system/{rebuild-index,rebuild-vectors,shutdown}
│   │   ├── config.py           # /api/config（GET 脱敏 · POST 保存）
│   │   ├── diagnostics.py      # /api/diagnostics
│   │   ├── search.py           # /api/search · /api/topics · /api/graph
│   │   ├── ask.py              # /api/chat/completions(SSE) · /api/ai/test · /api/ollama/models
│   │   ├── library.py          # /api/notes* · /api/notes/import · /api/import/formats
│   │   └── capture.py          # /api/capture/{url,duplicate}
│   ├── core/
│   │   ├── paths.py            # 相对路径解析（杜绝盘符绑定）
│   │   ├── config.py           # config.ini 读写与默认模板
│   │   ├── db.py               # SQLite Schema / WAL 自愈 / 签名守卫
│   │   ├── chunker.py          # Parent-Child 切片器
│   │   ├── embedder.py         # 嵌入源解析 + AVX2 指令集防护
│   │   ├── search.py           # FTS5 Trigram + 短词 LIKE 降级 + 向量 + RRF
│   │   ├── indexer.py          # 文档级索引流水线 / 孤儿切片回收
│   │   ├── crawler.py          # 网页抓取与降级矩阵
│   │   ├── llm.py              # 双模 AI 网关 + SSE 流式
│   │   ├── sync.py             # exFAT 双指纹增量同步
│   │   ├── graph.py            # 知识星图数据构建
│   │   ├── net_util.py         # 标准库 HTTP（代理 / certifi / UTF-8）
│   │   └── context.py          # 应用上下文装配
│   └── web/
│       ├── index.html          # 单文件控制台（零框架）
│       └── vendor/d3.v7.min.js # 完全离线静态库
├── data/                       # 用户核心资产
│   ├── notes/                  # 唯一真相源：手写笔记与剪藏 Markdown
│   ├── snapshots/              # 降级页面的原始 HTML 快照
│   └── cache.db                # 衍生索引（可全量重建）
└── tests/
    ├── test_suite.py           # 自动化测试统一入口（数据目录整体隔离，不碰真实知识库）
    └── smoke_core.py           # 核心链路冒烟
```

---

## 4. 核心能力

| 能力 | 实现要点 |
| :--- | :--- |
| **开箱即用** | Windows 内置嵌入式 Python 3.11 + 预编译轮子，不写注册表、不污染宿主环境 |
| **双轨 AI** | `auto` 自动探测 Ollama（60s 健康缓存）→ 云端 OpenAI 兼容 API → 纯离线 FTS5 检索，永不崩溃 |
| **混合 RAG** | FTS5 Trigram 词法 + `sqlite-vec` 向量近邻 + RRF 倒排融合，Top-5 父分块送入 Prompt |
| **短词召回** | 单汉字 / `AI` / `C#` 等短查询自动降级 `LIKE '%kw%'`，召回率 100% |
| **精确溯源** | 回答携带 `[^n]` 角标，悬停展示父分块来源文件与原文摘要 |
| **外部编辑器共存** | 15s 轮询 + exFAT 双指纹（含等长编辑哈希比对），Obsidian/Typora 随意读写 |
| **离线星图** | `[[Wikilink]]` 强连线 + 向量余弦弱连线，阈值滑块 0.70~0.95 化解「毛线团」 |
| **介质安全** | WAL + `synchronous=NORMAL` 抑制写放大；启动自愈 + 优雅退出保证拔盘后主库自洽 |

---

## 5. 配置要点（`config.ini`）

```ini
[AI]
provider = auto                      ; auto / ollama / api / offline
api_base_url = https://api.deepseek.com/v1
api_key =                            ; 明文存储，公共设备请勿留存高余额 Key
api_chat_model = deepseek-chat       ; 云端接口的模型名（仅在用云端时生效）
ollama_host = http://127.0.0.1:11434
ollama_chat_model =                  ; 留空＝未选择：由你在「设置 → AI」里挑本机模型
embedding_source = local_onnx        ; local_onnx / ollama / api / local_hash
embedding_dim = 512
```

### AI 能力分三层，缺哪层都不影响基础使用

| 层 | 能力 | 依赖 |
| :--- | :--- | :--- |
| **Level 1 基础知识库** | 启动 / 导入 / 查看 / FTS 检索 / 返回引用 | **无任何 AI 依赖** |
| **Level 2 本地语义检索** | 向量召回（`local_onnx` → `ollama` → `api` 逐级降级） | 嵌入源可用；不可用则退化为纯 FTS5 |
| **Level 3 生成式对话** | 自然语言总结回答 | **你选择的**本地 Ollama 模型，或已配置的云端 API |

**对话模型没有默认值。** 程序不会替你预设、也不会自动挑第一个本机模型 ——
`ollama_chat_model` 留空时状态为 `selection_required`，由你在设置页显式选择。
就绪状态一览：`no_runtime`（没装 Ollama）/ `no_model`（Ollama 在线但无模型）/
`selection_required`（有模型未选）/ `model_missing`（选过的模型被删）/
`ready`（可对话）。只有 `ready` 时 `provider` 才会是 `ollama`。

> **没有可用对话模型时**：程序**不会**报错或阻塞 —— 知识库检索、引用、导入全部照常，
> 问答自动转为「纯离线检索回答」（`provider=offline`），并明确标注未调用大模型。

> **向量空间签名守卫**：库内 `sys_meta.embedding_signature` 会记录嵌入模型与维度。
> 一旦在配置里更换模型或维度，控制台将常驻黄色告警并**强制阻断向量召回**，
> 需点击「设置 → 重建向量索引」后恢复。

---

## 6. 测试

```bash
python tests/test_suite.py     # 切片/检索/网关/主题分组/破坏性/数据安全/原子写/结构升级/安全边界
                               # 末行输出 TOTAL/PASS/SKIP/FAIL；离线时网络用例记 SKIP，总数不变
python tests/smoke_core.py     # 核心链路冒烟（4 组查询）
python setup_runtime_windows.py --check           # 运行时体检（解释器 / _pth / 依赖包能否跑起来）
python setup_runtime_windows.py --verify          # 发布完整性校验（答「这个 U 盘能不能交付」）
python setup_runtime_windows.py --verify --json   # 同上，输出 JSON 供 CI 消费
python setup_runtime_windows.py --clean           # 清理工程垃圾（__pycache__ / .pyc）
python setup_runtime_windows.py --clean --dry-run # 只预览将被清除的内容，不实际删除
```

测试覆盖 TC-01 / 03 / 04 / 05 的可自动化等价场景；
TC-02（写入中物理拔盘）需在真实 exFAT U 盘上人工执行。

---

## 7. 安全与合规提示

- `config.ini` **明文**存储密钥，且随 U 盘移动 —— 公共借出设备请先清空 `api_key`。
- 抓取仅面向静态博客与科技文章；动态 SPA / 强反爬站点触发透明降级，不做反爬对抗。
- 所有网络访问仅在你主动点击「抓取」或发起问答时发生；其余时间完全离线。
