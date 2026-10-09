"""Windows backend: low-level keyboard/mouse hooks + SendInput injection + system clipboard.

Only ctypes is used to call user32/kernel32; nothing has to be installed with pip.

## How capture and "takeover" work

* Two low-level hooks, WH_KEYBOARD_LL / WH_MOUSE_LL, observe global input. The
  hook functions live on a dedicated thread that does nothing but run the message
  loop, and the callbacks **only do arithmetic and enqueue**, never logging and
  never doing network IO -- a low-level hook timeout (300 ms by default) gets the
  hook torn off by the system, which means the user's keyboard and mouse suddenly
  "stop obeying", and that is the most dangerous failure mode of this tool.
* Local mode (not taken over): the hooks see the events but let every one of them
  through; we only learn where the mouse is and which way it is being pushed on
  the side. The user cannot tell the tool is there at all.
* Forwarding mode (taken over): the hooks return 1 to suppress keys/wheel/mouse
  buttons entirely and hand them to the upper layer to forward to the client.
  Mouse **movement** cannot be intercepted by a hook (the cursor is updated
  directly by the input stack), so we use the "recenter (park)" trick: on every
  movement we SetCursorPos the cursor back to the park point, and derive the
  delta from the difference between two consecutive pt values -- that way we get
  unrestricted movement while the local cursor stays parked in the corner of the
  screen instead of running around.
* An exception inside a hook function is printed to stderr by Python and makes
  the callback return 0 (= pass through); together with the watchdog thread and
  atexit this guarantees that "no exception ever swallows the user's keyboard
  for good".

## Injection (client role)

SetCursorPos provides absolute positioning and SendInput(KEYEVENTF_SCANCODE)
sends key presses, using scancodes rather than characters, so it is independent
of the server's keyboard layout.
"""
from __future__ import annotations

import atexit
import ctypes
import threading
import time
from ctypes import wintypes as wt
from typing import Callable, List, Optional, Tuple

from ..events import (BTN_BACK, BTN_FORWARD, BTN_LEFT, BTN_MIDDLE, BTN_RIGHT,
                      Event)
from ..image import (DEFAULT_PIXEL_CAP, PNG_SIG, ImageError, dib_from_png,
                     png_from_dib)
from ..keys import (KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP,
                    KEYEVENTF_SCANCODE, VK_PAUSE)
from ..layout import Rect
from .base import Backend, BackendError, LogFn, SinkFn

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ------------------------------------------------------------------ constants
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

WHEEL_DELTA = 120

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP = 0x0080, 0x0100
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000

XBUTTON1, XBUTTON2 = 0x0001, 0x0002
CF_BITMAP = 2
CF_DIB = 8
CF_DIBV5 = 17
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

#: A single movement larger than this many pixels counts as "warped by another
#: program": only the position is updated and no delta is produced, so that a jump
#: of thousands of pixels in one frame does not fling the cursor onto the screen
#: next door.
WARP_LIMIT = 600

ULONG_PTR = ctypes.c_size_t


# ------------------------------------------------------------------ structures
class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                                     ctypes.POINTER(RECT), wt.LPARAM)

user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(RECT),
                                       MONITORENUMPROC, wt.LPARAM]
user32.GetClipboardSequenceNumber.restype = wt.DWORD
user32.RegisterClipboardFormatW.restype = wt.UINT
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
user32.IsClipboardFormatAvailable.restype = wt.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [wt.HGLOBAL]

# The restypes below must be declared explicitly as pointer-sized, otherwise the
# default int truncates handles on 64-bit
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalFree.restype = wt.HGLOBAL
kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.EmptyClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wt.UINT]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.EnumClipboardFormats.restype = wt.UINT
user32.EnumClipboardFormats.argtypes = [wt.UINT]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.GetClipboardFormatNameW.argtypes = [wt.UINT, wt.LPWSTR, ctypes.c_int]


