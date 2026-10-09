"""Linux /dev/uinput 注入器: 合成一个**绝对定位**的虚拟指针设备。

为什么是绝对设备(而不是相对鼠标):
    相对鼠标(REL_X/REL_Y)写进去以后, 显示服务器/合成器会照样给它套上
    "指针加速"和"提高指针精确度"之类的处理, 于是 CrossPC 算出来的位移
    到了屏幕上就是错的, 而且是累积漂移的 —— 只要指针加速开着, 用相对
    设备做 KVM 就不可能准。
    绝对设备(类 tablet/触摸屏)走的是"直接跳到这个坐标"的语义, 合成器对
    它不做加速, 只做一次线性映射, 所以 inject_motion(x, y) 给的绝对像素
    坐标就是最终落点。代价是设备必须知道屏幕分辨率, 于是有 set_screen_size()。

本模块顶层不加载任何东西、不打开任何设备: ctypes/结构体/常量都是纯 Python
定义, 打开 /dev/uinput 只发生在 open() 里。这样 `python -m compileall` 以
及在 Windows 上 import 本模块都不会炸(见 tests/test_linux_backend.py)。

ioctl 常量的来源与算法: 见下面 _IOW / _IO 的注释(linux/uinput.h + asm-generic/ioctl.h)。
"""
from __future__ import annotations

import ctypes
import errno
import os
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from .. import keys

# ---------------------------------------------------------------------------
# ioctl 编号手算
#
# 来源: linux/asm-generic/ioctl.h
#     _IOC(dir,type,nr,size) = (dir << 30) | (size << 16) | (type << 8) | nr
#     _IO (type,nr)  = _IOC(0, type, nr, 0)      无数据
#     _IOW(type,nr,size 参数的类型) = _IOC(1, type, nr, sizeof(那个类型))
#     _IOR 同 _IOW 只是 dir=2
# uinput 的 type 永远是 'U' = 0x55; size 是"内核在 ioctl 里期望的数据大小"。
#
# 校验(可以和 /usr/include/linux/uinput.h 对照):
#     _IO ('U',1) = 0x5501                       UI_DEV_CREATE
#     _IO ('U',2) = 0x5502                       UI_DEV_DESTROY
#     _IOW('U',3, struct uinput_setup)           UI_DEV_SETUP
#     _IOW('U',100,int) = (1<<30)|(4<<16)|(0x55<<8)|100 = 0x40045564  UI_SET_EVBIT
#     _IOW('U',101,int) =                        0x40045565  UI_SET_KEYBIT
#     _IOW('U',102,int) =                        0x40045566  UI_SET_RELBIT
#     _IOW('U',103,int) =                        0x40045567  UI_SET_ABSBIT
#     _IOW('U',104,int) =                        0x40045568  UI_SET_MSCBIT
#     _IOW('U',105,int) =                        0x40045569  UI_SET_LEDBIT
#     _IOW('U',106,int) =                        0x4004556A  UI_SET_SNDBIT
#     _IOW('U',107,int) =                        0x4004556B  UI_SET_FFBIT
#     _IOW('U',108,int) =                        0x4004556C  UI_SET_PHYS
#     _IOW('U',109,int) =                        0x4004556D  UI_SET_SWBIT
#     _IOW('U',110,int) =                        0x4004556E  UI_SET_PROPBIT
#     _IOW('U',111,int) =                        0x4004556F  UI_SET_ABSBIT_SETUP
#     _IOW('U',112,struct uinput_abs_setup)       UI_ABS_SETUP
# 这里有个很容易踩的坑: UINPUT_IOCTL_BASE + 4 那个算法(把 nr 当成 4/5/6/7)
# 是**错的**, 会得到 0x40045504 这种内核根本不认的编号, ioctl 直接 ENOTTY。
# 正确的 nr 是 100 起步(见 uinput.h: UINPUT_IOCTL_BASE + 100 = UI_SET_EVBIT)。
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction: int, type_: int, nr: int, size: int) -> int:
    return ((direction << 30) | (size << 16) | (type_ << 8) | nr) & 0xFFFFFFFF


def _IO(type_: int, nr: int) -> int:
    return _IOC(0, type_, nr, 0)


