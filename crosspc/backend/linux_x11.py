"""Linux X11 injector: call libX11 + libXtst's XTest extension directly through ctypes.

Why it is a separate small class (that does not inherit Backend):
    It is only a thin wrapper for "write events into some X display" and does not
    carry the backend duties of platform self-check/clipboard/geometry;
    LinuxBackend uses it as a replaceable injection strategy. That also lets
    doctor ask available() on its own without opening a device.

Why the library loading is not at the top (same as linux_uinput):
    This module only defines constants and ctypes signatures at the top level;
    ctypes.CDLL(...) for libX11.so.6 / libXtst.so.6 is always deferred into
    open()/available(). So importing this file on Windows, and compileall, both
    work fine.

Thread safety:
    Xlib itself is not thread-safe. With multiple threads (capture thread +
    injection thread), XInitThreads() must come first, and it must be called
    before **any** other Xlib call -- the first thing open() does is call it.
    Even so, a lock is still used here to serialize "a batch of events + flush",
    so that the events of two threads cannot interleave in the request buffer.

flush strategy:
    The injection methods do not XFlush immediately by default (otherwise every
    mouse movement would cost an RTT and a 1000 Hz mouse would saturate the X
    connection). Internally a flush happens automatically once FLUSH_EVERY events
    have accumulated, and the caller can also call flush() at the end of an event
    batch. close() always flushes.
"""
from __future__ import annotations

import ctypes
import os
import threading
from typing import Callable, Dict, Optional, Tuple

from .. import keys

#: Library name candidates. On the vast majority of distributions it is
#: libX11.so.6 / libXtst.so.6, but some minimal images (and certain BSD-style
#: ABIs) only ship the unversioned .so, so both are tried.
X11_LIB_CANDIDATES = ("libX11.so.6", "libX11.so")
XTST_LIB_CANDIDATES = ("libXtst.so.6", "libXtst.so")

#: Automatically flush once this many events have accumulated
FLUSH_EVERY = 8

# XTest mouse button numbers are X11's button numbers (consistent with events.py);
# the only twist is that in X the wheel is a "button" too: 4=up 5=down 6=left
# 7=right. They are spelled out here to serve as documentation.
X_BUTTON_LEFT = 1
X_BUTTON_MIDDLE = 2
X_BUTTON_RIGHT = 3
X_BUTTON_WHEEL_UP = 4
X_BUTTON_WHEEL_DOWN = 5
X_BUTTON_WHEEL_LEFT = 6
X_BUTTON_WHEEL_RIGHT = 7
X_BUTTON_BACK = 8
X_BUTTON_FORWARD = 9


def _load_library(candidates) -> Tuple[Optional[ctypes.CDLL], str]:
    """Find the first library that can be loaded, in candidate order; returns (library object or None, explanation)."""
    errors = []
    for name in candidates:
        try:
            return ctypes.CDLL(name), name
        except OSError as exc:
            errors.append("%s: %s" % (name, exc))
    return None, "; ".join(errors) or "no library found"