def _short(v: int) -> int:
    """Interpret the low 16 bits of a DWORD as a signed short (this is how the wheel delta is stored)."""
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _enable_dpi_awareness() -> str:
    """Must be called before any window/coordinate query, otherwise scaling garbles coordinates under high DPI."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "none"


class WindowsBackend(Backend):
    name = "windows"
    display_server = "windows"
    supports_capture = True
    supports_suppress = True
    supports_inject = True
    supports_clipboard = True

    def __init__(self, log: Optional[LogFn] = None):
        super().__init__(log=log)
        self._dpi = ""
        self._hook_thread: Optional[threading.Thread] = None
        self._hook_tid = 0
        self._kbd_hook = None
        self._mouse_hook = None
        self._kbd_proc = None
        self._mouse_proc = None
        self._ready = threading.Event()
        self._stopping = False
        self._last_pt: Optional[Tuple[int, int]] = None
        self._park: Optional[Tuple[int, int]] = None
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._send_failures = 0
        self._png_fmt: Optional[int] = None
        #: Maximum pixels for one image (defends against malformed data / a huge screenshot eating all the memory)
        self.image_pixel_cap = DEFAULT_PIXEL_CAP
        atexit.register(self.emergency_restore)

    # ------------------------------------------------------------ lifecycle
    def prepare(self) -> None:
        if not self._dpi:
            self._dpi = _enable_dpi_awareness()
            self.log("DPI awareness: %s" % self._dpi)
        p = POINT()
        if user32.GetCursorPos(ctypes.byref(p)):
            self._last_pt = (p.x, p.y)

    def close(self) -> None:
        self.emergency_restore()
        self.stop_capture()
        self._watchdog_stop.set()

    # ------------------------------------------------------------ geometry
    def monitors(self) -> List[Rect]:
        out: List[Rect] = []

        def cb(hmon, hdc, lprect, lparam):
            r = lprect.contents
            out.append(Rect(r.left, r.top, r.right - r.left, r.bottom - r.top))
            return True

        if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0):
            raise BackendError("EnumDisplayMonitors failed, no monitor information available")
        return out

    def desktop_rect(self) -> Rect:
        mons = self.monitors()
        if not mons:
            raise BackendError("no monitors detected")
        r = mons[0]
        for m in mons[1:]:
            r = r.union(m)
        return r

    # ------------------------------------------------------------ hooks
    def start_capture(self, sink: SinkFn) -> None:
        if self._hook_thread is not None and self._hook_thread.is_alive():
            self._sink = sink
            return
        self.prepare()
        self._sink = sink
        self._stopping = False
        self._ready.clear()
        self._hook_thread = threading.Thread(target=self._hook_loop,
                                             name="crosspc-hook", daemon=True)
        self._hook_thread.start()
        if not self._ready.wait(5.0):
            raise BackendError("timed out installing the keyboard/mouse hooks (security software may be blocking it)")
        if self._kbd_hook is None or self._mouse_hook is None:
            raise BackendError(self._last_hook_error or "failed to install the keyboard/mouse hooks")
        self.log("low-level keyboard/mouse hooks installed (DPI: %s)" % (self._dpi or "?"))

    _last_hook_error = ""

    def stop_capture(self) -> None:
        self.emergency_restore()
        thread = self._hook_thread
        if thread is None:
            return
        self._stopping = True
        if self._hook_tid:
            user32.PostThreadMessageW(self._hook_tid, 0x0012, 0, 0)   # WM_QUIT
        thread.join(3.0)
        self._hook_thread = None
        self._hook_tid = 0
        self.log("keyboard/mouse hooks removed")

    def _hook_loop(self) -> None:
        """Dedicated thread: install the hooks + run the message loop."""
        self._hook_tid = kernel32.GetCurrentThreadId()
        try:
            hmod = kernel32.GetModuleHandleW(None)
            self._kbd_proc = HOOKPROC(self._on_key_event)
            self._mouse_proc = HOOKPROC(self._on_mouse_event)
            self._kbd_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL,
                                                      self._kbd_proc, hmod, 0)
            if not self._kbd_hook:
                self._last_hook_error = ("SetWindowsHookEx (keyboard) failed: %s"
                                         % ctypes.WinError(ctypes.get_last_error()))
                return
            self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL,
                                                        self._mouse_proc, hmod, 0)
            if not self._mouse_hook:
                self._last_hook_error = ("SetWindowsHookEx (mouse) failed: %s"
                                         % ctypes.WinError(ctypes.get_last_error()))
                # If the mouse hook did not install, remove the keyboard hook as
                # well rather than leaving a half-installed state
                user32.UnhookWindowsHookEx(self._kbd_hook)
                self._kbd_hook = None
                return
        finally:
            self._ready.set()

        msg = wt.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._kbd_hook:
            user32.UnhookWindowsHookEx(self._kbd_hook)
            self._kbd_hook = None
        if self._mouse_hook:
            user32.UnhookWindowsHookEx(self._mouse_hook)
            self._mouse_hook = None

    # ------------------------------------------------------------ hook callbacks
    def _on_key_event(self, ncode, wparam, lparam) -> int:
        """Careful: this function is called from inside a low-level hook, so it must be extremely fast and must never raise."""
        try:
            if ncode == HC_ACTION:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                pressed = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                self._emit(Event.key(kb.scanCode, kb.vkCode, pressed,
                                     bool(kb.flags & LLKHF_EXTENDED)))
                if self._forwarding:
                    return 1
        except Exception:
            self._fail_safe()
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _on_mouse_event(self, ncode, wparam, lparam) -> int:
        try:
            if ncode == HC_ACTION:
                ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if wparam == WM_MOUSEMOVE:
                    self._on_move(ms.pt.x, ms.pt.y, bool(ms.flags & LLMHF_INJECTED))
                else:
                    ev = self._translate_mouse(wparam, ms.mouseData)
                    if ev is not None:
                        self._emit(ev)
                    if self._forwarding:
                        return 1
        except Exception:
            self._fail_safe()
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    @staticmethod
    def _translate_mouse(wparam: int, mouse_data: int) -> Optional[Event]:
        if wparam == WM_LBUTTONDOWN:
            return Event.button(BTN_LEFT, True)
        if wparam == WM_LBUTTONUP:
            return Event.button(BTN_LEFT, False)
        if wparam == WM_RBUTTONDOWN:
            return Event.button(BTN_RIGHT, True)
        if wparam == WM_RBUTTONUP:
            return Event.button(BTN_RIGHT, False)
        if wparam == WM_MBUTTONDOWN:
            return Event.button(BTN_MIDDLE, True)
        if wparam == WM_MBUTTONUP:
            return Event.button(BTN_MIDDLE, False)
        if wparam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            xbtn = (mouse_data >> 16) & 0xFFFF
            btn = BTN_BACK if xbtn == XBUTTON1 else BTN_FORWARD
            return Event.button(btn, wparam == WM_XBUTTONDOWN)
        if wparam == WM_MOUSEWHEEL:
            return Event.wheel(0, _short(mouse_data) // WHEEL_DELTA)
        if wparam == WM_MOUSEHWHEEL:
            return Event.wheel(_short(mouse_data) // WHEEL_DELTA, 0)
        return None

    def _on_move(self, x: int, y: int, injected: bool) -> None:
        last = self._last_pt
        self._last_pt = (x, y)
        if injected:
            # A fake event produced by our own SetCursorPos recenter: update the
            # baseline only, it does not count as movement
            return
        if last is None:
            self._emit(Event.motion(x, y, 0, 0))
            return
        dx, dy = x - last[0], y - last[1]
        if abs(dx) > WARP_LIMIT or abs(dy) > WARP_LIMIT:
            dx = dy = 0                    # warped directly by another program, ignore this jump
        self._emit(Event.motion(x, y, dx, dy))
        if self._forwarding and self._park is not None:
            px, py = self._park
            if (x, y) != (px, py):
                # Update the baseline before moving the cursor: SetCursorPos fires
                # an injected event synchronously, and by then _last_pt must
                # already be the park point, or a huge fake delta would be
                # computed.
                self._last_pt = (px, py)
                user32.SetCursorPos(px, py)

    def _emit(self, ev: Event) -> None:
        sink = self._sink
        if sink is not None:
            sink(ev)

    def _fail_safe(self) -> None:
        """An exception escaped a hook: drop the takeover at once and never swallow the user's keyboard and mouse for good."""
        if self._forwarding:
            self._forwarding = False
        self._sink = None

    # ------------------------------------------------------------ takeover
    def set_forwarding(self, on: bool) -> None:
        on = bool(on)
        if on and (self._hook_thread is None or not self._hook_thread.is_alive()):
            raise BackendError("hooks are not running, cannot enter takeover mode")
        if on and self._kbd_hook is None:
            raise BackendError("the keyboard hook is not ready, cannot enter takeover mode")
        if on:
            p = POINT()
            if user32.GetCursorPos(ctypes.byref(p)):
                self._last_pt = (p.x, p.y)
                if self._park is None:
                    self._park = (p.x, p.y)
            self._forwarding = True
            self._start_watchdog()
            self.log("entering takeover mode: local keyboard and mouse will be forwarded to the remote side")
        else:
            was = self._forwarding
            self._forwarding = False
            self._stop_watchdog()
            if was:
                self.log("leaving takeover mode: local keyboard and mouse restored")

    def set_park_point(self, x: int, y: int) -> None:
        self._park = (int(x), int(y))
        super().set_park_point(x, y)

    # ------------------------------------------------------------ watchdog
    def _start_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name="crosspc-watchdog", daemon=True)
        self._watchdog.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()

    def _watchdog_loop(self) -> None:
        """In takeover mode, keep an eye on the hook thread; the moment it dies, restore local input."""
        while not self._watchdog_stop.wait(0.5):
            if not self._forwarding:
                return
            if self._hook_thread is None or not self._hook_thread.is_alive():
                self.log("!! hook thread exited, emergency restore of local keyboard and mouse")
                self._forwarding = False
                self._sink = None
                return

    def emergency_restore(self) -> None:
        self._forwarding = False
        self._stop_watchdog()

    # ------------------------------------------------------------ cursor
    def cursor(self) -> Tuple[int, int]:
        p = POINT()
        if not user32.GetCursorPos(ctypes.byref(p)):
            raise BackendError("GetCursorPos failed")
        return p.x, p.y

    def set_cursor(self, x: int, y: int) -> None:
        self._last_pt = (int(x), int(y))
        if not user32.SetCursorPos(int(x), int(y)):
            self._last_pt = None

    # ------------------------------------------------------------ injection
    def _send(self, inp: INPUT) -> None:
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if n != 1:
            # Usually UIPI blocking it (the target window has higher privileges,
            # e.g. a program running as administrator). Warn only once, otherwise
            # a 1000 Hz mouse would flood the log
            self._send_failures += 1
            if self._send_failures == 1:
                self.log("SendInput was rejected (error code %d); if the target is "
                         "an elevated window, CrossPC must be run as administrator"
                         % ctypes.get_last_error())

    def _mouse_input(self, flags: int, dx: int = 0, dy: int = 0,
                     data: int = 0) -> INPUT:
        return INPUT(type=INPUT_MOUSE,
                     u=_INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF,
                                                 flags, 0, 0)))

    def inject_motion(self, x: int, y: int) -> None:
        self.set_cursor(x, y)

    def inject_button(self, button: int, pressed: bool) -> None:
        table = {
            BTN_LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            BTN_RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            BTN_MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
            BTN_BACK: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
            BTN_FORWARD: (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
        }
        pair = table.get(button)
        if pair is None:
            self.log("ignoring unknown mouse button: %r" % (button,))
            return
        flags = pair[0] if pressed else pair[1]
        data = 0
        if button in (BTN_BACK, BTN_FORWARD):
            data = XBUTTON1 if button == BTN_BACK else XBUTTON2
        self._send(self._mouse_input(flags, data=data))

    def inject_wheel(self, dx: int, dy: int) -> None:
        if dy:
            self._send(self._mouse_input(MOUSEEVENTF_WHEEL,
                                         data=(dy * WHEEL_DELTA) & 0xFFFFFFFF))
        if dx:
            self._send(self._mouse_input(MOUSEEVENTF_HWHEEL,
                                         data=(dx * WHEEL_DELTA) & 0xFFFFFFFF))

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        up = 0 if pressed else KEYEVENTF_KEYUP
        if vk == VK_PAUSE or not scancode:
            # Pause, and keys whose scancode is unavailable: fall back to the virtual-key code
            ki = KEYBDINPUT(vk & 0xFFFF, 0, up, 0, 0)
        else:
            flags = KEYEVENTF_SCANCODE | up
            if extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            ki = KEYBDINPUT(0, scancode & 0xFFFF, flags, 0, 0)
        self._send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=ki)))

    # ------------------------------------------------------------ clipboard
    def _open_clipboard(self, tries: int = 10, delay: float = 0.02) -> bool:
        for _ in range(tries):
            if user32.OpenClipboard(None):
                return True
            time.sleep(delay)              # the clipboard is often held by someone else, worth retrying
        return False

    def _get_clipboard_bytes(self, fmt: int) -> Optional[bytes]:
        """Read a clipboard format that is a "memory block (HGLOBAL)" and return the raw bytes."""
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return None
        size = int(kernel32.GlobalSize(handle))
        if size <= 0:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)

    def _set_clipboard_bytes(self, fmt: int, data: bytes) -> None:
        """Write a "memory block" format. The clipboard must already have been opened with OpenClipboard + EmptyClipboard."""
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise BackendError("GlobalAlloc failed (%d bytes)" % len(data))
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise BackendError("GlobalLock failed")
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
            raise BackendError("SetClipboardData (format %d) failed" % fmt)
        # After success the memory is owned by the system and must not be freed again

    def clipboard_text(self) -> Optional[str]:
        if not self._open_clipboard():
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_clipboard_text(self, text: str) -> None:
        if not self._open_clipboard():
            raise BackendError("cannot open the clipboard (another program is holding it)")
        try:
            if not user32.EmptyClipboard():
                raise BackendError("EmptyClipboard failed")
            self._set_clipboard_bytes(CF_UNICODETEXT,
                                      text.encode("utf-16-le") + b"\x00\x00")
        finally:
            user32.CloseClipboard()

    def clipboard_revision(self):
        seq = user32.GetClipboardSequenceNumber()
        return int(seq)

    def clipboard_formats(self) -> List[str]:
        """List which formats the clipboard currently holds (diagnostics only, read-only)."""
        out: List[str] = []
        if not self._open_clipboard():
            return out
        try:
            fmt = 0
            while True:
                fmt = int(user32.EnumClipboardFormats(fmt))
                if not fmt:
                    break
                if fmt >= 0xC000:                      # registered format: it has a name
                    buf = ctypes.create_unicode_buffer(128)
                    if user32.GetClipboardFormatNameW(fmt, buf, 128):
                        out.append(buf.value)
                    else:
                        out.append("fmt#%d" % fmt)
                else:
                    out.append({2: "CF_BITMAP", 8: "CF_DIB", 13: "CF_UNICODETEXT",
                                17: "CF_DIBV5"}.get(fmt, "CF_%d" % fmt))
        finally:
            user32.CloseClipboard()
        return out

    # ------------------------------------------------------------ clipboard images
    @property
    def supports_clipboard_images(self) -> bool:
        return True

    def _png_format_id(self) -> int:
        """On Windows "PNG" is a registered format (not a CF_ constant), so its id has to be looked up once first."""
        if self._png_fmt is None:
            self._png_fmt = int(user32.RegisterClipboardFormatW("PNG") or 0)
            if not self._png_fmt:
                self.log("failed to register the PNG clipboard format, images can only go through DIB")
        return self._png_fmt

    def clipboard_image_png(self, max_bytes: int = 0) -> Optional[bytes]:
        """Read the clipboard image -> PNG bytes.

        Order: registered format "PNG" -> CF_DIBV5 -> CF_DIB.
        PNG comes first because it is lossless and keeps alpha; some programs only
        offer DIB (the system screenshot tool does), so that is converted to PNG
        in place. A failed conversion falls back to the next candidate instead of
        failing the whole thing.
        """
        if not self._open_clipboard():
            return None
        try:
            fmt = self._png_format_id()
            if fmt and user32.IsClipboardFormatAvailable(fmt):
                data = self._get_clipboard_bytes(fmt)
                if data and data[:8] == PNG_SIG:
                    if max_bytes and len(data) > max_bytes:
                        self.log("clipboard image is %d bytes, over the cap of %d, not syncing"
                                 % (len(data), max_bytes))
                        return None
                    return data
            for cf in (CF_DIBV5, CF_DIB):
                if not user32.IsClipboardFormatAvailable(cf):
                    continue
                dib = self._get_clipboard_bytes(cf)
                if not dib:
                    continue
                try:
                    png = png_from_dib(dib, pixel_cap=self.image_pixel_cap)
                except ImageError as exc:
                    self.log("converting the DIB on the clipboard to PNG failed (%s), trying the next format" % exc)
                    continue
                if max_bytes and len(png) > max_bytes:
                    self.log("clipboard image is %d bytes once converted to PNG, over the cap of %d, not syncing"
                             % (len(png), max_bytes))
                    return None
                return png
            return None
        finally:
            user32.CloseClipboard()

    def set_clipboard_image_png(self, png: bytes) -> None:
        """Write a PNG into the clipboard: offer both the registered format "PNG" and CF_DIB.

        Why write it twice: modern programs (browsers/Office/the new Paint) take
        PNG directly (lossless, with alpha), while old programs only understand
        DIB. Writing PNG alone greys out pasting in some programs, and writing DIB
        alone loses transparency, so both are provided.
        """
        if not png:
            raise BackendError("the image is empty")
        if not self._open_clipboard():
            raise BackendError("cannot open the clipboard (another program is holding it)")
        wrote = 0
        try:
            if not user32.EmptyClipboard():
                raise BackendError("EmptyClipboard failed")
            fmt = self._png_format_id()
            if fmt:
                try:
                    self._set_clipboard_bytes(fmt, png)
                    wrote += 1
                except BackendError as exc:
                    self.log("writing the PNG format failed: %s" % exc)
            try:
                dib = dib_from_png(png, pixel_cap=self.image_pixel_cap)
            except ImageError as exc:
                self.log("this PNG cannot be decoded (%s), only the PNG format is "
                         "offered; old programs may not be able to paste it" % exc)
                dib = None
            if dib is not None:
                try:
                    self._set_clipboard_bytes(CF_DIB, dib)
                    wrote += 1
                except BackendError as exc:
                    self.log("writing the DIB format failed: %s" % exc)
            if not wrote:
                raise BackendError("failed to write the image to the clipboard (neither PNG nor DIB succeeded)")
        finally:
            user32.CloseClipboard()

    # ------------------------------------------------------------ self-check
    def probe(self) -> List[Tuple[str, bool, str]]:
        out: List[Tuple[str, bool, str]] = []
        try:
            mons = self.monitors()
            dr = self.desktop_rect()
            out.append(("monitors", True, "%d of them, virtual desktop %s" % (len(mons), dr)))
        except Exception as exc:
            out.append(("monitors", False, str(exc)))
        out.append(("DPI awareness", True, self._dpi or _enable_dpi_awareness()))
        # Really install the hooks once and then remove them: if they install,
        # capture/suppress is available (no key is suppressed, so it is safe)
        try:
            ok, detail = self._probe_hooks()
            out.append(("keyboard/mouse hooks", ok, detail))
        except Exception as exc:
            out.append(("keyboard/mouse hooks", False, "probe failed: %s" % exc))
        # Injection capability: only check the symbol, do not touch the real cursor
        out.append(("SendInput injection", bool(user32.SendInput), "available"))
        try:
            p = self.cursor()
            out.append(("cursor read", True, "current position %d,%d" % p))
        except Exception as exc:
            out.append(("cursor read", False, str(exc)))
        try:
            txt = self.clipboard_text()
            out.append(("clipboard", txt is not None,
                        "read %d characters" % len(txt) if txt else "empty/unreadable right now"))
        except Exception as exc:
            out.append(("clipboard", False, str(exc)))
        try:
            fmts = self.clipboard_formats()
            has_image = any(f in ("CF_DIB", "CF_DIBV5", "PNG") for f in fmts)
            out.append(("image clipboard", True,
                        "CF_DIB/CF_DIBV5/PNG supported; current clipboard formats: %s%s"
                        % (", ".join(fmts) or "(unreadable)",
                           " (contains an image)" if has_image else "")))
        except Exception as exc:
            out.append(("image clipboard", False, "query failed: %s" % exc))
        return out

    def _probe_hooks(self) -> Tuple[bool, str]:
        """Install the low-level hooks once on a temporary thread, then remove them right away."""
        state = {"kbd": None, "mouse": None, "err": "", "ready": threading.Event()}

        def loop():
            hmod = kernel32.GetModuleHandleW(None)
            kp = HOOKPROC(lambda n, w, l: user32.CallNextHookEx(None, n, w, l))
            mp = HOOKPROC(lambda n, w, l: user32.CallNextHookEx(None, n, w, l))
            state["kp"], state["mp"] = kp, mp          # keep the references alive against GC
            state["kbd"] = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kp, hmod, 0)
            state["mouse"] = user32.SetWindowsHookExW(WH_MOUSE_LL, mp, hmod, 0)
            if not state["kbd"] or not state["mouse"]:
                state["err"] = str(ctypes.WinError(ctypes.get_last_error()))
            state["ready"].set()
            time.sleep(0.3)
            if state["kbd"]:
                user32.UnhookWindowsHookEx(state["kbd"])
            if state["mouse"]:
                user32.UnhookWindowsHookEx(state["mouse"])

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        state["ready"].wait(3.0)
        t.join(2.0)
        if state["kbd"] and state["mouse"]:
            return True, "can install/uninstall, both capture and takeover are available"
        win = (kernel32.GetConsoleWindow() != 0)
        extra = "" if win else " (it may also fail to install without a console)"
        return False, "installation failed: %s%s" % (state["err"] or "unknown reason", extra)
