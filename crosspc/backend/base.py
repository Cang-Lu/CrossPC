"""Platform backend interface (frozen contract).

A backend is the set of capabilities an operating system has for "capture
input / suppress local input / inject input / read and write the clipboard".
The upper layers (Router / ServerApp / ClientApp) depend only on the interface
here and never touch any platform details, so that:

  * a full loopback self-check can run on FakeBackend without a second machine;
  * adding a platform = adding one subclass + registering it in
    backend/__init__.py.

Three modes (chosen by the upper layer, merely carried out by the backend):
  local mode    capture but pass through: input still reaches the local machine
                as usual and the backend only hands it to the sink on the side
                for observation. Used to decide "has the mouse hit the screen
                edge and wants to cross out".
  forwarding mode   set_forwarding(True): input is suppressed (it no longer
                reaches the local machine) and is handed entirely to the sink,
                while the local cursor is "parked" at a fixed point so that it
                does not run all over the place.
  injection mode   used by the client: inject() writes remote events into the
                local machine.

Threading contract: the sink registered by start_capture() is called on the
backend's input thread and must return immediately (it must not block and must
not do network IO). The backend's stop/close must be callable from other
threads, and must guarantee that "keys are no longer suppressed even if we
crash" (the safety bottom line).
"""
from __future__ import annotations

from typing import Callable, Iterable, List, Optional, Tuple

from ..events import (BUTTON, KEY, MOTION, WHEEL, BTN_LEFT, BTN_MIDDLE,
                      BTN_RIGHT, Event)
from ..layout import Rect

#: Log callback
LogFn = Callable[[str], None]
#: Input callback: every event the backend captures
SinkFn = Callable[[Event], None]


class BackendError(RuntimeError):
    """A backend capability is missing or initialization failed (the message is for the end user)."""


