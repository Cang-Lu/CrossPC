"""Windows 后端: 低层键鼠钩子 + SendInput 注入 + 系统剪辑板。

只用 ctypes 调 user32/kernel32, 不需要 pip 装任何东西。

## 捕获与"接管"是怎么做的

* 用 WH_KEYBOARD_LL / WH_MOUSE_LL 两个低层钩子观察全局输入。钩子函数挂在
  一个专职线程上, 该线程只跑消息循环, 回调里**只做算术和入队**, 绝不写日志、
  绝不做网络 IO —— 低层钩子超时(默认 300ms)会被系统直接摘掉, 那意味着
  用户的键鼠突然"不听话", 这是本工具最危险的失败模式。
* 本地模式(未接管): 钩子看到了事件但一律放行, 我们只是"顺便"知道鼠标在哪、
  往哪推。用户完全感觉不到工具存在。
* 转发模式(接管): 钩子返回 1 把按键/滚轮/鼠标键全部吞掉, 交给上层转发给
  client。鼠标**移动**没法被钩子拦住(光标由输入栈直接更新), 所以我们用
  "回中(park)"的办法: 每收到一次移动就把光标 SetCursorPos 回到停靠点,
  位移量则通过相邻两次 pt 的差值算出来 —— 这样既拿到了不受限的位移,
  本机光标又始终停在屏幕边角不乱跑。
* 钩子函数里发生的异常会被 Python 打到 stderr 并让回调返回 0(=放行),
  再加上看门狗线程与 atexit, 保证"任何异常都不会把用户的键盘吞死"。

## 注入(client 角色)

SetCursorPos 做绝对定位, SendInput(KEYEVENTF_SCANCODE) 发按键, 用扫描码
而不是字符, 因此和 server 的键盘布局无关。
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

# ------------------------------------------------------------------ 常量
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

#: 单次移动超过这个像素数就当成"被别的程序 warp 了", 只更新位置不产生位移,
#: 免得一帧跳几千像素把光标甩到隔壁屏幕去。
WARP_LIMIT = 600

ULONG_PTR = ctypes.c_size_t


# ------------------------------------------------------------------ 结构体
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

# 下面这些 restype 必须显式声明成指针大小, 否则 64 位下默认 int 会把句柄截断
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
    """把 DWORD 的低 16 位当有符号短整型解释(滚轮增量就是这么存的)。"""
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _enable_dpi_awareness() -> str:
    """必须在任何窗口/坐标查询之前调用, 否则高 DPI 下坐标会被缩放搞乱。"""
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
        #: 单张图最多多少像素(防御畸形数据/超大截图把内存吃光)
        self.image_pixel_cap = DEFAULT_PIXEL_CAP
        atexit.register(self.emergency_restore)

    # ------------------------------------------------------------ 生命周期
    def prepare(self) -> None:
        if not self._dpi:
            self._dpi = _enable_dpi_awareness()
            self.log("DPI 感知: %s" % self._dpi)
        p = POINT()
        if user32.GetCursorPos(ctypes.byref(p)):
            self._last_pt = (p.x, p.y)

    def close(self) -> None:
        self.emergency_restore()
        self.stop_capture()
        self._watchdog_stop.set()

    # ------------------------------------------------------------ 几何
    def monitors(self) -> List[Rect]:
        out: List[Rect] = []

        def cb(hmon, hdc, lprect, lparam):
            r = lprect.contents
            out.append(Rect(r.left, r.top, r.right - r.left, r.bottom - r.top))
            return True

        if not user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0):
            raise BackendError("EnumDisplayMonitors 失败, 拿不到显示器信息")
        return out

    def desktop_rect(self) -> Rect:
        mons = self.monitors()
        if not mons:
            raise BackendError("没有检测到显示器")
        r = mons[0]
        for m in mons[1:]:
            r = r.union(m)
        return r

    # ------------------------------------------------------------ 钩子
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
            raise BackendError("安装键鼠钩子超时(可能被安全软件拦截)")
        if self._kbd_hook is None or self._mouse_hook is None:
            raise BackendError(self._last_hook_error or "安装键鼠钩子失败")
        self.log("已安装低层键鼠钩子(DPI: %s)" % (self._dpi or "?"))

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
        self.log("已卸载键鼠钩子")

    def _hook_loop(self) -> None:
        """专职线程: 装钩子 + 跑消息循环。"""
        self._hook_tid = kernel32.GetCurrentThreadId()
        try:
            hmod = kernel32.GetModuleHandleW(None)
            self._kbd_proc = HOOKPROC(self._on_key_event)
            self._mouse_proc = HOOKPROC(self._on_mouse_event)
            self._kbd_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL,
                                                      self._kbd_proc, hmod, 0)
            if not self._kbd_hook:
                self._last_hook_error = ("SetWindowsHookEx(键盘) 失败: %s"
                                         % ctypes.WinError(ctypes.get_last_error()))
                return
            self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL,
                                                        self._mouse_proc, hmod, 0)
            if not self._mouse_hook:
                self._last_hook_error = ("SetWindowsHookEx(鼠标) 失败: %s"
                                         % ctypes.WinError(ctypes.get_last_error()))
                # 鼠标钩子没装上就把键盘钩子也摘掉, 不留半拉子状态
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

    # ------------------------------------------------------------ 钩子回调
    def _on_key_event(self, ncode, wparam, lparam) -> int:
        """注意: 这个函数在低层钩子里被调用, 必须极快且不能抛异常。"""
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
            # 我们自己 SetCursorPos 回中产生的伪事件: 只更新基准, 不算位移
            return
        if last is None:
            self._emit(Event.motion(x, y, 0, 0))
            return
        dx, dy = x - last[0], y - last[1]
        if abs(dx) > WARP_LIMIT or abs(dy) > WARP_LIMIT:
            dx = dy = 0                    # 被别的程序直接 warp 了, 忽略这一跳
        self._emit(Event.motion(x, y, dx, dy))
        if self._forwarding and self._park is not None:
            px, py = self._park
            if (x, y) != (px, py):
                # 先更新基准再移动光标: SetCursorPos 会同步触发一次注入事件,
                # 那时 _last_pt 必须已经是停靠点, 否则会算出一大坨假位移。
                self._last_pt = (px, py)
                user32.SetCursorPos(px, py)

    def _emit(self, ev: Event) -> None:
        sink = self._sink
        if sink is not None:
            sink(ev)

    def _fail_safe(self) -> None:
        """钩子里出异常: 立刻放弃接管, 绝不把用户的键鼠吞死。"""
        if self._forwarding:
            self._forwarding = False
        self._sink = None

    # ------------------------------------------------------------ 接管
    def set_forwarding(self, on: bool) -> None:
        on = bool(on)
        if on and (self._hook_thread is None or not self._hook_thread.is_alive()):
            raise BackendError("钩子未运行, 不能进入接管模式")
        if on and self._kbd_hook is None:
            raise BackendError("密钥钩子未就绪, 不能进入接管模式")
        if on:
            p = POINT()
            if user32.GetCursorPos(ctypes.byref(p)):
                self._last_pt = (p.x, p.y)
                if self._park is None:
                    self._park = (p.x, p.y)
            self._forwarding = True
            self._start_watchdog()
            self.log("进入接管模式: 本机键鼠将转发到远端")
        else:
            was = self._forwarding
            self._forwarding = False
            self._stop_watchdog()
            if was:
                self.log("退出接管模式: 本机键鼠恢复")

    def set_park_point(self, x: int, y: int) -> None:
        self._park = (int(x), int(y))
        super().set_park_point(x, y)

    # ------------------------------------------------------------ 看门狗
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
        """接管模式下盯着钩子线程; 它一旦死了立刻恢复本机输入。"""
        while not self._watchdog_stop.wait(0.5):
            if not self._forwarding:
                return
            if self._hook_thread is None or not self._hook_thread.is_alive():
                self.log("!! 钩子线程已退出, 紧急恢复本机键鼠")
                self._forwarding = False
                self._sink = None
                return

    def emergency_restore(self) -> None:
        self._forwarding = False
        self._stop_watchdog()

    # ------------------------------------------------------------ 光标
    def cursor(self) -> Tuple[int, int]:
        p = POINT()
        if not user32.GetCursorPos(ctypes.byref(p)):
            raise BackendError("GetCursorPos 失败")
        return p.x, p.y

    def set_cursor(self, x: int, y: int) -> None:
        self._last_pt = (int(x), int(y))
        if not user32.SetCursorPos(int(x), int(y)):
            self._last_pt = None

    # ------------------------------------------------------------ 注入
    def _send(self, inp: INPUT) -> None:
        n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if n != 1:
            # 常见于 UIPI 拦截(目标窗口权限更高, 例如以管理员身份运行的程序),
            # 只提示一次, 不然 1000Hz 的鼠标会把日志刷爆
            self._send_failures += 1
            if self._send_failures == 1:
                self.log("SendInput 被拒绝 (错误码 %d); 若目标是管理员窗口, "
                         "需要以管理员身份运行 CrossPC" % ctypes.get_last_error())

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
            self.log("忽略未知鼠标键: %r" % (button,))
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
            # Pause 以及拿不到扫描码的键: 退回虚拟键码方式
            ki = KEYBDINPUT(vk & 0xFFFF, 0, up, 0, 0)
        else:
            flags = KEYEVENTF_SCANCODE | up
            if extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            ki = KEYBDINPUT(0, scancode & 0xFFFF, flags, 0, 0)
        self._send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=ki)))

    # ------------------------------------------------------------ 剪辑板
    def _open_clipboard(self, tries: int = 10, delay: float = 0.02) -> bool:
        for _ in range(tries):
            if user32.OpenClipboard(None):
                return True
            time.sleep(delay)              # 剪辑板常被别人占着, 值得重试
        return False

    def _get_clipboard_bytes(self, fmt: int) -> Optional[bytes]:
        """读一个"内存块(HGLOBAL)"类型的剪辑板格式, 返回原始字节。"""
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
        """写一个"内存块"格式。剪辑板必须已经被 OpenClipboard + EmptyClipboard。"""
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise BackendError("GlobalAlloc 失败(%d 字节)" % len(data))
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise BackendError("GlobalLock 失败")
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
            raise BackendError("SetClipboardData(格式 %d) 失败" % fmt)
        # 成功后内存所有权归系统, 不能再 free

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
            raise BackendError("打不开剪辑板(被其它程序占用)")
        try:
            if not user32.EmptyClipboard():
                raise BackendError("EmptyClipboard 失败")
            self._set_clipboard_bytes(CF_UNICODETEXT,
                                      text.encode("utf-16-le") + b"\x00\x00")
        finally:
            user32.CloseClipboard()

    def clipboard_revision(self):
        seq = user32.GetClipboardSequenceNumber()
        return int(seq)

    def clipboard_formats(self) -> List[str]:
        """列出剪辑板里现在有哪些格式(诊断用, 只读)。"""
        out: List[str] = []
        if not self._open_clipboard():
            return out
        try:
            fmt = 0
            while True:
                fmt = int(user32.EnumClipboardFormats(fmt))
                if not fmt:
                    break
                if fmt >= 0xC000:                      # 注册格式: 有名字
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

    # ------------------------------------------------------------ 剪辑板图片
    @property
    def supports_clipboard_images(self) -> bool:
        return True

    def _png_format_id(self) -> int:
        """Windows 上 "PNG" 是注册格式(不是 CF_ 常量), 要先查一次它的编号。"""
        if self._png_fmt is None:
            self._png_fmt = int(user32.RegisterClipboardFormatW("PNG") or 0)
            if not self._png_fmt:
                self.log("注册 PNG 剪辑板格式失败, 图片只能走 DIB")
        return self._png_fmt

    def clipboard_image_png(self, max_bytes: int = 0) -> Optional[bytes]:
        """读剪辑板图片 -> PNG 字节。

        顺序: 注册格式 "PNG" -> CF_DIBV5 -> CF_DIB。
        先看 PNG 是因为它无损且保留 alpha; 有些程序只给 DIB(系统截图就是),
        那就地转成 PNG。转换失败会退回下一个候选, 不会整条失败。
        """
        if not self._open_clipboard():
            return None
        try:
            fmt = self._png_format_id()
            if fmt and user32.IsClipboardFormatAvailable(fmt):
                data = self._get_clipboard_bytes(fmt)
                if data and data[:8] == PNG_SIG:
                    if max_bytes and len(data) > max_bytes:
                        self.log("剪辑板图片 %d 字节, 超过上限 %d, 不同步"
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
                    self.log("剪辑板里的 DIB 转 PNG 失败(%s), 试下一种格式" % exc)
                    continue
                if max_bytes and len(png) > max_bytes:
                    self.log("剪辑板图片转成 PNG 后 %d 字节, 超过上限 %d, 不同步"
                             % (len(png), max_bytes))
                    return None
                return png
            return None
        finally:
            user32.CloseClipboard()

    def set_clipboard_image_png(self, png: bytes) -> None:
        """把 PNG 写进剪辑板: 同时提供注册格式 "PNG" 和 CF_DIB。

        为什么要写两份: 现代程序(浏览器/Office/新版画图)直接吃 PNG(无损、带
        alpha), 而老程序只认 DIB。只写 PNG 会让一部分程序粘贴变灰, 只写 DIB
        会丢透明度, 所以两个都给。
        """
        if not png:
            raise BackendError("图片内容为空")
        if not self._open_clipboard():
            raise BackendError("打不开剪辑板(被其它程序占用)")
        wrote = 0
        try:
            if not user32.EmptyClipboard():
                raise BackendError("EmptyClipboard 失败")
            fmt = self._png_format_id()
            if fmt:
                try:
                    self._set_clipboard_bytes(fmt, png)
                    wrote += 1
                except BackendError as exc:
                    self.log("写 PNG 格式失败: %s" % exc)
            try:
                dib = dib_from_png(png, pixel_cap=self.image_pixel_cap)
            except ImageError as exc:
                self.log("这张 PNG 解不开(%s), 只提供 PNG 格式; "
                         "老程序可能粘贴不了" % exc)
                dib = None
            if dib is not None:
                try:
                    self._set_clipboard_bytes(CF_DIB, dib)
                    wrote += 1
                except BackendError as exc:
                    self.log("写 DIB 格式失败: %s" % exc)
            if not wrote:
                raise BackendError("图片写剪辑板失败(PNG 与 DIB 都没成功)")
        finally:
            user32.CloseClipboard()

    # ------------------------------------------------------------ 自检
    def probe(self) -> List[Tuple[str, bool, str]]:
        out: List[Tuple[str, bool, str]] = []
        try:
            mons = self.monitors()
            dr = self.desktop_rect()
            out.append(("显示器", True, "%d 个, 虚拟桌面 %s" % (len(mons), dr)))
        except Exception as exc:
            out.append(("显示器", False, str(exc)))
        out.append(("DPI 感知", True, self._dpi or _enable_dpi_awareness()))
        # 真装一次钩子再卸掉: 能装说明捕获/抑制可用(不吞任何键, 安全)
        try:
            ok, detail = self._probe_hooks()
            out.append(("键鼠钩子", ok, detail))
        except Exception as exc:
            out.append(("键鼠钩子", False, "探测失败: %s" % exc))
        # 注入能力: 只查符号, 不动真实光标
        out.append(("SendInput 注入", bool(user32.SendInput), "可用"))
        try:
            p = self.cursor()
            out.append(("光标读取", True, "当前位置 %d,%d" % p))
        except Exception as exc:
            out.append(("光标读取", False, str(exc)))
        try:
            txt = self.clipboard_text()
            out.append(("剪辑板", txt is not None,
                        "读到 %d 个字符" % len(txt) if txt else "当前为空/不可读"))
        except Exception as exc:
            out.append(("剪辑板", False, str(exc)))
        try:
            fmts = self.clipboard_formats()
            has_image = any(f in ("CF_DIB", "CF_DIBV5", "PNG") for f in fmts)
            out.append(("图片剪辑板", True,
                        "支持 CF_DIB/CF_DIBV5/PNG; 当前剪辑板格式: %s%s"
                        % (", ".join(fmts) or "(读不到)",
                           "(含图片)" if has_image else "")))
        except Exception as exc:
            out.append(("图片剪辑板", False, "查询失败: %s" % exc))
        return out

    def _probe_hooks(self) -> Tuple[bool, str]:
        """在临时线程里装一次低层钩子, 立刻卸载。"""
        state = {"kbd": None, "mouse": None, "err": "", "ready": threading.Event()}

        def loop():
            hmod = kernel32.GetModuleHandleW(None)
            kp = HOOKPROC(lambda n, w, l: user32.CallNextHookEx(None, n, w, l))
            mp = HOOKPROC(lambda n, w, l: user32.CallNextHookEx(None, n, w, l))
            state["kp"], state["mp"] = kp, mp          # 保持引用防 GC
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
            return True, "可安装/可卸载, 捕获与接管均可用"
        win = (kernel32.GetConsoleWindow() != 0)
        extra = "" if win else " (无控制台时也可能装不上)"
        return False, "安装失败: %s%s" % (state["err"] or "未知原因", extra)
