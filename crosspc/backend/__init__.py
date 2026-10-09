"""后端工厂: 按平台挑选实现。

导入一律延迟到函数内部, 这样在 Linux 上 import 本模块不会碰到 ctypes.windll,
反之亦然; 也方便测试时替换成 FakeBackend。
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from .base import Backend, BackendError, LogFn

__all__ = ["Backend", "BackendError", "get_backend", "platform_summary"]


def _detect_display_server() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "none"
    if sys.platform == "darwin":
        return "quartz"
    return "unknown"


def get_backend(log: Optional[LogFn] = None, prefer: Optional[str] = None) -> Backend:
    """创建当前平台可用的后端。

    prefer: 强制指定实现, 例如 "windows" / "x11" / "uinput" / "fake"。
    """
    plat = sys.platform
    want = prefer or (_detect_display_server() if plat.startswith("linux") else plat)

    if want in ("fake", "test"):
        from .fake import FakeBackend
        return FakeBackend(log=log)

    if plat == "win32":
        from .windows import WindowsBackend
        return WindowsBackend(log=log)

    if plat.startswith("linux"):
        from .linux import LinuxBackend
        return LinuxBackend(log=log, prefer=prefer)

    raise BackendError("暂不支持的平台: %s (目前支持 Windows 与 Linux)" % plat)


def platform_summary() -> str:
    return "%s / %s / python %s" % (
        sys.platform, _detect_display_server(),
        ".".join(str(v) for v in sys.version_info[:3]))
