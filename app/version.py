"""应用版本的**唯一来源**。

## 为什么必须唯一

此前 `1.2` / `WikiUSB/1.2` / `v1.2` / `WikiUSB/1.3` 散落在 launcher 横幅、
HTTP Server 头、前端品牌栏、User-Agent 里，各自演进 —— 用户看到的版本号
与真实版本已经对不上（应用自报 1.2，而索引结构早就是 1.4）。

## 与应用版本 / 索引结构版本的关系（**不可混淆**）

    APP_VERSION    用户拿到的软件版本      标准三段式  MAJOR.MINOR.PATCH
    SCHEMA_VERSION cache.db 的结构兼容版本  允许两段    1.4

两者**语义完全不同，必须分开演进**：

* `1.3.0 → 1.3.1` 可能只修了 UI Bug，索引结构一个字都没变 → Schema 不动
* 某次只调整索引结构（如新增派生表）→ Schema 单独 +1，软件版本可以不动

因此这里**不导入、也不派生** `SCHEMA_VERSION`；它继续住在
`app/core/migrations.py`。任何 `SCHEMA_VERSION = APP_VERSION` 式的耦合
都会让「只修 UI」变成一次全库重建。

## 谁该读这里

运行时代码（launcher / server / 前端 / User-Agent）与未来的构建发布脚本
（ZIP 名、Git Tag、BUILD_INFO）都从这里取。
README 与 CHANGELOG 是**文档**，不是运行时版本源，也不允许被程序解析。
"""

from __future__ import annotations

APP_VERSION = "1.3.0"

# 便于构建脚本与 User-Agent 复用，避免各处自己拼
APP_NAME = "Wiki-USB"
APP_NAME_CN = "随身第二大脑"
# HTTP Server 头 / User-Agent 用的紧凑形式
USER_AGENT_TOKEN = f"WikiUSB/{APP_VERSION}"


def version() -> str:
    """函数式取法（给不方便引常量的地方用）。"""
    return APP_VERSION


def version_tag() -> str:
    """Git Tag / 发布包名用：``v1.3.0``。"""
    return f"v{APP_VERSION}"


def release_zip_name(platform: str = "win-x64") -> str:
    """未来 Release 包名：``USB-WIKI-v1.3.0-win-x64.zip``。

    本轮不做发布流水线，但先把命名规则钉在版本源里，
    免得将来又变成「构建脚本里再硬编码一次」。
    """
    return f"USB-WIKI-v{APP_VERSION}-{platform}.zip"


# 说明（A3）：BUILD_INFO.json 的**生成器**是 `scripts/release_integrity.py`
# （build_info / write_build_info），它按 `from app.version import APP_VERSION` 取值 ——
# 即本文件仍是 app_version 的唯一来源。
#
# 这里刻意**不再**保留第二份 `build_info()` 结构体：两处各定义一个 BUILD_INFO 形状，
# 迟早出现「构建脚本写 A、诊断/测试按 B 读」的漂移。结构体只允许有一个定义处。

