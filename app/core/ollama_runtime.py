"""Ollama 运行时的「找得到 + 拉得起」——供启动时**静默自启**使用。

为什么需要它
============
产品的本地 AI 全押在 Ollama 上，但 Ollama 默认不是一个「开机自启的系统服务」：
用户不点开那个托盘程序，`/api/tags` 就连不上，于是「本地对话 / 向量检索」全部
降级 —— 用户看到的是「尚未配置」，很难自己想到「先去开 Ollama」。

因此：启动时探测一次，连不上就**在后台把 `ollama serve` 拉起来**，用户无感。

铁律（与产品既有降级哲学一致）
==============================
* **绝不阻塞启动**：本模块的 ``ensure_running`` 是有界的（带超时），调用方应放进
  后台线程，不要卡在 UI 启动路径上。
* **找不到 / 起不来一律如实返回**，由调用方按降级链继续 —— 不抛异常给启动流程。
* **不猜路径**：只认「PATH」与 Ollama 官方安装位置；找不到就是找不到，不去全盘扫描。
* **只在 Windows 上做静默**（本项目 V1 仅 Windows）；其它平台只探测不拉起。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from . import net_util
from . import proc as proc_mod

#: 拉起后等待就绪的上限（秒）。冷启动含加载运行时，给足但不无限等。
DEFAULT_WAIT_S = 25.0
#: 轮询间隔（秒）
_POLL_S = 0.5


def find_ollama_exe() -> Path | None:
    """定位 ollama 可执行文件：PATH → Ollama 官方安装目录。找不到返回 None。

    刻意**不做全盘搜索** —— 那既慢又可能命中来路不明的同名文件。
    """
    found = shutil.which("ollama")
    if found:
        return Path(found)
    if os.name != "nt":
        return None
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "Ollama" / "ollama.exe")
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def reachable(host: str, timeout: float = 2.0) -> bool:
    """`/api/tags` 是否可达（与网关的健康判定同一口径）。"""
    try:
        status, _body, _h = net_util.http_get(
            net_util.join_url(net_util.normalize_base(host), "/api/tags"),
            timeout=timeout, with_proxy=False,
        )
    except Exception:  # noqa: BLE001 - 探测失败就是不可达
        return False
    return status == 200


def _spawn_serve(exe: Path) -> None:
    """后台拉起 `ollama serve`：**不弹窗**、不继承控制台、父进程退出后仍存活。

    ⚠ 这里的窗口抑制不是可选项：App 以 pythonw 无控制台启动时，未抑制的
    ``ollama.exe``（console 程序）会被分配一个可见黑框并闪一下。见 ``proc.py``。

    ⚠⚠ **绝不能用 ``DETACHED_PROCESS``**（2026-09-30 实机取证的黑框真因）：
    它让 ollama 自己「没有控制台」，而 ollama 启动后会**再拉自己的 console 子进程**
    （``lib/ollama/llama-server.exe``，每个模型/GPU 一个）—— 子进程无控制台可继承，
    Windows 就给它分配**新的可见控制台** → 用户看到「连弹好几个黑框」。
    本进程的 ``STARTUPINFO(SW_HIDE)`` 只管直接子进程，管不到孙进程。

    正解：``CREATE_NO_WINDOW``（ollama 自带一个**隐藏**控制台，孙进程**继承**它，
    不再新开窗口）+ ``CREATE_NEW_PROCESS_GROUP``（隔离 Ctrl+C）。子进程能否
    **活得比父进程久**与控制台 flags 无关，不需要 DETACHED_PROCESS。
    """
    kw = proc_mod.silent_kwargs()
    if os.name == "nt":
        kw["creationflags"] = int(kw.get("creationflags", 0)) \
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(  # noqa: S603 - 路径来自 find_ollama_exe，参数固定
        [str(exe), "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **kw,
    )


def ensure_running(host: str, *, enabled: bool = True,
                   wait_s: float = DEFAULT_WAIT_S) -> dict:
    """确保 Ollama 在运行；返回结构化结果（**不抛异常**）。

    ``{"enabled", "already", "started", "exe", "waited_s", "reason"}``
    * ``already=True``  → 本来就在跑，什么都没做；
    * ``started=True``  → 本次拉起成功了；
    * 两者皆 False     → 没拉起，``reason`` 说明为什么（调用方应据此提示/降级）。
    """
    out = {"enabled": bool(enabled), "already": False, "started": False,
           "exe": None, "waited_s": 0.0, "reason": ""}

    if reachable(host):
        out["already"] = True
        out["reason"] = "已在运行"
        return out

    if not enabled:
        out["reason"] = "未启用自动启动"
        return out

    if os.name != "nt":
        out["reason"] = "非 Windows 平台，仅探测不自动拉起"
        return out

    exe = find_ollama_exe()
    if exe is None:
        out["reason"] = "未找到 ollama 可执行文件（PATH 与官方安装目录均无）"
        return out
    out["exe"] = str(exe)

    try:
        _spawn_serve(exe)
    except OSError as exc:
        out["reason"] = f"拉起 ollama serve 失败：{exc}"
        return out

    t0 = time.time()
    while time.time() - t0 < max(0.0, wait_s):
        if reachable(host, timeout=1.5):
            out["started"] = True
            out["waited_s"] = round(time.time() - t0, 1)
            out["reason"] = f"已自动启动（等待 {out['waited_s']}s）"
            return out
        time.sleep(_POLL_S)
    out["waited_s"] = round(time.time() - t0, 1)
    out["reason"] = f"拉起后等待 {out['waited_s']}s 仍不可达"
    return out
