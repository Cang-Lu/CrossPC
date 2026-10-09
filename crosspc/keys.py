"""Keycode mapping tables: PC scancode (set 1) <-> X11 keysym / Linux evdev code / Windows VK.

Why scancodes are the source of truth:
    what the server captures is a physical position (a scancode). Once the
    scancode is forwarded to the client, the client interprets it with its own
    keyboard layout -- which is exactly the correct "the key follows the
    position" behavior, and it also avoids mismatches caused by the two ends
    having different layouts. Only when no scancode is available do we fall back
    to VK/characters.

Extended keys (with the E0 prefix, such as the arrow keys / right Ctrl / keypad
Enter) are split by the Windows hook into "scancode + LLKHF_EXTENDED flag", so
this module keeps two tables: the normal one and the extended one.

Note: in the normal range a Linux evdev code is almost equal to the scancode
(KEY_A=30=0x1E), but extended keys differ (for example the Up arrow scancode
0x48 corresponds to KEY_UP=103), so the extended table has to be listed
separately.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

# ------------------------------------------------------------------ X11 keysym
XK_SPACE = 0x0020
XK_ESCAPE = 0xFF1B
XK_RETURN = 0xFF0D
XK_TAB = 0xFF09
XK_BACKSPACE = 0xFF08
XK_DELETE = 0xFFFF
XK_INSERT = 0xFF63
XK_HOME = 0xFF50
XK_END = 0xFF57
XK_PRIOR = 0xFF55          # Page Up
XK_NEXT = 0xFF56           # Page Down
XK_LEFT = 0xFF51
XK_UP = 0xFF52
XK_RIGHT = 0xFF53
XK_DOWN = 0xFF54
XK_SHIFT_L = 0xFFE1
XK_SHIFT_R = 0xFFE2
XK_CONTROL_L = 0xFFE3
XK_CONTROL_R = 0xFFE4
XK_ALT_L = 0xFFE9
XK_ALT_R = 0xFFEA
XK_SUPER_L = 0xFFEB
XK_SUPER_R = 0xFFEC
XK_MENU = 0xFF67
XK_CAPS_LOCK = 0xFFE5
XK_NUM_LOCK = 0xFF7F
XK_SCROLL_LOCK = 0xFF14
XK_PRINT = 0xFF61
XK_PAUSE = 0xFF13
XK_KP_ENTER = 0xFF8D
XK_KP_DIVIDE = 0xFFAF
XK_KP_MULTIPLY = 0xFFAA
XK_KP_SUBTRACT = 0xFFAD
XK_KP_ADD = 0xFFAB
XK_KP_DECIMAL = 0xFFAE
# In the X11 core mapping the numeric keypad is by default bound to the
# KP_Home/KP_Up/... combination names; only by using those can
# XKeysymToKeycode find the default keycode.
XK_KP_HOME = 0xFF95
XK_KP_UP = 0xFF97
XK_KP_PRIOR = 0xFF9A
XK_KP_LEFT = 0xFF96
XK_KP_BEGIN = 0xFF9D
XK_KP_RIGHT = 0xFF98
XK_KP_END = 0xFF9C
XK_KP_DOWN = 0xFF99
XK_KP_NEXT = 0xFF9B
XK_KP_INSERT = 0xFF9E
XK_KP_DELETE = 0xFF9F

_F1 = 0xFFBE          # F1 .. F35 are contiguous

# ------------------------------------------------------------------ Windows VK
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C                 # PrintScreen
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5


def _chars(seq: str, start: int) -> Dict[int, int]:
    """Map a run of ASCII characters in order onto keysyms starting at start (= the ASCII code)."""
    return {start + i: ord(c) for i, c in enumerate(seq)}


# normal (no E0 prefix) scancode -> X11 keysym
SCAN_TO_KEYSYM: Dict[int, int] = {
    0x01: XK_ESCAPE,
    0x0C: ord("-"), 0x0D: ord("="), 0x0E: XK_BACKSPACE, 0x0F: XK_TAB,
    0x1A: ord("["), 0x1B: ord("]"), 0x1C: XK_RETURN, 0x1D: XK_CONTROL_L,
    0x27: ord(";"), 0x28: ord("'"), 0x29: ord("`"),
    0x2A: XK_SHIFT_L, 0x2B: ord("\\"),
    0x33: ord(","), 0x34: ord("."), 0x35: ord("/"),
    0x36: XK_SHIFT_R, 0x37: XK_KP_MULTIPLY, 0x38: XK_ALT_L, 0x39: XK_SPACE,
    0x3A: XK_CAPS_LOCK,
    0x45: XK_NUM_LOCK, 0x46: XK_SCROLL_LOCK,
    0x47: XK_KP_HOME, 0x48: XK_KP_UP, 0x49: XK_KP_PRIOR, 0x4A: XK_KP_SUBTRACT,
    0x4B: XK_KP_LEFT, 0x4C: XK_KP_BEGIN, 0x4D: XK_KP_RIGHT, 0x4E: XK_KP_ADD,
    0x4F: XK_KP_END, 0x50: XK_KP_DOWN, 0x51: XK_KP_NEXT, 0x52: XK_KP_INSERT,
    0x53: XK_KP_DELETE,
    0x56: ord("<"),                      # the ISO 102 key (the one at the bottom left)
    0x59: 0xFFBD,                        # KP_Equal
}
SCAN_TO_KEYSYM.update(_chars("1234567890", 0x02))
SCAN_TO_KEYSYM.update(_chars("qwertyuiop", 0x10))
SCAN_TO_KEYSYM.update(_chars("asdfghjkl", 0x1E))
SCAN_TO_KEYSYM.update(_chars("zxcvbnm", 0x2C))
for _i in range(10):                     # F1..F10
    SCAN_TO_KEYSYM[0x3B + _i] = _F1 + _i
SCAN_TO_KEYSYM[0x57] = _F1 + 10          # F11
SCAN_TO_KEYSYM[0x58] = _F1 + 11          # F12

# extended (E0 prefix) scancode -> X11 keysym
EXT_SCAN_TO_KEYSYM: Dict[int, int] = {
    0x1C: XK_KP_ENTER,
    0x1D: XK_CONTROL_R,
    0x35: XK_KP_DIVIDE,
    0x37: XK_PRINT,
    0x38: XK_ALT_R,
    0x47: XK_HOME, 0x48: XK_UP, 0x49: XK_PRIOR,
    0x4B: XK_LEFT, 0x4D: XK_RIGHT,
    0x4F: XK_END, 0x50: XK_DOWN, 0x51: XK_NEXT,
    0x52: XK_INSERT, 0x53: XK_DELETE,
    0x5B: XK_SUPER_L, 0x5C: XK_SUPER_R, 0x5D: XK_MENU,
}

# extended scancode -> Linux evdev code
EXT_SCAN_TO_EVDEV: Dict[int, int] = {
    0x1C: 96,     # KEY_KPENTER
    0x1D: 97,     # KEY_RIGHTCTRL
    0x35: 98,     # KEY_KPSLASH
    0x37: 99,     # KEY_SYSRQ
    0x38: 100,    # KEY_RIGHTALT
    0x47: 102,    # KEY_HOME
    0x48: 103,    # KEY_UP
    0x49: 104,    # KEY_PAGEUP
    0x4B: 105,    # KEY_LEFT
    0x4D: 106,    # KEY_RIGHT
    0x4F: 107,    # KEY_END
    0x50: 108,    # KEY_DOWN
    0x51: 109,    # KEY_PAGEDOWN
    0x52: 110,    # KEY_INSERT
    0x53: 111,    # KEY_DELETE
    0x5B: 125,    # KEY_LEFTMETA
    0x5C: 126,    # KEY_RIGHTMETA
    0x5D: 127,    # KEY_COMPOSE
}

# Windows: KEYEVENTF_*
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# Linux evdev only (the few keys that do not match in the scancode table)
EVDEV_PAUSE = 119          # KEY_PAUSE
EVDEV_SYSRQ = 99           # KEY_SYSRQ

# Modifier keys that are "claimed exclusively" and force-released on a switch,
# so no stuck key is left behind on the other end.
# key = (scancode, extended)
MODIFIER_KEYS = {
    (0x2A, False): "LShift",
    (0x36, False): "RShift",
    (0x1D, False): "LCtrl",
    (0x1D, True): "RCtrl",
    (0x38, False): "LAlt",
    (0x38, True): "RAlt",
    (0x5B, True): "LWin",
    (0x5C, True): "RWin",
}

# Lock keys: they do not take part in "release all keys", otherwise the
# CapsLock state would get scrambled.
TOGGLE_KEYS = {
    (0x3A, False): "CapsLock",
    (0x45, False): "NumLock",
    (0x46, False): "ScrollLock",
}


def keysym_for(scancode: int, extended: bool = False) -> Optional[int]:
    """Scancode -> X11 keysym (None when not found; the caller should skip that key and log it)."""
    if extended:
        ks = EXT_SCAN_TO_KEYSYM.get(scancode)
        if ks is not None:
            return ks
    return SCAN_TO_KEYSYM.get(scancode)


def evdev_for(scancode: int, vk: int = 0, extended: bool = False) -> Optional[int]:
    """Scancode -> Linux evdev code."""
    if vk == VK_PAUSE:
        return EVDEV_PAUSE
    if extended:
        code = EXT_SCAN_TO_EVDEV.get(scancode)
        if code is not None:
            return code
        if scancode == 0x45:                 # E0 45 = another way NumLock is sent
            return 69
        return None
    # PrintScreen is also 0x37, but its evdev code is not 0x37
    # (KEY_KPASTERISK=55), so the VK fallback must come before the direct
    # mapping of the normal range.
    if vk == VK_SNAPSHOT:
        return EVDEV_SYSRQ
    if 0x01 <= scancode <= 0x58 or scancode in (0x59, 0x64, 0x65, 0x66, 0x67, 0x68):
        # in the normal range the evdev code is the scancode
        return scancode
    return None


def is_modifier(scancode: int, extended: bool = False) -> bool:
    return (scancode, bool(extended)) in MODIFIER_KEYS


def is_toggle(scancode: int, extended: bool = False) -> bool:
    return (scancode, bool(extended)) in TOGGLE_KEYS


def key_name(scancode: int, vk: int = 0, extended: bool = False) -> str:
    """Readable key name for logging."""
    for table in (MODIFIER_KEYS, TOGGLE_KEYS):
        n = table.get((scancode, bool(extended)))
        if n:
            return n
    ks = keysym_for(scancode, extended)
    if ks is not None:
        if 0x20 <= ks < 0x7F:
            return chr(ks).upper()
        for name, val in list(globals().items()):
            if name.startswith("XK_") and val == ks:
                return name[3:]
    if vk:
        return "VK_%02X" % vk
    return "scan0x%02X%s" % (scancode, "e" if extended else "")


def pair_of(scancode: int, vk: int, extended: bool) -> Tuple[int, int, bool]:
    return scancode, vk, extended


# ------------------------------------------------------------------ hotkey names
# The config file writes strings such as "ctrl+alt+f12", parsed uniformly into
# (scancode, whether E0 extended). Scancodes are used rather than characters so
# that they are the same thing the hook produces, unaffected by the input method
# or the layout.
NAME_TO_KEY: Dict[str, Tuple[int, bool]] = {
    # modifier keys
    "ctrl": (0x1D, False), "control": (0x1D, False), "lctrl": (0x1D, False),
    "rctrl": (0x1D, True),
    "shift": (0x2A, False), "lshift": (0x2A, False), "rshift": (0x36, False),
    "alt": (0x38, False), "lalt": (0x38, False), "ralt": (0x38, True),
    "altgr": (0x38, True),
    "super": (0x5B, True), "win": (0x5B, True), "meta": (0x5B, True),
    "cmd": (0x5B, True), "lwin": (0x5B, True), "rwin": (0x5C, True),
    # common function keys
    "escape": (0x01, False), "esc": (0x01, False),
    "tab": (0x0F, False), "space": (0x39, False),
    "enter": (0x1C, False), "return": (0x1C, False),
    "backspace": (0x0E, False), "bkps": (0x0E, False),
    "capslock": (0x3A, False), "caps": (0x3A, False),
    "numlock": (0x45, False), "scrolllock": (0x46, False),
    # Pause's E1 sequence is incomplete in the hook and may not be caught, so
    # this is only a best-effort mapping
    "pause": (0x45, False),
    "printscreen": (0x37, True), "print": (0x37, True),
    "insert": (0x52, True), "ins": (0x52, True),
    "delete": (0x53, True), "del": (0x53, True),
    "home": (0x47, True), "end": (0x4F, True),
    "pgup": (0x49, True), "pageup": (0x49, True),
    "pgdn": (0x51, True), "pagedown": (0x51, True),
    "up": (0x48, True), "down": (0x50, True),
    "left": (0x4B, True), "right": (0x4D, True),
    # symbols (US layout positions)
    "grave": (0x29, False), "backtick": (0x29, False),
    "minus": (0x0C, False), "equal": (0x0D, False),
    "comma": (0x33, False), "period": (0x34, False), "slash": (0x35, False),
    "backslash": (0x2B, False), "semicolon": (0x27, False),
    "apostrophe": (0x28, False),
}
for _i in range(1, 11):
    # F1=0x3B ... F10=0x44
    NAME_TO_KEY["f%d" % _i] = (0x3A + _i, False)
# F11/F12 are not in the contiguous F1..F10 range (0x57/0x58), so they have to
# be written separately, otherwise they would be mapped by mistake to
# 0x45/0x46 (NumLock/ScrollLock).
NAME_TO_KEY["f11"] = (0x57, False)
NAME_TO_KEY["f12"] = (0x58, False)


def parse_key_name(name: str) -> Optional[Tuple[int, bool]]:
    """Parse "f12" / "q" / "ctrl" into (scancode, extended). Returns None when it cannot be parsed."""
    n = (name or "").strip().lower()
    if not n:
        return None
    hit = NAME_TO_KEY.get(n)
    if hit is not None:
        return hit
    if len(n) == 1:
        want = ord(n)
        for scan, sym in SCAN_TO_KEYSYM.items():
            if sym == want:
                return scan, False
    return None