def _IOW(type_: int, nr: int, size: int) -> int:
    return _IOC(_IOC_WRITE, type_, nr, size)


UINPUT_TYPE = ord("U")                                   # 'U' = 0x55

UI_DEV_CREATE = _IO(UINPUT_TYPE, 1)                      # 0x5501
UI_DEV_DESTROY = _IO(UINPUT_TYPE, 2)                     # 0x5502
UI_DEV_SETUP = _IOW(UINPUT_TYPE, 3, 92)                  # 0x405C5503 (sizeof(struct uinput_setup)=92)
UI_SET_EVBIT = _IOW(UINPUT_TYPE, 100, 4)                 # 0x40045564
UI_SET_KEYBIT = _IOW(UINPUT_TYPE, 101, 4)                # 0x40045565
UI_SET_RELBIT = _IOW(UINPUT_TYPE, 102, 4)                # 0x40045566
UI_SET_ABSBIT = _IOW(UINPUT_TYPE, 103, 4)                # 0x40045567
UI_SET_PROPBIT = _IOW(UINPUT_TYPE, 110, 4)               # 0x4004556E

# ---------------------------------------------------------------------------
# 事件类型 / 事件码 (linux/input-event-codes.h)
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
SYN_REPORT = 0x00

REL_HWHEEL = 0x06
REL_WHEEL = 0x08

ABS_X = 0x00
ABS_Y = 0x01
#: 绝对坐标的取值范围。0..65535 是内核 BTN_TOOL/ABS 类设备事实上的通用刻度,
#: 2560x1440 这样的分辨率映射过去也远不到 1 像素的量化误差。
ABS_MAX = 65535

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114

#: X11 按钮编号(events.py 的约定) -> evdev BTN_*
#: 8/9 是 X 的后退/前进键, 对应 BTN_SIDE/BTN_EXTRA。
BUTTON_TO_EVDEV: Dict[int, int] = {
    1: BTN_LEFT,
    2: BTN_MIDDLE,
    3: BTN_RIGHT,
    8: BTN_SIDE,
    9: BTN_EXTRA,
}

INPUT_PROP_POINTER = 0

#: struct input_event 的大小: struct timeval(16) + type(2) + code(2) + value(4)
INPUT_EVENT_SIZE = 24
#: struct uinput_setup 的大小: input_id(8) + name[80] + ff_effects_max(4)
UINPUT_SETUP_SIZE = 92
#: struct uinput_user_dev 的大小: name[80] + input_id(8) + ff_effects_max(4) + absmax[64]*5*4
UINPUT_USER_DEV_SIZE = 80 + 8 + 4 + 64 * 5 * 4    # 1372

#: 设备名(必须是 bytes, 内核按 C 字符串读)
DEVICE_NAME = b"CrossPC Virtual Pointer"

#: 扫描码表里有、但 evdev 码对不上普通区的几个键(见 keys.py 的注释)
_EXTRA_EVDEV_CODES = (
    keys.EVDEV_PAUSE,        # 119 KEY_PAUSE
    keys.EVDEV_SYSRQ,        # 99  KEY_SYSRQ
    58,                      # KEY_CAPSLOCK (0x3A 普通区 evdev 码不是 0x3A)
    69,                      # KEY_NUMLOCK
    70,                      # KEY_SCROLLLOCK
)


# ---------------------------------------------------------------------------
# 结构体布局
class _TimeVal(ctypes.Structure):
    """struct timeval: 在 64 位 Linux 上是两个 8 字节(long)。"""

    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _InputEvent(ctypes.Structure):
    """struct input_event (linux/input.h):

        struct timeval time;  __u16 type;  __u16 code;  __s32 value;
    """

    _fields_ = [("time", _TimeVal),
                ("type", ctypes.c_uint16),
                ("code", ctypes.c_uint16),
                ("value", ctypes.c_int32)]


class _InputId(ctypes.Structure):
    """struct input_id (linux/input.h): 总线/厂商/产品/版本各 2 字节。"""

    _fields_ = [("bustype", ctypes.c_uint16),
                ("vendor", ctypes.c_uint16),
                ("product", ctypes.c_uint16),
                ("version", ctypes.c_uint16)]


