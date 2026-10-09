"""Linux X11 注入器: 用 ctypes 直接调 libX11 + libXtst 的 XTest 扩展。

为什么独立成一个小类(不继承 Backend):
    它只是"把事件写进某个 X display"的薄封装, 不承担平台自检/剪辑板/几何
    这些后端职责; LinuxBackend 把它当成一个可替换的注入策略来用。这样
    doctor 也能在不开设备的前提下单独问 available()。

不放抬头的理由(和 linux_uinput 一样):
    本模块顶层只定义常量与 ctypes 签名, libX11.so.6 / libXtst.so.6 的
    ctypes.CDLL(...) 一律推迟到 open()/available() 里。于是本文件在
    Windows 上 import、compileall 都不会炸。

线程安全:
    Xlib 本身不是线程安全的。多线程(捕获线程 + 注入线程)下必须先
    XInitThreads(), 且必须在**任何**其它 Xlib 调用之前调用 —— open() 里
    第一件事就是它。即使如此, 这里仍然用一把锁把"一批事件 + flush"串起来,
    避免两个线程的事件在请求缓冲里交错。

flush 策略:
    注入方法默认不立刻 XFlush(否则每个鼠标移动一个 RTT, 1000Hz 的鼠标会
    把 X 连接打满)。内部每攒够 FLUSH_EVERY 个事件自动 flush 一次, 调用方
    也可以在事件批次结束时调 flush()。close() 一定会 flush。
"""
from __future__ import annotations

import ctypes
import os
import threading
from typing import Callable, Dict, Optional, Tuple

from .. import keys

#: 库名候选。绝大多数发行版是 libX11.so.6 / libXtst.so.6, 但有些最小化
#: 镜像(以及某些 BSD 风格的 ABI)只有无版本号的 .so, 所以都试一遍。
X11_LIB_CANDIDATES = ("libX11.so.6", "libX11.so")
XTST_LIB_CANDIDATES = ("libXtst.so.6", "libXtst.so")

#: 攒够这么多事件就自动 flush 一次
FLUSH_EVERY = 8

# XTest 鼠标按键编号就是 X11 的按钮编号(与 events.py 一致), 只是滚轮
# 在 X 里也是"按钮": 4=上 5=下 6=左 7=右。这里显式写出来当文档。
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
    """按候选顺序找第一个能加载的库, 返回 (库对象或 None, 说明)。"""
    errors = []
    for name in candidates:
        try:
            return ctypes.CDLL(name), name
        except OSError as exc:
            errors.append("%s: %s" % (name, exc))
    return None, "; ".join(errors) or "找不到库"