class Backend:
    # ------------------------------------------------------------ capability declaration
    name = "base"
    display_server = ""            # windows / x11 / wayland
    supports_capture = False       # can capture local input
    supports_suppress = False      # can suppress local input in forwarding mode
    supports_inject = False        # can inject input
    supports_clipboard = False     # can read and write the system clipboard

    # ------------------------------------------------------------ lifecycle
    def __init__(self, log: Optional[LogFn] = None):
        self._log = log or (lambda m: None)
        self._sink: Optional[SinkFn] = None
        self._forwarding = False
        self._park: Optional[Tuple[int, int]] = None
        # Keys/buttons currently held down, used to "release all keys" when the
        # connection drops, which avoids stuck keys
        self._pressed_keys: List[Event] = []
        self._pressed_buttons: List[int] = []

    # ------------------------------------------------------------ utilities
    def log(self, msg: str) -> None:
        self._log("[%s] %s" % (self.name, msg))

    @property
    def can_serve(self) -> bool:
        """Whether this machine can act as the server (the one the keyboard and mouse are attached to)."""
        return self.supports_capture and self.supports_suppress

    @property
    def can_be_client(self) -> bool:
        return self.supports_inject

    def caps(self) -> str:
        bits = []
        if self.supports_capture:
            bits.append("capture")
        if self.supports_suppress:
            bits.append("suppress")
        if self.supports_inject:
            bits.append("inject")
        if self.supports_clipboard:
            bits.append("clipboard")
        return "%s(%s)" % (self.name, ",".join(bits) or "none")

    def prepare(self) -> None:
        """Open devices / set up DPI awareness and so on. Must be idempotent."""

    def close(self) -> None:
        """Release every resource; set_forwarding(False) must happen before tearing down hooks."""
        try:
            self.stop_capture()
        finally:
            self.set_forwarding(False)

    # ------------------------------------------------------------ desktop geometry
    def desktop_rect(self) -> Rect:
        """Rectangle of the local virtual desktop (the union of all monitors); the top-left corner may not be (0,0)."""
        raise BackendError("%s backend does not implement desktop_rect()" % self.name)

    def monitors(self) -> List[Rect]:
        return [self.desktop_rect()]

    # ------------------------------------------------------------ capture (server)
    def start_capture(self, sink: SinkFn) -> None:
        raise BackendError("%s backend does not support capturing input" % self.name)

    def stop_capture(self) -> None:
        pass

    def set_forwarding(self, on: bool) -> None:
        """True: suppress local input and hand it to the sink; False: restore local input.

        Implementations must guarantee that even an abnormal process exit does
        not leave the user's keyboard and mouse frozen -- restore in a finally
        block and register an atexit/signal fallback.
        """
        raise BackendError("%s backend does not support suppressing local input" % self.name)

    @property
    def forwarding(self) -> bool:
        return self._forwarding

    def set_park_point(self, x: int, y: int) -> None:
        """In forwarding mode, park the local cursor on this point (local desktop coordinates)."""
        self._park = (int(x), int(y))

    def park_point(self) -> Optional[Tuple[int, int]]:
        return self._park

    def emergency_restore(self) -> None:
        """Emergency restore: stop suppressing input at once and put the cursor back to a normal position.

        Interrupt handling, the watchdog and abnormal exits all go through here.
        """
        try:
            self.set_forwarding(False)
        except Exception:
            pass

    # ------------------------------------------------------------ cursor
    def cursor(self) -> Tuple[int, int]:
        """Read the local cursor position (local desktop coordinates)."""
        raise BackendError("%s backend does not implement cursor()" % self.name)

    def set_cursor(self, x: int, y: int) -> None:
        """Move the local cursor to the given position (local desktop coordinates)."""
        raise BackendError("%s backend does not implement set_cursor()" % self.name)

    # ------------------------------------------------------------ injection (client)
    def inject_motion(self, x: int, y: int) -> None:
        raise BackendError("%s backend does not support injection" % self.name)

    def inject_button(self, button: int, pressed: bool) -> None:
        raise BackendError("%s backend does not support injection" % self.name)

    def inject_wheel(self, dx: int, dy: int) -> None:
        raise BackendError("%s backend does not support injection" % self.name)

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        raise BackendError("%s backend does not support injection" % self.name)

    # ------------------------------------------------------------ unified injection entry point
    def inject(self, ev: Event) -> None:
        """The client calls only this method; it also records key state for release_all()."""
        k = ev.kind
        if k == MOTION:
            self.inject_motion(ev.a, ev.b)
        elif k == BUTTON:
            if ev.b:
                if ev.a not in self._pressed_buttons:
                    self._pressed_buttons.append(ev.a)
            elif ev.a in self._pressed_buttons:
                self._pressed_buttons.remove(ev.a)
            self.inject_button(ev.a, bool(ev.b))
        elif k == WHEEL:
            self.inject_wheel(ev.a, ev.b)
        elif k == KEY:
            if ev.c:
                if ev not in self._pressed_keys:
                    self._pressed_keys.append(ev)
            elif ev in self._pressed_keys:
                self._pressed_keys.remove(ev)
            self.inject_key(ev.a, ev.b, bool(ev.c), bool(ev.d))
        else:
            raise BackendError("unknown event kind %r" % (k,))

    def release_all(self) -> None:
        """Release every key and mouse button currently held down (called on switch/disconnect, stuck-key protection)."""
        keys, self._pressed_keys = self._pressed_keys, []
        buttons, self._pressed_buttons = self._pressed_buttons, []
        for ev in keys:
            try:
                self.inject_key(ev.a, ev.b, False, bool(ev.d))
            except Exception as exc:            # pragma: no cover - fallback
                self.log("failed to release key %s: %s" % (ev.describe(), exc))
        for btn in buttons:
            try:
                self.inject_button(btn, False)
            except Exception as exc:            # pragma: no cover
                self.log("failed to release mouse button %s: %s" % (btn, exc))

    # ------------------------------------------------------------ clipboard
    def clipboard_text(self) -> Optional[str]:
        return None

    def set_clipboard_text(self, text: str) -> None:
        raise BackendError("%s backend does not support writing the clipboard" % self.name)

    def clipboard_revision(self) -> Optional[object]:
        """Clipboard change marker (Windows: sequence number; X11: content hash; None if unsupported)."""
        return None

    def clipboard_formats(self) -> List[str]:
        """Which formats the clipboard currently holds (diagnostics only, read-only). Platforms without support return an empty list."""
        return []

    # ------------------------------------------------------------ clipboard images
    @property
    def supports_clipboard_images(self) -> bool:
        """Whether the image clipboard is supported. Off by default; the Windows and Linux backends each override it."""
        return False

    def clipboard_image_png(self, max_bytes: int = 0) -> Optional[bytes]:
        """Read the image on the clipboard and return PNG bytes; None if there is no image, it is unsupported, or the cap is exceeded."""
        return None

    def set_clipboard_image_png(self, png: bytes) -> None:
        raise BackendError("%s backend does not support writing an image to the clipboard" % self.name)

    def clipboard_read(self, max_image_bytes: int = 0,
                       prefer_image: bool = False) -> Optional[Tuple[str, object]]:
        """The single "read the current clipboard content" entry point.

        Returns ("text", str) or ("image", png_bytes); returns None when there is
        nothing to sync. prefer_image decides which of the two kinds is taken
        first when both are present at the same time -- this is a config option,
        because in a case such as "copy a chart out of Excel" the text is cheap
        and the image is accurate, and users do not all prefer the same one.
        """
        order = ("image", "text") if prefer_image else ("text", "image")
        for kind in order:
            if kind == "text":
                if not self.supports_clipboard:
                    continue
                text = self.clipboard_text()
                if text:
                    return ("text", text)
            else:
                if not (self.supports_clipboard_images and max_image_bytes > 0):
                    continue
                png = self.clipboard_image_png(max_image_bytes)
                if png:
                    return ("image", png)
        return None

    # ------------------------------------------------------------ self-check
    def probe(self) -> List[Tuple[str, bool, str]]:
        """Return [(check name, passed, explanation)] for `crosspc doctor` to display."""
        return []
