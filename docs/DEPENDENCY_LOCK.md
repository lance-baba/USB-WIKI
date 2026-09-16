# 依赖锁定（Dependency Release Lock）

> 目标：**同一个 USB-WIKI Release，在未来重新构建时仍能得到同一套 Python 依赖。**

## 两份清单，职责不同

| 文件 | 职责 | 何时用 |
| :--- | :--- | :--- |
| `requirements.txt` | **直接依赖 + 兼容区间**（`trafilatura>=1.9.0`） | 开发安装（浮动解析） |
| `requirements-release.lock` | **完整依赖闭包 + `==` 精确锁定**（含 environment marker） | 发布构建 / CI（可复现） |

`requirements.txt` 只列主动依赖，不塞传递依赖；`requirements-release.lock` 由生成器从它解析出整棵闭包并钉死版本。

## 生成（可复现，不手工编辑）

```bash
# 默认用产品运行时（Windows Embedded Python 3.11）做解析 → 锁自动落在 3.11 地板
python scripts/lock_dependencies.py

# 仅校验现有 lock 与 requirements.txt 一致（不写盘）
python scripts/lock_dependencies.py --dev
```

生成器逻辑（`scripts/lock_dependencies.py`）：

1. 在 **嵌入式 3.11.9** 上用 `pip-tools` 的 `pip-compile` 解析 `requirements.txt`
   → 输出带 marker 的 `==` 锁定。落在 3.11 保证 3.11 兼容，进而兼容 3.13。
2. 过滤构建期工具（`pip` / `setuptools` / `wheel` 等，非产品运行依赖）。
3. 写元信息头（解析解释器 / 时间 / pip-tools 版本）。
4. 计算最终文件 SHA256 由运行时（diagnostics）报告，不固定在头里以免与正文漂移。

生成期用到的 `pip-tools` 装在临时目录并按 site-packages 快照差分卸载，
**不会污染** `runtime/python-3.11-embed`（它已被 gitignore）。

## 安装

```bash
# 发布构建 / CI（推荐，可复现）
pip install -r requirements-release.lock

# 开发（想要最新兼容版本）
pip install -r requirements.txt
```

- `setup_runtime_windows.py`：发布构建**默认读 lock**；`--dev` 才用 `requirements.txt`。
- 五路 CI（Win Portable / Win 3.11 / Win 3.13 / Ubuntu 3.11 / Ubuntu 3.13）
  全部从 `requirements-release.lock` 安装并跑 578+ 测试。

## 约束（实现约定）

- 锁只管 **USB-WIKI Core 的 Python 依赖闭包**；不锁 `Ollama` / `GGUF` / `ONNX` 模型
  （那些属于将来的 Offline Distribution Manifest）。
- 不锁 `pip` / `setuptools` / `wheel` / `pytest` / `Codex` / `WorkBuddy` / 本机工具。
- `requirements.txt` 的每个直接依赖都必须在 lock 中 `==` 精确锁定
  （由 `tests/test_suite.py::test_dependency_consistency` 强制）。
- 离线 wheelhouse（`/wheels` + `--no-index`）是下一步，本轮只解决**版本可复现**。

## 验证「重新构建一致」

CI 的五路安装即是验证：同一份 lock 在 Win/Linux × 3.11/3.13 上都能装出同一闭包。
本地可单独核验 3.11 闭包：

```bash
python runtime/python-3.11-embed/python.exe -m pip install \
  --target /tmp/verify -r requirements-release.lock
```
