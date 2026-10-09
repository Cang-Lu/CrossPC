"""Miscellaneous helpers: unified logging, coordinate conversion, time formatting."""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional, Tuple

from .layout import Rect

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


class Log:
    """Line-oriented log with a timestamp and a level.

    Writes to stdout by default; if file_path is given, a copy also goes to that
    file (UTF-8, line buffered). Why being able to write a file matters: during
    real-device debugging the user runs server/client in **their own** window
    (it cannot be run from a restricted session), so with a log file they can
    send it over afterwards, or just let a tool read it, and pinpoint the
    problem without manually copying and pasting what was on screen.
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
            # Append mode: one debugging effort may span many runs, so do not
            # overwrite each other
            self._file = open(path, "a", encoding="utf-8", buffering=1)
        except OSError as exc:
            self._file = None
            self.warn("cannot open log file %s: %s (continuing with screen "
                      "output only)" % (path, exc))
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
        """High-frequency event log (only emitted with --debug-events)."""
        if self.debug_events:
            self._write("EVENT", msg)

    def plain(self, msg: str = "") -> None:
        """Emit one line verbatim, with no timestamp or level.

        Meant for report-style output such as doctor's: it looks clean on screen
        while still making it into the log file.
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
        """Lets a Log instance itself be passed to a backend as a LogFn."""
        self.info(msg)


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:                                  # pragma: no cover
        return "?"


def norm_from_desktop(rect: Rect, x: int, y: int) -> Tuple[int, int]:
    """Local real desktop coordinates -> coordinates normalized to top-left (0,0)."""
    return x - rect.x, y - rect.y


def desktop_from_norm(rect: Rect, x: int, y: int) -> Tuple[int, int]:
    """Coordinates normalized to (0,0) -> local real desktop coordinates (may be negative)."""
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


# ------------------------------------------------------------------ temporary files
def pick_writable_dir(candidates=None) -> str:
    """Pick a directory where we can **actually write files**.

    Why not tempfile.mkdtemp(): in some restricted environments (Windows
    sandbox, read-only mounts, some corporate policies) "can create a directory"
    and "can write a file into that directory" are two different things -- the
    subdirectory that gets created may not be writable. So here we write a probe
    file directly and check.
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
    """Unique prefix for temporary files (carries the pid, so it cannot clash with the user's real config)."""
    import os
    return ".crosspc-%s-%d-" % (tag, os.getpid())
