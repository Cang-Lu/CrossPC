"""Backend factory: pick an implementation per platform.

Imports are always deferred into the function bodies, so importing this module on
Linux never touches ctypes.windll and vice versa; it also makes it easy to swap in
FakeBackend from tests.
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
    """Create a backend that is usable on the current platform.

    prefer: force a specific implementation, e.g. "windows" / "x11" / "uinput" / "fake".
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

    raise BackendError("unsupported platform: %s (only Windows and Linux are supported for now)" % plat)


def platform_summary() -> str:
    return "%s / %s / python %s" % (
        sys.platform, _detect_display_server(),
        ".".join(str(v) for v in sys.version_info[:3]))
