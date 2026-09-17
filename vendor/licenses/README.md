# vendor/licenses —— 源码侧许可原文库

## 为什么需要它

某些上游发行物（wheel / sdist）**不随附许可原文**，但许可义务仍在：分发者必须随产品提供
许可与版权声明。典型例子：`sqlite-vec` 的 wheel 里 `dist-info` 只有 `METADATA` /
`RECORD` / `WHEEL`，一个 `LICENSE` 都没有，而项目元数据明确写了双许可。

如果把「去上游抓许可」放在构建时做，就会让 **正式发布构建依赖网络、且不可复现**。
所以许可原文在这里**入库一次**，构建时只做**本地拷贝**。

## 目录结构

```
vendor/licenses/<package>/<version>/
├─ PROVENANCE.json      ← 必需：溯源与完整性声明
└─ <许可原文文件…>       ← 原字节副本，例如 LICENSE-MIT / LICENSE-APACHE
```

`<package>` 用发行名（大小写/连字符不限，匹配时按 PEP 503 规范化），`<version>` 必须与
随包运行时的实际版本完全一致 —— 版本不同就要另开一个目录，不许复用。

## PROVENANCE.json 必需字段

```json
{
  "format_version": 1,
  "package": "sqlite-vec",
  "version": "0.1.9",
  "upstream_project": "https://github.com/asg017/sqlite-vec",
  "upstream_ref": "v0.1.9",
  "upstream_commit": "<完整 commit sha>",
  "retrieved_at_utc": "YYYY-MM-DD",
  "declared_in_metadata": "MIT License, Apache License, Version 2.0",
  "note": "为什么需要 vendor（例如上游 wheel 未随附许可原文）",
  "files": [
    { "name": "LICENSE-MIT", "source_url": "…/blob/<sha>/LICENSE-MIT",
      "size": 1068, "sha256": "…" }
  ]
}
```

## 硬性规则

1. **原字节**。只允许把上游文件按字节落盘，**禁止手写、翻译、重排、补全或"整理"许可文本**。
   `files[].sha256` 就是这条规则的执行器：构建时会逐文件校验，改一个字节就红。
2. **`files` 必须列全**。声明的文件少一个、或 sha256 不符 → 该组件视为 `VENDOR_LICENSE_INVALID`，
   **strict 构建直接失败**（`inventory_complete=false`）。
3. **不联网**。构建脚本永不访问 GitHub；`source_url` 只用于人类审计与将来复核。
4. **不越权解释许可**。这里只记录上游声明（`declared_in_metadata`）与实际文件，
   不推断 SPDX 表达式——那属于法律判断，不在本项目范围。
5. 这些是 **KB 级纯文本**，可以进 Git。

## 新增一个组件的步骤

```bash
# 1) 解析 tag → commit（pin 到具体 commit 才是可复核的溯源）
curl -s https://api.github.com/repos/<owner>/<repo>/git/ref/tags/<tag>
# 2) 按 commit 取许可原文（原字节落盘，不经过任何改写工具）
curl -sL -o vendor/licenses/<pkg>/<ver>/LICENSE-XXX \
     https://raw.githubusercontent.com/<owner>/<repo>/<commit>/LICENSE-XXX
# 3) 计算 sha256 并写 PROVENANCE.json
# 4) python scripts/build_release.py --strict 复验
```