class _UinputSetup(ctypes.Structure):
    """struct uinput_setup (linux/uinput.h) —— 新接口 UI_DEV_SETUP 的载荷。

        struct input_id id;  char name[UINPUT_MAX_NAME_SIZE];  __u32 ff_effects_max;
    """

    _fields_ = [("id", _InputId),
                ("name", ctypes.c_char * 80),
                ("ff_effects_max", ctypes.c_uint32)]


class _UinputAbsinfo(ctypes.Structure):
    """struct input_absinfo (linux/input.h): 每个绝对轴 5 个 __s32。"""

    _fields_ = [("value", ctypes.c_int32), ("minimum", ctypes.c_int32),
                ("maximum", ctypes.c_int32), ("fuzz", ctypes.c_int32),
                ("flat", ctypes.c_int32),
                # resolution 是较新内核才加的, 但对 uinput_user_dev(老接口)而言
                # 内核只读前 5 个字段(每个轴 20 字节), 所以这里不能加, 否则
                # absmax 数组的元素步长就错了。
                ]


class _UinputUserDev(ctypes.Structure):
    """struct uinput_user_dev (linux/uinput.h) —— 老接口, 直接 write() 给 fd。

        char name[80];  struct input_id id;  __u32 ff_effects_max;
        __s32 absmax[ABS_CNT][5];     // ABS_CNT = 64
    """

    _fields_ = [("name", ctypes.c_char * 80),
                ("id", _InputId),
                ("ff_effects_max", ctypes.c_uint32),
                ("absmax", _UinputAbsinfo * 64)]


# ---------------------------------------------------------------------------
# 纯函数(不碰设备, 可在任意平台单测)
def pixel_to_abs(x: int, y: int, screen_w: int, screen_h: int) -> Tuple[int, int]:
    """像素坐标 -> 绝对设备刻度 0..65535。

    线性映射并把边界钉死: 0 -> 0, w-1 -> 65535。用"四舍五入"而不是截断,
    否则 w-1 只会得到 65534, 屏幕最右下角那一列/一行永远点不到。
    屏幕尺寸非法(<=0)时退化为 0..65535 的直通, 这样至少不会抛异常。
    """
    if screen_w <= 0 or screen_h <= 0:
        return (min(max(int(x), 0), ABS_MAX), min(max(int(y), 0), ABS_MAX))
    ax = int(round(float(x) * ABS_MAX / float(screen_w - 1))) if screen_w > 1 else 0
    ay = int(round(float(y) * ABS_MAX / float(screen_h - 1))) if screen_h > 1 else 0
    return (min(max(ax, 0), ABS_MAX), min(max(ay, 0), ABS_MAX))


def pack_event(type_: int, code: int, value: int,
               sec: Optional[int] = None, usec: Optional[int] = None) -> bytes:
    """把一条 input_event 编码成 24 字节(纯函数, 便于单测)。

    时间戳用 struct 而不是 ctypes 结构体来打包: 这样本函数在 Windows 上也能
    正确算出 24 字节(Windows 的 c_long 只有 4 字节, ctypes 结构体会是 16 字节)。
    """
    if sec is None:
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1000000)
    usec = int(usec or 0)
    # 防止浮点误差把 usec 顶到 1000000(内核会当成非法时间戳)
    if usec >= 1000000:
        sec += usec // 1000000
        usec %= 1000000
    elif usec < 0:
        usec = 0
    return struct.pack("=qqHHi", int(sec), usec,
                       int(type_) & 0xFFFF, int(code) & 0xFFFF,
                       int(value))


def button_to_evdev(button: int) -> Optional[int]:
    """X11 按钮编号 -> evdev BTN_*, 未知返回 None(调用方记日志并跳过)。"""
    return BUTTON_TO_EVDEV.get(int(button))


def keyboard_evdev_codes() -> Set[int]:
    """把 keys.py 里出现过的扫描码全部翻成 evdev 码, 供注册 KEYBIT。

    直接遍历 keys.py 的两张表(而不是另抄一份键表): 以后 keys.py 加了键,
    这里自动跟着注册, 不会出现"表里有、设备没注册"的哑键。
    """
    codes: Set[int] = set(_EXTRA_EVDEV_CODES)
    for scan in keys.SCAN_TO_KEYSYM:
        code = keys.evdev_for(scan, 0, False)
        if code:
            codes.add(code)
    for scan in keys.EXT_SCAN_TO_EVDEV:
        code = keys.evdev_for(scan, 0, True)
        if code:
            codes.add(code)
    return codes


