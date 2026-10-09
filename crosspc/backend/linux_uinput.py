"""Linux /dev/uinput injector: synthesize an **absolute-positioning** virtual pointer device.

Why an absolute device (instead of a relative mouse):
    Once a relative mouse (REL_X/REL_Y) is written, the display
    server/compositor still applies "pointer acceleration" and "enhance pointer
    precision"-style processing to it, so the delta CrossPC computes comes out
    wrong on screen, and it drifts and accumulates -- as long as pointer
    acceleration is on, a KVM built on a relative device can never be accurate.
    An absolute device (tablet/touchscreen-like) has "jump straight to this
    coordinate" semantics; the compositor does not accelerate it, it only applies
    one linear mapping, so the absolute pixel coordinate handed to
    inject_motion(x, y) is exactly where the pointer lands. The price is that the
    device has to know the screen resolution, which is why set_screen_size()
    exists.

This module loads nothing and opens no device at the top level: ctypes, structures
and constants are all plain Python definitions, and /dev/uinput is opened only
inside open(). That way `python -m compileall` and importing this module on
Windows both work (see tests/test_linux_backend.py).

Where the ioctl constants come from and how they are computed: see the comments on
_IOW / _IO below (linux/uinput.h + asm-generic/ioctl.h).
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
# ioctl numbers computed by hand
#
# Source: linux/asm-generic/ioctl.h
#     _IOC(dir,type,nr,size) = (dir << 30) | (size << 16) | (type << 8) | nr
#     _IO (type,nr)  = _IOC(0, type, nr, 0)      no data
#     _IOW(type,nr,type of the size argument) = _IOC(1, type, nr, sizeof(that type))
#     _IOR is the same as _IOW but with dir=2
# The type for uinput is always 'U' = 0x55; size is "the size of the data the
# kernel expects in the ioctl".
#
# Verification (can be compared against /usr/include/linux/uinput.h):
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
# There is a trap here that is very easy to fall into: the UINPUT_IOCTL_BASE + 4
# algorithm (treating nr as 4/5/6/7) is **wrong** and produces numbers such as
# 0x40045504 that the kernel does not recognize at all, making the ioctl return
# ENOTTY straight away. The correct nr starts at 100 (see uinput.h:
# UINPUT_IOCTL_BASE + 100 = UI_SET_EVBIT).
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
# Event types / event codes (linux/input-event-codes.h)
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
SYN_REPORT = 0x00

REL_HWHEEL = 0x06
REL_WHEEL = 0x08

ABS_X = 0x00
ABS_Y = 0x01
#: Range of the absolute coordinates. 0..65535 is the de facto common scale of the
#: kernel's BTN_TOOL/ABS-class devices; a resolution such as 2560x1440 maps into it
#: with a quantization error far below one pixel.
ABS_MAX = 65535

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114

#: X11 button numbers (the convention in events.py) -> evdev BTN_*
#: 8/9 are X's back/forward buttons, matching BTN_SIDE/BTN_EXTRA.
BUTTON_TO_EVDEV: Dict[int, int] = {
    1: BTN_LEFT,
    2: BTN_MIDDLE,
    3: BTN_RIGHT,
    8: BTN_SIDE,
    9: BTN_EXTRA,
}

INPUT_PROP_POINTER = 0

#: Size of struct input_event: struct timeval(16) + type(2) + code(2) + value(4)
INPUT_EVENT_SIZE = 24
#: Size of struct uinput_setup: input_id(8) + name[80] + ff_effects_max(4)
UINPUT_SETUP_SIZE = 92
#: Size of struct uinput_user_dev: name[80] + input_id(8) + ff_effects_max(4) + absmax[64]*5*4
UINPUT_USER_DEV_SIZE = 80 + 8 + 4 + 64 * 5 * 4    # 1372

#: Device name (must be bytes; the kernel reads it as a C string)
DEVICE_NAME = b"CrossPC Virtual Pointer"

#: A few keys that are in the scancode table but whose evdev codes do not line up with the normal range (see the comments in keys.py)
_EXTRA_EVDEV_CODES = (
    keys.EVDEV_PAUSE,        # 119 KEY_PAUSE
    keys.EVDEV_SYSRQ,        # 99  KEY_SYSRQ
    58,                      # KEY_CAPSLOCK (the normal-range evdev code is not 0x3A)
    69,                      # KEY_NUMLOCK
    70,                      # KEY_SCROLLLOCK
)


# ---------------------------------------------------------------------------
# Structure layout
class _TimeVal(ctypes.Structure):
    """struct timeval: two 8-byte values (long) on 64-bit Linux."""

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
    """struct input_id (linux/input.h): bus/vendor/product/version, 2 bytes each."""

    _fields_ = [("bustype", ctypes.c_uint16),
                ("vendor", ctypes.c_uint16),
                ("product", ctypes.c_uint16),
                ("version", ctypes.c_uint16)]


class _UinputSetup(ctypes.Structure):
    """struct uinput_setup (linux/uinput.h) -- the payload of the newer UI_DEV_SETUP interface.

        struct input_id id;  char name[UINPUT_MAX_NAME_SIZE];  __u32 ff_effects_max;
    """

    _fields_ = [("id", _InputId),
                ("name", ctypes.c_char * 80),
                ("ff_effects_max", ctypes.c_uint32)]


class _UinputAbsinfo(ctypes.Structure):
    """struct input_absinfo (linux/input.h): 5 __s32 values per absolute axis."""

    _fields_ = [("value", ctypes.c_int32), ("minimum", ctypes.c_int32),
                ("maximum", ctypes.c_int32), ("fuzz", ctypes.c_int32),
                ("flat", ctypes.c_int32),
                # resolution was only added by newer kernels, but for
                # uinput_user_dev (the old interface) the kernel reads just the first
                # 5 fields (20 bytes per axis), so it cannot be added here, otherwise
                # the element stride of the absmax array would be wrong.
                ]


class _UinputUserDev(ctypes.Structure):
    """struct uinput_user_dev (linux/uinput.h) -- the old interface, written straight to the fd.

        char name[80];  struct input_id id;  __u32 ff_effects_max;
        __s32 absmax[ABS_CNT][5];     // ABS_CNT = 64
    """

    _fields_ = [("name", ctypes.c_char * 80),
                ("id", _InputId),
                ("ff_effects_max", ctypes.c_uint32),
                ("absmax", _UinputAbsinfo * 64)]


# ---------------------------------------------------------------------------
# Pure functions (they touch no device and can be unit-tested on any platform)
def pixel_to_abs(x: int, y: int, screen_w: int, screen_h: int) -> Tuple[int, int]:
    """Pixel coordinates -> absolute device scale 0..65535.

    A linear mapping with the boundaries pinned down: 0 -> 0, w-1 -> 65535. It
    rounds instead of truncating, otherwise w-1 would only reach 65534 and the
    very last column/row at the bottom-right of the screen could never be reached.
    When the screen size is invalid (<=0) it degrades to a straight pass-through
    of 0..65535, so at least it never raises.
    """
    if screen_w <= 0 or screen_h <= 0:
        return (min(max(int(x), 0), ABS_MAX), min(max(int(y), 0), ABS_MAX))
    ax = int(round(float(x) * ABS_MAX / float(screen_w - 1))) if screen_w > 1 else 0
    ay = int(round(float(y) * ABS_MAX / float(screen_h - 1))) if screen_h > 1 else 0
    return (min(max(ax, 0), ABS_MAX), min(max(ay, 0), ABS_MAX))


def pack_event(type_: int, code: int, value: int,
               sec: Optional[int] = None, usec: Optional[int] = None) -> bytes:
    """Encode one input_event into 24 bytes (a pure function, easy to unit-test).

    The timestamp is packed with struct rather than a ctypes structure: that way
    this function still computes the correct 24 bytes on Windows (on Windows c_long
    is only 4 bytes, so the ctypes structure would be 16 bytes).
    """
    if sec is None:
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1000000)
    usec = int(usec or 0)
    # Keep floating-point error from pushing usec up to 1000000 (the kernel treats that as an invalid timestamp)
    if usec >= 1000000:
        sec += usec // 1000000
        usec %= 1000000
    elif usec < 0:
        usec = 0
    return struct.pack("=qqHHi", int(sec), usec,
                       int(type_) & 0xFFFF, int(code) & 0xFFFF,
                       int(value))


def button_to_evdev(button: int) -> Optional[int]:
    """X11 button number -> evdev BTN_*; returns None when unknown (the caller logs and skips)."""
    return BUTTON_TO_EVDEV.get(int(button))


def keyboard_evdev_codes() -> Set[int]:
    """Translate every scancode that appears in keys.py into an evdev code, for KEYBIT registration.

    It walks the two tables in keys.py directly (rather than copying a key table
    of its own): when keys.py gains a key later, registration here follows along
    automatically, so there are no dead keys that exist "in the table but not
    registered on the device".
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
    """Synthesize a virtual pointer + keyboard through /dev/uinput for absolute-positioning injection.

    Threading contract: the instance protects write() with a lock internally, so
    the injection methods can be called from any thread.
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

    # ------------------------------------------------------------ lifecycle
    @property
    def opened(self) -> bool:
        return self._fd is not None

    def open(self) -> None:
        """Open /dev/uinput and register the device capabilities + UI_DEV_CREATE. Idempotent."""
        if self._fd is not None:
            return
        try:
            import fcntl                       # only present on Linux, imported lazily
        except ImportError as exc:             # pragma: no cover - Windows
            raise RuntimeError(
                "this platform has no fcntl module, so /dev/uinput injection is "
                "unavailable (Linux only)") from exc

        fd = None
        try:
            fd = os.open(self.device_path, os.O_WRONLY | os.O_NONBLOCK)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "%s does not exist. The uinput kernel module is not loaded or was "
                "built out: run sudo modprobe uinput first, or just run "
                "tools/install_linux.sh, which installs the dependencies on Debian, "
                "loads the module and writes the udev rules." % self.device_path
            ) from exc
        except PermissionError as exc:
            raise RuntimeError(
                "no permission to open %s. Add the current user to the input group "
                "and log in again (sudo usermod -aG input $USER), or run "
                "tools/install_linux.sh to write "
                "/etc/udev/rules.d/99-crosspc-uinput.rules and then log the session "
                "out and back in." % self.device_path) from exc
        except OSError as exc:
            raise RuntimeError(
                "failed to open %s: %s. Make sure the kernel supports uinput "
                "(sudo modprobe uinput) and that the current user has permission."
                % (self.device_path, exc)) from exc

        try:
            self._setup_device(fd, fcntl)
        except Exception:
            # A failure part-way through must close the fd, otherwise a device node stays occupied
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        self._fd = fd

    def _setup_device(self, fd: int, fcntl) -> None:
        """Register capabilities + device information + UI_DEV_CREATE."""
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
        # INPUT_PROP_POINTER: tell X/the compositor that "this is a pointing
        # device" so that it takes the pointer code path (rather than treating it
        # as a drawing tablet/gamepad).
        self._bit(fcntl, fd, UI_SET_PROPBIT, INPUT_PROP_POINTER)

        setup = _UinputSetup()
        ctypes.memset(ctypes.byref(setup), 0, ctypes.sizeof(setup))
        setup.id.bustype = 0x03                # BUS_USB
        setup.id.vendor = 0x1D6B               # an arbitrary id that does not conflict
        setup.id.product = 0x0001
        setup.id.version = 1
        setup.name = DEVICE_NAME
        setup.ff_effects_max = 0
        if ctypes.sizeof(setup) != UINPUT_SETUP_SIZE:   # pragma: no cover - self-check
            raise RuntimeError("unexpected uinput_setup structure size: %d" % ctypes.sizeof(setup))

        try:
            # Newer interface: UI_DEV_SETUP carries input_id/name in one call, kernel >= 2.6.38 (2011).
            fcntl.ioctl(fd, UI_DEV_SETUP, ctypes.byref(setup))
        except OSError:
            # Fallback: the old interface write()s the whole struct uinput_user_dev.
            self._say("UI_DEV_SETUP is unavailable, falling back to the old uinput_user_dev interface")
            self._write_legacy_user_dev(fd)

        fcntl.ioctl(fd, UI_DEV_CREATE, 0)
        self._created = True
        # X has not enumerated the device right after creation, so give it a moment, otherwise the first few events are lost.
        time.sleep(0.05)
        self._say("created virtual pointer device %r" % DEVICE_NAME.decode())

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
        if len(raw) != UINPUT_USER_DEV_SIZE:   # pragma: no cover - self-check
            raise RuntimeError("unexpected uinput_user_dev structure size: %d" % len(raw))
        os.write(fd, raw)

    @staticmethod
    def _bit(fcntl, fd: int, request: int, value: int) -> None:
        """The payload of UI_SET_* is an int (the number of the bit to turn on).

        Some kernels reject the value 0 (historically 0 was treated as "null
        pointer => turn everything off"), so a ctypes.c_int address is always
        passed, guaranteeing "turn on bit number value" under every implementation.

        Every OSError is swallowed here and merely logged: different kernel
        versions support different sets of UI_SET_* (very old kernels have no
        PROPBIT and no capability bit for REL_HWHEEL, for example), and one failed
        bit registration should not keep the whole device from coming up --
        degrading to "one uncommon key is missing" is better than the client being
        unable to inject at all. If UI_DEV_CREATE later fails for real, that is
        still raised as usual.
        """
        try:
            fcntl.ioctl(fd, request, ctypes.c_int(int(value)))
        except OSError as exc:                 # pragma: no cover - depends on the specific kernel
            self._say("failed to register capability (request=0x%08X value=%d, ignoring and continuing): %s"
                      % (request, value, exc))

    def close(self) -> None:
        """UI_DEV_DESTROY and close the fd. Callable from any thread, idempotent."""
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                import fcntl
                if self._created:
                    fcntl.ioctl(fd, UI_DEV_DESTROY, 0)
            except Exception as exc:           # pragma: no cover - failing to close a device is not fatal
                self._say("failed to destroy the virtual device (ignored): %s" % exc)
            finally:
                self._created = False
                try:
                    os.close(fd)
                except OSError:
                    pass

    # ------------------------------------------------------------ screen size
    def set_screen_size(self, width: int, height: int) -> None:
        """Set the target resolution (in pixels) used to map the absolute coordinates."""
        if width > 0 and height > 0:
            self._screen_size = (int(width), int(height))
            self._say("screen size set to %dx%d" % self._screen_size)

    @property
    def screen_size(self) -> Optional[Tuple[int, int]]:
        return self._screen_size

    # ------------------------------------------------------------ writing events
    def _emit(self, type_: int, code: int, value: int) -> None:
        """Write one event. The caller is responsible for appending SYN_REPORT after a group."""
        fd = self._fd
        if fd is None:
            raise RuntimeError("the uinput device is not open yet (call open() first)")
        os.write(fd, pack_event(type_, code, value))

    def _sync(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    # ------------------------------------------------------------ injection
    def inject_motion(self, x: int, y: int,
                      screen_w: Optional[int] = None,
                      screen_h: Optional[int] = None) -> None:
        """Map pixel coordinates linearly into 0..65535, then write ABS_X/ABS_Y.

        An absolute device can move "proportionally" without knowing the screen
        size, but making the landing point equal the pixel coordinate does require
        the resolution: the value passed to this call takes priority, then whatever
        set_screen_size() configured.
        """
        with self._lock:
            w = screen_w if screen_w else (self._screen_size[0] if self._screen_size else 0)
            h = screen_h if screen_h else (self._screen_size[1] if self._screen_size else 0)
            if not w or not h:
                raise RuntimeError(
                    "uinput injection needs the screen resolution to map coordinates "
                    "onto an absolute device: set the CROSSPC_SCREEN=WxH environment "
                    "variable (for example 2560x1440), or have the upper layer call "
                    "set_screen_size()")
            ax, ay = pixel_to_abs(x, y, w, h)
            self._emit(EV_ABS, ABS_X, ax)
            self._emit(EV_ABS, ABS_Y, ay)
            self._sync()

    def inject_button(self, button: int, pressed: bool) -> None:
        code = button_to_evdev(button)
        if code is None:
            self._say("unknown mouse button %r, skipped" % (button,))
            return
        with self._lock:
            self._emit(EV_KEY, code, 1 if pressed else 0)
            self._sync()

    def inject_wheel(self, dx: int, dy: int) -> None:
        """dx positive to the right -> REL_HWHEEL; dy positive up/away from the user -> REL_WHEEL."""
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
            self._say("scancode 0x%02X (vk=0x%02X%s) has no matching evdev code, skipped"
                      % (scancode, vk, " ext" if extended else ""))
            return
        with self._lock:
            self._emit(EV_KEY, code, 1 if pressed else 0)
            self._sync()

    # ------------------------------------------------------------ self-check
    @staticmethod
    def available() -> Tuple[bool, str]:
        """(whether uinput can be used, explanation). Never raises; used by doctor."""
        path = "/dev/uinput"
        try:
            if not os.path.exists("/sys/class/misc/uinput"):
                # The device node may be missing because the module is not loaded yet or the kernel was built without uinput
                return (False, "the uinput kernel module is not loaded: run "
                               "sudo modprobe uinput (or tools/install_linux.sh), "
                               "reboot if necessary")
            if not os.path.exists(path):
                return (False, "%s does not exist: sudo modprobe uinput, or check the "
                               "udev rules (tools/install_linux.sh)" % path)
            if not os.access(path, os.R_OK | os.W_OK):
                return (False, "the current user lacks read/write permission on %s: "
                               "add the user to the input group "
                               "(sudo usermod -aG input $USER) and log in again, or run "
                               "tools/install_linux.sh to install the udev rules" % path)
        except Exception as exc:               # pragma: no cover - fallback
            return (False, "error while checking uinput: %s" % exc)
        return (True, "%s is readable/writable, an absolute-positioning virtual pointer device can be synthesized" % path)


def open_injector(log: Optional[Callable[[str], None]] = None
                  ) -> Optional[UInputInjector]:
    """Convenience function: open and return it when available, otherwise return None (the error goes to the log)."""
    ok, why = UInputInjector.available()
    if not ok:
        if log:
            log("[uinput] unavailable: %s" % why)
        return None
    inj = UInputInjector(log=log)
    try:
        inj.open()
    except Exception as exc:
        if log:
            log("[uinput] failed to open: %s" % exc)
        return None
    return inj