class X11Injector:
    """Treats one X display as the injection target. name is shown in logs/diagnostics."""

    name = "x11"

    def __init__(self, log: Optional[Callable[[str], None]] = None,
                 display: Optional[str] = None):
        self._log = log or (lambda m: None)
        self._display_name = display
        self._x11: Optional[ctypes.CDLL] = None
        self._xtst: Optional[ctypes.CDLL] = None
        self._dpy = ctypes.c_void_p(None)
        self._root = 0
        self._lock = threading.RLock()
        self._pending = 0
        #: (scancode, extended) -> keycode, so the X server is not asked every time
        self._keycode_cache: Dict[Tuple[int, bool], int] = {}
        #: X buttons currently held, used as a fallback release in close(), stuck-key protection
        self._pressed_buttons = set()

    def _say(self, msg: str) -> None:
        self._log("[x11] %s" % msg)

    # ------------------------------------------------------------ lifecycle
    @property
    def opened(self) -> bool:
        return bool(self._dpy)

    def open(self) -> None:
        """Load the libraries, XInitThreads, XOpenDisplay, query the XTest extension, cache the root window."""
        if self._dpy:
            return
        display = self._display_name or os.environ.get("DISPLAY") or ""
        if not display:
            raise RuntimeError(
                "the DISPLAY environment variable is empty, cannot inject into X11. "
                "Run inside a graphical session (for example from a terminal on the "
                "desktop, or make sure SSH was given -X / DISPLAY is set); if this "
                "machine is a pure Wayland session, switch to uinput injection instead.")

        x11, x11_name = _load_library(X11_LIB_CANDIDATES)
        if x11 is None:
            raise RuntimeError(
                "failed to load libX11 (%s). Please install the X11 runtime "
                "libraries: on Debian/Ubuntu, sudo apt install libx11-6 libxtst6" % x11_name)
        xtst, xtst_name = _load_library(XTST_LIB_CANDIDATES)
        if xtst is None:
            raise RuntimeError(
                "failed to load libXtst (%s). The XTest extension lives in a "
                "separate library, please install it: on Debian/Ubuntu, "
                "sudo apt install libxtst6" % xtst_name)

        self._declare_signatures(x11, xtst)
        # Must come before any other Xlib call: only after this does Xlib protect
        # itself with internal locks.
        x11.XInitThreads()

        dpy = x11.XOpenDisplay(display.encode("utf-8") if display else None)
        if not dpy:
            raise RuntimeError(
                "XOpenDisplay(%r) failed: the X server cannot be reached. Make sure "
                "DISPLAY points at a live X session and that the current user is "
                "allowed to access it (xhost +local: is only a stopgap)."
                % display)
        self._x11 = x11
        self._xtst = xtst
        self._dpy = ctypes.c_void_p(dpy)
        self._root = x11.XDefaultRootWindow(self._dpy)

        # Some X servers / nested servers are built without XTest, so ask up front
        ev_base = ctypes.c_int(0)
        err_base = ctypes.c_int(0)
        major = ctypes.c_int(0)
        minor = ctypes.c_int(0)
        if not xtst.XTestQueryExtension(self._dpy, ctypes.byref(ev_base),
                                        ctypes.byref(err_base),
                                        ctypes.byref(major), ctypes.byref(minor)):
            self.close()
            raise RuntimeError(
                "the X server has no XTest extension, so keyboard/mouse events cannot "
                "be injected. If this is a Wayland session, switch to uinput injection.")
        self._say("connected to X11 display=%r XTest %d.%d"
                  % (display, major.value, minor.value))

    @staticmethod
    def _declare_signatures(x11: ctypes.CDLL, xtst: ctypes.CDLL) -> None:
        """Declare the argument/return types explicitly.

        This step is mandatory: ctypes passes arguments as int by default, whereas
        Xlib's Dpy*/Window/KeyCode are 8-byte pointers/unsigned long on 64-bit.
        Without the declarations a pointer gets truncated to 32 bits, and the
        typical symptoms are a segfault or "injecting to Mars".
        """
        P = ctypes.c_void_p
        x11.XInitThreads.restype = ctypes.c_int
        x11.XInitThreads.argtypes = []
        x11.XOpenDisplay.restype = P
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [P]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [P]
        x11.XDefaultScreen.restype = ctypes.c_int
        x11.XDefaultScreen.argtypes = [P]
        x11.XDisplayWidth.restype = ctypes.c_int
        x11.XDisplayWidth.argtypes = [P, ctypes.c_int]
        x11.XDisplayHeight.restype = ctypes.c_int
        x11.XDisplayHeight.argtypes = [P, ctypes.c_int]
        x11.XFlush.restype = ctypes.c_int
        x11.XFlush.argtypes = [P]
        x11.XSync.restype = ctypes.c_int
        x11.XSync.argtypes = [P, ctypes.c_int]
        x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        x11.XKeysymToKeycode.argtypes = [P, ctypes.c_ulong]
        # XWarpPointer(dpy, src_w, dest_w, src_x, src_y, src_w, src_h, dest_x, dest_y)
        # Note that Window is an XID = unsigned long (8 bytes on 64-bit); coordinates are int.
        x11.XWarpPointer.restype = ctypes.c_int
        x11.XWarpPointer.argtypes = [P, ctypes.c_ulong, ctypes.c_ulong,
                                     ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint, ctypes.c_uint,
                                     ctypes.c_int, ctypes.c_int]
        # Prefer XTestFakeMotionEvent when it is available: it sends a motion event
        # relative to the root window in "screen coordinates" and is the standard way
        # to synthesize clicks; XWarpPointer really moves the X pointer and gets
        # pushed back in some multi-screen / pointer-constraint (confine_to) setups.
        # Both are absolute positioning.
        xtst.XTestFakeMotionEvent.restype = ctypes.c_int
        xtst.XTestFakeMotionEvent.argtypes = [P, ctypes.c_int,
                                              ctypes.c_int, ctypes.c_int,
                                              ctypes.c_ulong]
        xtst.XTestFakeButtonEvent.restype = ctypes.c_int
        xtst.XTestFakeButtonEvent.argtypes = [P, ctypes.c_uint,
                                              ctypes.c_int, ctypes.c_ulong]
        xtst.XTestFakeKeyEvent.restype = ctypes.c_int
        xtst.XTestFakeKeyEvent.argtypes = [P, ctypes.c_uint,
                                           ctypes.c_int, ctypes.c_ulong]
        xtst.XTestQueryExtension.restype = ctypes.c_int
        xtst.XTestQueryExtension.argtypes = [P, ctypes.POINTER(ctypes.c_int),
                                             ctypes.POINTER(ctypes.c_int),
                                             ctypes.POINTER(ctypes.c_int),
                                             ctypes.POINTER(ctypes.c_int)]

    def close(self) -> None:
        """Release mouse buttons still held, flush, close the display. Idempotent, callable from any thread."""
        with self._lock:
            dpy = self._dpy
            if not dpy:
                return
            # Fallback: if any mouse button is still held when we disconnect, release
            # it first, so the other side does not end up with a stuck key
            for btn in sorted(self._pressed_buttons):
                try:
                    self._xtst.XTestFakeButtonEvent(dpy, btn, 0, 0)
                except Exception:
                    pass
            self._pressed_buttons.clear()
            # The keycode cache depends on this display (different displays can have
            # different mappings), so it must be cleared
            self._keycode_cache.clear()
            try:
                self._x11.XFlush(dpy)
                self._x11.XCloseDisplay(dpy)
            except Exception as exc:           # pragma: no cover
                self._say("error while closing the X display (ignored): %s" % exc)
            self._dpy = ctypes.c_void_p(None)
            self._pending = 0

    # ------------------------------------------------------------ flush
    def _bump(self) -> None:
        """Count "one more event in the buffer"; flush automatically once enough have piled up."""
        self._pending += 1
        if self._pending >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        """Push the accumulated requests to the X server. The caller invokes it once at the end of a batch."""
        if not self._dpy:
            return
        try:
            self._x11.XFlush(self._dpy)
        finally:
            self._pending = 0

    def sync(self) -> None:
        """flush and wait until the X server has processed it (for diagnostics/tests, slower than flush)."""
        if self._dpy:
            self._x11.XSync(self._dpy, 0)
            self._pending = 0

    def _require(self) -> ctypes.c_void_p:
        if not self._dpy:
            raise RuntimeError("the X11 injector is not open yet (call open() first)")
        return self._dpy

    # ------------------------------------------------------------ geometry
    def screen_size(self) -> Tuple[int, int]:
        """Pixel size of the default screen (XDisplayWidth/Height)."""
        dpy = self._require()
        scr = self._x11.XDefaultScreen(dpy)
        return (int(self._x11.XDisplayWidth(dpy, scr)),
                int(self._x11.XDisplayHeight(dpy, scr)))

    def pointer(self) -> Tuple[int, int]:
        """Read the current pointer position (XQueryPointer). Returns (0, 0) when it cannot be read."""
        dpy = self._require()
        x11 = self._x11
        if not hasattr(x11, "XQueryPointer"):    # pragma: no cover
            return (0, 0)
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + \
            [ctypes.POINTER(ctypes.c_ulong)] * 3 + \
            [ctypes.POINTER(ctypes.c_int)] * 2 + [ctypes.POINTER(ctypes.c_uint)]
        root_ret = ctypes.c_ulong(0)
        child_ret = ctypes.c_ulong(0)
        rx = ctypes.c_int(0)
        ry = ctypes.c_int(0)
        wx = ctypes.c_int(0)
        wy = ctypes.c_int(0)
        mask = ctypes.c_uint(0)
        ok = x11.XQueryPointer(dpy, self._root, ctypes.byref(root_ret),
                               ctypes.byref(child_ret), ctypes.byref(rx),
                               ctypes.byref(ry), ctypes.byref(wx),
                               ctypes.byref(wy), ctypes.byref(mask))
        if not ok:
            return (0, 0)
        return (int(rx.value), int(ry.value))

    # ------------------------------------------------------------ injection
    def inject_motion(self, x: int, y: int) -> None:
        """Absolute positioning to screen pixel (x, y)."""
        with self._lock:
            dpy = self._require()
            if self._xtst:
                self._xtst.XTestFakeMotionEvent(dpy, -1, int(x), int(y), 0)
            else:                              # pragma: no cover - open() already guarantees this
                self._x11.XWarpPointer(dpy, 0, self._root, 0, 0, 0, 0,
                                       int(x), int(y))
            self._bump()

    def inject_button(self, button: int, pressed: bool) -> None:
        """X11 button numbers 1/2/3 are left/middle/right and 8/9 are back/forward; handed to XTest as they are."""
        btn = int(button)
        if btn not in (1, 2, 3, 8, 9):
            self._say("unknown mouse button %r, skipped" % (button,))
            return
        with self._lock:
            dpy = self._require()
            self._xtst.XTestFakeButtonEvent(dpy, ctypes.c_uint(btn),
                                            1 if pressed else 0, 0)
            if pressed:
                self._pressed_buttons.add(btn)
            else:
                self._pressed_buttons.discard(btn)
            self._bump()

    def inject_wheel(self, dx: int, dy: int) -> None:
        """Wheel: send one press+release pair per notch. dy is positive upwards, dx positive to the right."""
        with self._lock:
            dpy = self._require()
            up = X_BUTTON_WHEEL_UP if dy > 0 else X_BUTTON_WHEEL_DOWN
            right = X_BUTTON_WHEEL_RIGHT if dx > 0 else X_BUTTON_WHEEL_LEFT
            for _ in range(abs(int(dy))):
                self._xtst.XTestFakeButtonEvent(dpy, ctypes.c_uint(up), 1, 0)
                self._xtst.XTestFakeButtonEvent(dpy, ctypes.c_uint(up), 0, 0)
                self._bump()
            for _ in range(abs(int(dx))):
                self._xtst.XTestFakeButtonEvent(dpy, ctypes.c_uint(right), 1, 0)
                self._xtst.XTestFakeButtonEvent(dpy, ctypes.c_uint(right), 0, 0)
                self._bump()

    def keycode_for(self, scancode: int, extended: bool = False) -> Optional[int]:
        """(scancode, extended) -> X keycode. Returns None when the keysym cannot be
        found or that keysym has no keycode in the current keyboard mapping (the
        caller skips it and logs)."""
        ck = (int(scancode), bool(extended))
        cached = self._keycode_cache.get(ck)
        if cached is not None:
            return cached
        keysym = keys.keysym_for(scancode, extended)
        if keysym is None:
            return None
        code = int(self._x11.XKeysymToKeycode(self._dpy, ctypes.c_ulong(keysym)))
        if not code:                            # X returned 0 = that keysym is unbound
            return None
        self._keycode_cache[ck] = code
        return code

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        with self._lock:
            dpy = self._require()
            code = self.keycode_for(scancode, extended)
            if code is None:
                self._say("scancode 0x%02X (%s) has no matching keycode in the current "
                          "X keyboard mapping, skipped"
                          % (scancode, keys.key_name(scancode, vk, extended)))
                return
            self._xtst.XTestFakeKeyEvent(dpy, ctypes.c_uint(code),
                                         1 if pressed else 0, 0)
            self._bump()

    # ------------------------------------------------------------ self-check
    @staticmethod
    def available() -> Tuple[bool, str]:
        """(whether X11 injection is usable, explanation). Never raises; used by doctor.

        Note: this really connects to an X server once and disconnects immediately
        -- that is the only reliable way to decide (merely checking that the
        DISPLAY variable exists is misleading, which happens often with a leftover
        DISPLAY inside SSH). It injects no events, so there is no side effect for
        the user.
        """
        display = os.environ.get("DISPLAY") or ""
        if not display:
            return (False, "DISPLAY is empty: no X session is available; for a pure Wayland session use uinput injection")

        x11, why_x11 = _load_library(X11_LIB_CANDIDATES)
        if x11 is None:
            return (False, "failed to load libX11 (%s): on Debian, sudo apt install libx11-6" % why_x11)
        xtst, why_xtst = _load_library(XTST_LIB_CANDIDATES)
        if xtst is None:
            return (False, "failed to load libXtst (%s): on Debian, sudo apt install libxtst6" % why_xtst)

        inj = X11Injector()
        try:
            inj.open()
        except Exception as exc:
            return (False, "%s" % exc)
        try:
            w, h = inj.screen_size()
        except Exception:                       # pragma: no cover
            w = h = 0
        finally:
            inj.close()
        return (True, "DISPLAY=%s is usable, XTest extension ready, screen %dx%d"
                % (display, w, h))