# ---------------------------------------------------------------------------
class UInputInjector:
    """用 /dev/uinput 合成虚拟指针 + 键盘, 做绝对定位注入。

    线程约定: 实例内部用一把锁保护 write(), 可以从任意线程调用注入方法。
    """

    name = "uinput"

    def __init__(self, log: Optional[Callable[[str], None]] = None,
                 device_path: str = "/dev/uinput"):
        self._log = log or (lambda m: None)
        self.device_path = device_path
        self._fd: Optional[int] = None
        self._lock = threading.RLock()
        self._screen_size: Optional[Tuple[int, int]] = None
        self._created = False

    def _say(self, msg: str) -> None:
        self._log("[uinput] %s" % msg)

    # ------------------------------------------------------------ 生命周期
    @property
    def opened(self) -> bool:
        return self._fd is not None

    def open(self) -> None:
        """打开 /dev/uinput 并注册设备能力 + UI_DEV_CREATE。幂等。"""
        if self._fd is not None:
            return
        try:
            import fcntl                       # 只在 Linux 上存在, 延迟 import
        except ImportError as exc:             # pragma: no cover - Windows
            raise RuntimeError(
                "当前平台没有 fcntl 模块, 无法使用 /dev/uinput 注入(仅 Linux 支持)") from exc

        fd = None
        try:
            fd = os.open(self.device_path, os.O_WRONLY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "%s 不存在。uinput 内核模块没有加载或者被裁掉了: "
                "先运行 sudo modprobe uinput, 或直接跑 tools/install_linux.sh "
                "帮你在 Debian 上装好依赖、加载模块并写 udev 规则。" % self.device_path
            ) from exc
        except PermissionError as exc:
            raise RuntimeError(
                "没有权限打开 %s。请把当前用户加入 input 组并重新登录"
                "(sudo usermod -aG input $USER), 或运行 tools/install_linux.sh "
                "写入 /etc/udev/rules.d/99-crosspc-uinput.rules 后重新插拔一次会话。"
                % self.device_path) from exc
        except OSError as exc:
            raise RuntimeError(
                "打开 %s 失败: %s。请确认内核支持 uinput(sudo modprobe uinput) "
                "以及当前用户有权限。" % (self.device_path, exc)) from exc

        try:
            self._setup_device(fd, fcntl)
        except Exception:
            # 半途失败必须把 fd 关掉, 否则会占着一个设备节点
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        self._fd = fd

    def _setup_device(self, fd: int, fcntl) -> None:
        """注册能力 + 设备信息 + UI_DEV_CREATE。"""
        self._bit(fcntl, fd, UI_SET_EVBIT, EV_KEY)
        self._bit(fcntl, fd, UI_SET_EVBIT, EV_ABS)
        self._bit(fcntl, fd, UI_SET_EVBIT, EV_REL)
        for code in keyboard_evdev_codes():
            self._bit(fcntl, fd, UI_SET_KEYBIT, code)
        for btn in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA):
            self._bit(fcntl, fd, UI_SET_KEYBIT, btn)
        self._bit(fcntl, fd, UI_SET_ABSBIT, ABS_X)
        self._bit(fcntl, fd, UI_SET_ABSBIT, ABS_Y)
        self._bit(fcntl, fd, UI_SET_RELBIT, REL_WHEEL)
        self._bit(fcntl, fd, UI_SET_RELBIT, REL_HWHEEL)
        # INPUT_PROP_POINTER: 告诉 X/合成器"这是个指点设备", 让它走指针那条
        # 代码路径(而不是被当成画板/手柄)。
        self._bit(fcntl, fd, UI_SET_PROPBIT, INPUT_PROP_POINTER)

        setup = _UinputSetup()
        ctypes.memset(ctypes.byref(setup), 0, ctypes.sizeof(setup))
        setup.id.bustype = 0x03                # BUS_USB
        setup.id.vendor = 0x1D6B               # 随便给个不冲突的 id
        setup.id.product = 0x0001
        setup.id.version = 1
        setup.name = DEVICE_NAME
        setup.ff_effects_max = 0
        if ctypes.sizeof(setup) != UINPUT_SETUP_SIZE:   # pragma: no cover - 自检
            raise RuntimeError("uinput_setup 结构体大小异常: %d" % ctypes.sizeof(setup))

        try:
            # 新接口: UI_DEV_SETUP 一次带上 input_id/name, 内核 >= 2.6.38(2011)。
            fcntl.ioctl(fd, UI_DEV_SETUP, ctypes.byref(setup))
        except OSError:
            # 回退: 老接口把 struct uinput_user_dev 整个 write() 进去。
            self._say("UI_DEV_SETUP 不可用, 回退到老的 uinput_user_dev 接口")
            self._write_legacy_user_dev(fd)

        fcntl.ioctl(fd, UI_DEV_CREATE, 0)
        self._created = True
        # 设备刚创建时 X 还没枚举到它, 给它一点时间, 否则前几个事件会丢。
        time.sleep(0.05)
        self._say("已创建虚拟指针设备 %r" % DEVICE_NAME.decode())

    def _write_legacy_user_dev(self, fd: int) -> None:
        dev = _UinputUserDev()
        ctypes.memset(ctypes.byref(dev), 0, ctypes.sizeof(dev))
        dev.name = DEVICE_NAME
        dev.id.bustype = 0x03
        dev.id.vendor = 0x1D6B
        dev.id.product = 0x0001
        dev.id.version = 1
        dev.absmax[ABS_X].minimum = 0
        dev.absmax[ABS_X].maximum = ABS_MAX
        dev.absmax[ABS_Y].minimum = 0
        dev.absmax[ABS_Y].maximum = ABS_MAX
        raw = ctypes.string_at(ctypes.byref(dev), ctypes.sizeof(dev))
        if len(raw) != UINPUT_USER_DEV_SIZE:   # pragma: no cover - 自检
            raise RuntimeError("uinput_user_dev 结构体大小异常: %d" % len(raw))
        os.write(fd, raw)

    @staticmethod
    def _bit(fcntl, fd: int, request: int, value: int) -> None:
        """UI_SET_* 的载荷是一个 int(要打开的那一位的编号)。

        有的内核不接受值 0(历史上 0 被当成"空指针 => 关掉全部"), 所以统一
        用 ctypes.c_int 传地址, 保证任何实现下都是"打开第 value 位"。

        这里把 OSError 全部吞掉、只记日志: 不同内核版本支持的 UI_SET_* 集合
        不一样(例如很老的内核没有 PROPBIT、没有 REL_HWHEEL 对应的能力位),
        单个位注册失败不该让整个设备起不来 —— 退化成"少一个不常用的按键",
        比 client 直接不能注入要好。后面 UI_DEV_CREATE 如果真失败会照常抛。
        """
        try:
            fcntl.ioctl(fd, request, ctypes.c_int(int(value)))
        except OSError as exc:                 # pragma: no cover - 依赖具体内核
            self._say("注册能力失败(request=0x%08X value=%d, 忽略并继续): %s"
                      % (request, value, exc))

    def close(self) -> None:
        """UI_DEV_DESTROY 并关掉 fd。可从任意线程调用, 幂等。"""
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                import fcntl
                if self._created:
                    fcntl.ioctl(fd, UI_DEV_DESTROY, 0)
            except Exception as exc:           # pragma: no cover - 关设备失败不致命
                self._say("销毁虚拟设备失败(忽略): %s" % exc)
            finally:
                self._created = False
                try:
                    os.close(fd)
                except OSError:
                    pass

    # ------------------------------------------------------------ 屏幕尺寸
    def set_screen_size(self, width: int, height: int) -> None:
        """设定绝对坐标映射的目标分辨率(像素)。"""
        if width > 0 and height > 0:
            self._screen_size = (int(width), int(height))
            self._say("屏幕尺寸设为 %dx%d" % self._screen_size)

    @property
    def screen_size(self) -> Optional[Tuple[int, int]]:
        return self._screen_size

    # ------------------------------------------------------------ 写事件
    def _emit(self, type_: int, code: int, value: int) -> None:
        """写一条事件。调用方自己负责成组后补 SYN_REPORT。"""
        fd = self._fd
        if fd is None:
            raise RuntimeError("uinput 设备尚未打开(先调用 open())")
        os.write(fd, pack_event(type_, code, value))

    def _sync(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    # ------------------------------------------------------------ 注入
    def inject_motion(self, x: int, y: int,
                      screen_w: Optional[int] = None,
                      screen_h: Optional[int] = None) -> None:
        """把像素坐标线性映射到 0..65535 后写 ABS_X/ABS_Y。

        绝对设备不需要屏幕尺寸也能"按比例"动, 但要让落点等于像素坐标就
        必须知道分辨率: 优先用本次传入的值, 其次用 set_screen_size() 设过的。
        """
        with self._lock:
            w = screen_w if screen_w else (self._screen_size[0] if self._screen_size else 0)
            h = screen_h if screen_h else (self._screen_size[1] if self._screen_size else 0)
            if not w or not h:
                raise RuntimeError(
                    "uinput 注入需要屏幕分辨率才能把坐标映射到绝对设备: "
                    "请设置环境变量 CROSSPC_SCREEN=宽x高(例如 2560x1440), "
                    "或让上层调用 set_screen_size()")
            ax, ay = pixel_to_abs(x, y, w, h)
            self._emit(EV_ABS, ABS_X, ax)
            self._emit(EV_ABS, ABS_Y, ay)
            self._sync()

    def inject_button(self, button: int, pressed: bool) -> None:
        code = button_to_evdev(button)
        if code is None:
            self._say("未知鼠标按键 %r, 已跳过" % (button,))
            return
        with self._lock:
            self._emit(EV_KEY, code, 1 if pressed else 0)
            self._sync()

    def inject_wheel(self, dx: int, dy: int) -> None:
        """dx 右为正 -> REL_HWHEEL; dy 上/远离用户为正 -> REL_WHEEL。"""
        with self._lock:
            if dy:
                self._emit(EV_REL, REL_WHEEL, int(dy))
            if dx:
                self._emit(EV_REL, REL_HWHEEL, int(dx))
            if dx or dy:
                self._sync()

    def inject_key(self, scancode: int, vk: int, pressed: bool,
                   extended: bool = False) -> None:
        code = keys.evdev_for(scancode, vk, extended)
        if code is None:
            self._say("扫描码 0x%02X(vk=0x%02X%s) 没有对应的 evdev 码, 已跳过"
                      % (scancode, vk, " ext" if extended else ""))
            return
        with self._lock:
            self._emit(EV_KEY, code, 1 if pressed else 0)
            self._sync()

    # ------------------------------------------------------------ 自检
    @staticmethod
    def available() -> Tuple[bool, str]:
        """(能否用 uinput, 中文说明)。绝不抛异常, 供 doctor 用。"""
        path = "/dev/uinput"
        try:
            if not os.path.exists("/sys/class/misc/uinput"):
                # 设备节点可能是"模块还没加载"或"内核没编 uinput"
                return (False, "uinput 内核模块未加载: 运行 sudo modprobe uinput"
                               "(或 tools/install_linux.sh), 必要时重启")
            if not os.path.exists(path):
                return (False, "%s 不存在: sudo modprobe uinput, 或检查 udev 规则"
                               "(tools/install_linux.sh)" % path)
            if not os.access(path, os.R_OK | os.W_OK):
                return (False, "当前用户没有读写 %s 的权限: 把用户加入 input 组"
                               "(sudo usermod -aG input $USER) 后重新登录, "
                               "或运行 tools/install_linux.sh 安装 udev 规则" % path)
        except Exception as exc:               # pragma: no cover - 兜底
            return (False, "检查 uinput 时出错: %s" % exc)
        return (True, "%s 可读写, 可以合成绝对定位的虚拟指针设备" % path)


def open_injector(log: Optional[Callable[[str], None]] = None
                  ) -> Optional[UInputInjector]:
    """方便函数: 可用就打开并返回, 否则返回 None(错误写进日志)。"""
    ok, why = UInputInjector.available()
    if not ok:
        if log:
            log("[uinput] 不可用: %s" % why)
        return None
    inj = UInputInjector(log=log)
    try:
        inj.open()
    except Exception as exc:
        if log:
            log("[uinput] 打开失败: %s" % exc)
        return None
    return inj