class X11Injector:
    """把一个 X display 当作注入目标。name 供日志/诊断展示。"""

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
        #: (scancode, extended) -> keycode, 避免每次都问 X 服务器
        self._keycode_cache: Dict[Tuple[int, bool], int] = {}
        #: 已经按下的 X 按钮, 用于 close() 时兜底抬起, 防粘键
        self._pressed_buttons = set()

    def _say(self, msg: str) -> None:
        self._log("[x11] %s" % msg)

    # ------------------------------------------------------------ 生命周期
    @property
    def opened(self) -> bool:
        return bool(self._dpy)

    def open(self) -> None:
        """加载库、XInitThreads、XOpenDisplay、查 XTest 扩展、缓存 root window。"""
        if self._dpy:
            return
        display = self._display_name or os.environ.get("DISPLAY") or ""
        if not display:
            raise RuntimeError(
                "环境变量 DISPLAY 为空, 没法注入 X11。请在图形会话里运行"
                "(例如在桌面终端里执行, 或确认 SSH 带 -X/已设置 DISPLAY); "
                "如果这台机器是纯 Wayland 会话, 请改用 uinput 注入方式。")

        x11, x11_name = _load_library(X11_LIB_CANDIDATES)
        if x11 is None:
            raise RuntimeError(
                "加载 libX11 失败(%s)。请安装 X11 运行库: "
                "Debian/Ubuntu 上 sudo apt install libx11-6 libxtst6" % x11_name)
        xtst, xtst_name = _load_library(XTST_LIB_CANDIDATES)
        if xtst is None:
            raise RuntimeError(
                "加载 libXtst 失败(%s)。XTest 扩展在单独的库里, 请安装: "
                "Debian/Ubuntu 上 sudo apt install libxtst6" % xtst_name)

        self._declare_signatures(x11, xtst)
        # 必须在任何其它 Xlib 调用之前: 之后 Xlib 才会用内部锁保护自己。
        x11.XInitThreads()

        dpy = x11.XOpenDisplay(display.encode("utf-8") if display else None)
        if not dpy:
            raise RuntimeError(
                "XOpenDisplay(%r) 失败: X 服务器连不上。请确认 DISPLAY 指向一个"
                "活着的 X 会话, 且当前用户有权限访问它(xhost +local: 只是临时办法)。"
                % display)
        self._x11 = x11
        self._xtst = xtst
        self._dpy = ctypes.c_void_p(dpy)
        self._root = x11.XDefaultRootWindow(self._dpy)

        # 有些 X 服务器/嵌套服务器没编 XTest, 提前问一次
        ev_base = ctypes.c_int(0)
        err_base = ctypes.c_int(0)
        major = ctypes.c_int(0)
        minor = ctypes.c_int(0)
        if not xtst.XTestQueryExtension(self._dpy, ctypes.byref(ev_base),
                                        ctypes.byref(err_base),
                                        ctypes.byref(major), ctypes.byref(minor)):
            self.close()
            raise RuntimeError(
                "X 服务器没有 XTest 扩展, 无法注入键鼠事件。"
                "如果这是 Wayland 会话, 请改用 uinput 注入方式。")
        self._say("已连接 X11 display=%r XTest %d.%d"
                  % (display, major.value, minor.value))

    @staticmethod
    def _declare_signatures(x11: ctypes.CDLL, xtst: ctypes.CDLL) -> None:
        """显式声明参数/返回类型。

        必须做这一步: ctypes 默认把参数当 int 传, 而 Xlib 的 Dpy*/Window/KeyCode
        在 64 位上是 8 字节指针/unsigned long。不声明就会把指针截成 32 位,
        典型症状是段错误或"注入到火星去"。
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
        # 注意 Window 是 XID = unsigned long(64 位 8 字节), 坐标是 int。
        x11.XWarpPointer.restype = ctypes.c_int
        x11.XWarpPointer.argtypes = [P, ctypes.c_ulong, ctypes.c_ulong,
                                     ctypes.c_int, ctypes.c_int,
                                     ctypes.c_uint, ctypes.c_uint,
                                     ctypes.c_int, ctypes.c_int]
        # 能用 XTestFakeMotionEvent 时优先用它: 它按"屏幕坐标"发相对根窗口的
        # 移动事件, 是合成点击的标准做法; XWarpPointer 会真的移动 X 指针, 在
        # 某些多屏/指针约束(confine_to)场景下会被顶回来。两者都是绝对定位。
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
        """抬起还按着的鼠标键、flush、关掉 display。幂等, 可从任意线程调用。"""
        with self._lock:
            dpy = self._dpy
            if not dpy:
                return
            # 兜底: 断开时如果还有按下的鼠标键, 先抬起来, 免得对面粘键
            for btn in sorted(self._pressed_buttons):
                try:
                    self._xtst.XTestFakeButtonEvent(dpy, btn, 0, 0)
                except Exception:
                    pass
            self._pressed_buttons.clear()
            # keycode 缓存依赖这个 display(不同 display 的映射可能不同), 必须清
            self._keycode_cache.clear()
            try:
                self._x11.XFlush(dpy)
                self._x11.XCloseDisplay(dpy)
            except Exception as exc:           # pragma: no cover
                self._say("关闭 X display 时出错(忽略): %s" % exc)
            self._dpy = ctypes.c_void_p(None)
            self._pending = 0

    # ------------------------------------------------------------ flush
    def _bump(self) -> None:
        """记一笔"缓冲里多了一个事件", 够数就自动 flush。"""
        self._pending += 1
        if self._pending >= FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        """把攒着的请求推给 X 服务器。调用方在批次结束时调一次。"""
        if not self._dpy:
            return
        try:
            self._x11.XFlush(self._dpy)
        finally:
            self._pending = 0

    def sync(self) -> None:
        """flush 并等 X 服务器处理完(诊断/测试用, 比 flush 慢)。"""
        if self._dpy:
            self._x11.XSync(self._dpy, 0)
            self._pending = 0

    def _require(self) -> ctypes.c_void_p:
        if not self._dpy:
            raise RuntimeError("X11 注入器尚未打开(先调用 open())")
        return self._dpy

    # ------------------------------------------------------------ 几何
    def screen_size(self) -> Tuple[int, int]:
        """默认屏幕的像素尺寸(XDisplayWidth/Height)。"""
        dpy = self._require()
        scr = self._x11.XDefaultScreen(dpy)
        return (int(self._x11.XDisplayWidth(dpy, scr)),
                int(self._x11.XDisplayHeight(dpy, scr)))

    def pointer(self) -> Tuple[int, int]:
        """读当前指针位置(XQueryPointer)。读不到时返回 (0, 0)。"""
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

    # ------------------------------------------------------------ 注入
    def inject_motion(self, x: int, y: int) -> None:
        """绝对定位到屏幕像素 (x, y)。"""
        with self._lock:
            dpy = self._require()
            if self._xtst:
                self._xtst.XTestFakeMotionEvent(dpy, -1, int(x), int(y), 0)
            else:                              # pragma: no cover - open() 里已保证
                self._x11.XWarpPointer(dpy, 0, self._root, 0, 0, 0, 0,
                                       int(x), int(y))
            self._bump()

    def inject_button(self, button: int, pressed: bool) -> None:
        """X11 按钮编号 1/2/3 左中右, 8/9 后退/前进, 原样交给 XTest。"""
        btn = int(button)
        if btn not in (1, 2, 3, 8, 9):
            self._say("未知鼠标按键 %r, 已跳过" % (button,))
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
        """滚轮: 每个格数发一对 press+release。dy 上为正, dx 右为正。"""
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
        """(扫描码, 扩展) -> X keycode。keysym 找不到或该 keysym 在当前
        键盘映射里没有 keycode 时返回 None(调用方跳过并记日志)。"""
        ck = (int(scancode), bool(extended))
        cached = self._keycode_cache.get(ck)
        if cached is not None:
            return cached
        keysym = keys.keysym_for(scancode, extended)
        if keysym is None:
            return None
        code = int(self._x11.XKeysymToKeycode(self._dpy, ctypes.c_ulong(keysym)))
        if not code:                            # X 返回 0 = 该 keysym 未绑定
            return None
        self._keycode_cache[ck] = code
        return code

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        with self._lock:
            dpy = self._require()
            code = self.keycode_for(scancode, extended)
            if code is None:
                self._say("扫描码 0x%02X(%s) 在当前 X 键盘映射里没有对应 keycode, 已跳过"
                          % (scancode, keys.key_name(scancode, vk, extended)))
                return
            self._xtst.XTestFakeKeyEvent(dpy, ctypes.c_uint(code),
                                         1 if pressed else 0, 0)
            self._bump()

    # ------------------------------------------------------------ 自检
    @staticmethod
    def available() -> Tuple[bool, str]:
        """(能否用 X11 注入, 中文说明)。绝不抛异常, 供 doctor 用。

        注意: 这里会真的连一次 X 服务器并立刻断开 —— 这是唯一可靠的判断方法
        (只看 DISPLAY 变量存在是骗人的, 常见于 SSH 里残留的 DISPLAY)。
        它不注入任何事件, 所以对用户无副作用。
        """
        display = os.environ.get("DISPLAY") or ""
        if not display:
            return (False, "DISPLAY 为空: 没有 X 会话可用; 纯 Wayland 会话请用 uinput 注入")

        x11, why_x11 = _load_library(X11_LIB_CANDIDATES)
        if x11 is None:
            return (False, "加载 libX11 失败(%s): Debian 上 sudo apt install libx11-6" % why_x11)
        xtst, why_xtst = _load_library(XTST_LIB_CANDIDATES)
        if xtst is None:
            return (False, "加载 libXtst 失败(%s): Debian 上 sudo apt install libxtst6" % why_xtst)

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
        return (True, "DISPLAY=%s 可用, XTest 扩展就绪, 屏幕 %dx%d"
                % (display, w, h))
