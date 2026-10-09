"""Fake backend: touches no real input, used for loopback self-checks and unit tests.

It supports capture/suppress/inject/clipboard all at once, so the server's
Router + wire protocol + the client's injection can all be exercised in a single
process: no second computer is needed, and the mouse and keyboard in front of
the user are left alone.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional, Tuple

from ..events import Event
from ..layout import Rect
from .base import Backend, SinkFn


class FakeBackend(Backend):
    name = "fake"
    display_server = "fake"
    supports_capture = True
    supports_suppress = True
    supports_inject = True
    supports_clipboard = True

    def __init__(self, log=None, desktop: Optional[Rect] = None):
        super().__init__(log=log)
        self._desktop = desktop or Rect(0, 0, 1920, 1080)
        self._cursor = self._desktop.center
        self._sink: Optional[SinkFn] = None
        self._lock = threading.RLock()
        #: Events really injected into the "local machine" (what the client receives in a loopback test)
        self.injected: List[Event] = []
        #: Local events that were suppressed (in forwarding mode)
        self.suppressed: List[Event] = []
        self.clipboard = ""
        self.clipboard_image: Optional[bytes] = None
        self._clip_rev = 0
        self.forward_calls: List[bool] = []
        self.park: Optional[Tuple[int, int]] = None

    # ------------------------------------------------------------ test driver
    def feed(self, ev: Event) -> None:
        """Simulate "the user moved the physical keyboard/mouse": hand it to the sink and decide by mode whether to suppress it."""
        if self._forwarding:
            self.suppressed.append(ev)
        if ev.kind == 1 and not self._forwarding:
            self._cursor = (ev.a, ev.b)
        if self._sink:
            self._sink(ev)

    # ------------------------------------------------------------ capture
    def start_capture(self, sink: SinkFn) -> None:
        self._sink = sink

    def stop_capture(self) -> None:
        self._sink = None

    def set_forwarding(self, on: bool) -> None:
        self._forwarding = bool(on)
        self.forward_calls.append(self._forwarding)

    def set_park_point(self, x: int, y: int) -> None:
        self.park = (int(x), int(y))
        super().set_park_point(x, y)

    # ------------------------------------------------------------ geometry/cursor
    def desktop_rect(self) -> Rect:
        return self._desktop

    def cursor(self) -> Tuple[int, int]:
        return self._cursor

    def set_cursor(self, x: int, y: int) -> None:
        with self._lock:
            self._cursor = (int(x), int(y))

    # ------------------------------------------------------------ injection
    def inject_motion(self, x: int, y: int) -> None:
        with self._lock:
            self._cursor = (int(x), int(y))
            self.injected.append(Event.motion(x, y))

    def inject_button(self, button: int, pressed: bool) -> None:
        self.injected.append(Event.button(button, pressed))

    def inject_wheel(self, dx: int, dy: int) -> None:
        self.injected.append(Event.wheel(dx, dy))

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        self.injected.append(Event.key(scancode, vk, pressed, extended))

    # ------------------------------------------------------------ clipboard
    def clipboard_text(self) -> Optional[str]:
        return self.clipboard

    def set_clipboard_text(self, text: str) -> None:
        self.clipboard = text
        self._clip_rev += 1

    @property
    def supports_clipboard_images(self) -> bool:
        return True

    def clipboard_image_png(self, max_bytes: int = 0) -> Optional[bytes]:
        data = self.clipboard_image
        if not data:
            return None
        if max_bytes and len(data) > max_bytes:
            return None
        return data

    def set_clipboard_image_png(self, png: bytes) -> None:
        self.clipboard_image = bytes(png)
        self._clip_rev += 1

    def clipboard_revision(self):
        return self._clip_rev

    def probe(self):
        return [("fake backend", True, "used for tests only, does not affect real input")]
