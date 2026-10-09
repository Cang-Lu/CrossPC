"""零碎工具: 统一日志、坐标换算、时间格式化。"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional, Tuple

from .layout import Rect

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class Log:
    """带时间戳和级别的行日志。

    默认写 stdout; 传了 file_path 就同时写一份到文件(UTF-8, 行缓冲)。
    为什么要能写文件: 真机联调时用户是在**自己**的窗口里跑 server/client 的
    (受限会话里跑不了), 有了日志文件, 事后把文件发出来/让工具读一下就能定位
    问题, 不必靠人肉复制粘贴屏幕内容。
    """

    def __init__(self, level: str = "info", debug_events: bool = False,
                 stream=None, file_path: Optional[str] = None):
        self.level = LEVELS.get((level or "info").lower(), 20)
        self.debug_events = debug_events
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self.file_path = file_path
        self._file = None
        if file_path:
            self._open_file(file_path)

    def _open_file(self, path: str) -> None:
        import os
        try:
            folder = os.path.dirname(os.path.abspath(path))
            if folder and not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
            # 追加模式: 一次联调可能跑很多轮, 不要互相覆盖
            self._file = open(path, "a", encoding="utf-8", buffering=1)
        except OSError as exc:
            self._file = None
            self.warn("打不开日志文件 %s: %s(继续只输出到屏幕)" % (path, exc))
            return
        try:
            import sys as _sys
            self._file.write("\n==== CrossPC %s | %s | %s ====\n"
                             % (_version(), time.strftime("%Y-%m-%d %H:%M:%S"),
                                " ".join(_sys.argv)))
        except Exception:
            pass

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def set_level(self, level: str) -> None:
        self.level = LEVELS.get((level or "info").lower(), 20)

    def _write(self, tag: str, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        elapsed = time.monotonic() - self._start
        line = "[%s +%6.1fs] %-5s %s" % (stamp, elapsed, tag, msg)
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except Exception:
                pass
            if self._file is not None:
                try:
                    self._file.write(line + "\n")
                except Exception:
                    pass

    def debug(self, msg: str) -> None:
        if self.level <= LEVELS["debug"]:
            self._write("DEBUG", msg)

    def info(self, msg: str) -> None:
        if self.level <= LEVELS["info"]:
            self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        if self.level <= LEVELS["warn"]:
            self._write("WARN", msg)

    def error(self, msg: str) -> None:
        if self.level <= LEVELS["error"]:
            self._write("ERROR", msg)

    def event(self, msg: str) -> None:
        """高频事件日志(仅 --debug-events 时输出)。"""
        if self.debug_events:
            self._write("EVENT", msg)

    def plain(self, msg: str = "") -> None:
        """原样输出一行, 不加时间戳/级别。

        给 doctor 这类"报告"型输出用: 屏幕上看着干净, 同时也能进日志文件。
        """
        with self._lock:
            try:
                self._stream.write(msg + "\n")
                self._stream.flush()
            except Exception:
                pass
            if self._file is not None:
                try:
                    self._file.write(msg + "\n")
                except Exception:
                    pass

    def __call__(self, msg: str) -> None:
        """让 Log 实例本身可以当 LogFn 传给后端。"""
        self.info(msg)


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:                                  # pragma: no cover
        return "?"


def norm_from_desktop(rect: Rect, x: int, y: int) -> Tuple[int, int]:
    """本机真实桌面坐标 -> 归一到左上角 (0,0) 的坐标。"""
    return x - rect.x, y - rect.y


def desktop_from_norm(rect: Rect, x: int, y: int) -> Tuple[int, int]:
    """归一到 (0,0) 的坐标 -> 本机真实桌面坐标(可能为负)。"""
    return x + rect.x, y + rect.y


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%d" % n


def shorten(text: str, limit: int = 40) -> str:
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


# ------------------------------------------------------------------ 临时文件
def pick_writable_dir(candidates=None) -> str:
    """挑一个**真的能写文件**的目录。

    为什么不用 tempfile.mkdtemp(): 某些受限环境(Windows 沙箱、只读挂载、
    部分企业策略)里"能建目录"和"能往目录里写文件"是两回事 —— 建出来的
    子目录可能不可写。所以这里直接写一个探针文件来验证。
    """
    import os
    import tempfile
    if candidates is None:
        candidates = [tempfile.gettempdir(), os.getcwd()]
    for d in candidates:
        if not d or not os.path.isdir(d):
            continue
        probe = os.path.join(d, ".crosspc-write-probe-%d" % os.getpid())
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.remove(probe)
            return d
        except OSError:
            continue
    return os.getcwd()


def scratch_prefix(tag: str = "selftest") -> str:
    """给临时文件用的唯一前缀(带 pid, 不会和用户的真实配置撞名)。"""
    import os
    return ".crosspc-%s-%d-" % (tag, os.getpid())
