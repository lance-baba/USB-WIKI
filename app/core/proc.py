"""子进程「不弹窗」的统一参数 —— Windows 上避免莫名闪出控制台黑框。

为什么必须有这一层
==================
应用以 ``pythonw.exe`` 启动时**自身没有控制台**。此时任何**未抑制**的 console
子进程（如 ``python.exe -c ...``、``ollama.exe serve``）都会被 Windows 分配一个
**可见的控制台窗口** —— 用户看到的就是「莫名弹出 dos 窗口，闪一下」。

单点实现的原因：窗口抑制散落在各调用点，迟早有人新写一处 subprocess 忘了加，
用户就会在某条路径上重新看到黑框。集中一处，新增子进程时直接复用。

用法::

    subprocess.run(cmd, **proc.silent_kwargs())

非 Windows 平台返回空 dict（那里的「无窗口」由别的机制保证）。
"""
from __future__ import annotations

import os
import subprocess


def silent_kwargs() -> dict:
    """返回让子进程**完全不弹窗**的 ``subprocess`` 参数（Windows 专用）。

    * ``CREATE_NO_WINDOW`` —— 不给 console 子程序分配控制台窗口；
    * ``STARTUPINFO(SW_HIDE)`` —— 兜底：即便子程序自己 ``AllocConsole``，
      或它是 GUI 程序，也不会把窗口显示出来（-- 某些控制台程序会自己分配）。

    ⚠ 与 ``DETACHED_PROCESS`` 的关系：后者让子进程脱离父控制台（适合守护进程），
    Windows 文档说它与 ``CREATE_NO_WINDOW`` 同用时后者被忽略 —— 因此需要
    **长期后台存活**的场景（如 ``ollama serve``）应把 ``DETACHED_PROCESS``
    一并传进来，本函数负责补上 ``SW_HIDE`` 这层保险。
    """
    if os.name != "nt":
        return {}
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    si = None
    if hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    out: dict = {}
    if flags:
        out["creationflags"] = flags
    if si is not None:
        out["startupinfo"] = si
    return out


def detach_flags() -> int:
    """让子进程在父进程退出后**继续存活**的额外 creationflags（Windows）。"""
    if os.name != "nt":
        return 0
    return (getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def install_silent_subprocess() -> None:
    """给**整个进程**装上「子进程默认不弹窗」的兜底（Windows only，幂等）。

    为什么在 :func:`silent_kwargs` 之外还需要这一层
    ------------------------------------------------
    ``silent_kwargs()`` 只能管到**本项目自己写的**调用点。但第三方库也会开
    子进程，而且它们内部往往就是一行再普通不过的探测 —— 例如 onnxruntime 在
    import 时执行 ``platform.system() == "Windows"``，而 Windows 上
    ``platform.system()`` 会经 ``uname() → win32_ver() → _syscmd_ver()`` 调起
    ``cmd /c ver``。pythonw 无控制台启动时会因此闪出一个**可见黑框** ——
    这正是「一开程序就弹 dos 窗口」的来源之一，逐点加参数挡不住它。

    因此这里补一层**进程级默认**：凡未显式指定 ``creationflags`` /
    ``startupinfo`` 的子进程，一律按 :func:`silent_kwargs` 注入抑制参数。
    显式指定的调用方（含本项目的 ``silent_kwargs()``）一律保持原样，不被覆盖。

    注意：``subprocess.run/check_output/check_call/call`` 内部都走模块级
    ``Popen``，所以替换 ``subprocess.Popen`` 即可覆盖全部入口。
    """
    if os.name != "nt":
        return
    base = subprocess.Popen
    if getattr(base, "_wikiusb_silent", False):
        return                      # 已装过，幂等
    if not isinstance(base, type):
        return                      # 已被测试探针替换成函数，不再叠加

    class _SilentPopen(base):       # type: ignore[valid-type,misc]
        _wikiusb_silent = True

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            if "creationflags" not in kwargs and "startupinfo" not in kwargs:
                kwargs.update(silent_kwargs())
            super().__init__(*args, **kwargs)

    _SilentPopen.__name__ = "Popen"
    _SilentPopen.__qualname__ = "Popen"
    subprocess.Popen = _SilentPopen
