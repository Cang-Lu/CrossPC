"""Cross-platform normalized input events.

Every backend translates its own raw input into the Event defined here, the
layer above (Router) handles nothing but Event, and the wire protocol
transports nothing but Event. The meaning of the fields varies with kind, and
they are packed into the four slots a/b/c/d to keep the object lightweight
(mouse motion can reach 1000Hz, so a dict or a dataclass is not an option).

    MOTION  a=screen coordinate x, b=screen coordinate y, c=physical delta dx
            for this event, d=physical delta dy for this event
    BUTTON  a=button number (see BTN_*), b=1 pressed / 0 released
    WHEEL   a=horizontal wheel notches (right is positive), b=vertical wheel
            notches (away from the user / scrolling up is positive)
    KEY     a=PC scancode (set 1, without the E0 prefix), b=Windows virtual-key
            code (VK_*, may be 0), c=1 pressed / 0 released, d=1 means the key
            carries the E0 extended prefix

Coordinate convention: always screen coordinates in "physical pixels", and they
may be negative when the top-left corner of the local virtual desktop is the
origin (which happens when a secondary monitor sits to the left of the primary
one). The details are described by Rect, so never assume they start at 0.
"""
from __future__ import annotations

from typing import NamedTuple

# ---------------------------------------------------------------- event kinds
MOTION = 1
BUTTON = 2
WHEEL = 3
KEY = 4

# ---------------------------------------------------------------- mouse buttons
# We use the X11 button numbering (1/2/3 left/middle/right, 8/9 side buttons)
# because it maps both to Windows SendInput and to Linux XTestFakeButtonEvent,
# so there is no need to define an intermediate representation.
BTN_LEFT = 1
BTN_MIDDLE = 2
BTN_RIGHT = 3
BTN_BACK = 8
BTN_FORWARD = 9

BUTTON_NAMES = {
    BTN_LEFT: "left",
    BTN_MIDDLE: "middle",
    BTN_RIGHT: "right",
    BTN_BACK: "back",
    BTN_FORWARD: "forward",
}

KIND_NAMES = {MOTION: "MOTION", BUTTON: "BUTTON", WHEEL: "WHEEL", KEY: "KEY"}


class Event(NamedTuple):
    kind: int
    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0

    # ------------------------------------------------------------ constructors
    @staticmethod
    def motion(x: int, y: int, dx: int = 0, dy: int = 0) -> "Event":
        return Event(MOTION, x, y, dx, dy)

    @staticmethod
    def button(button: int, pressed: bool) -> "Event":
        return Event(BUTTON, int(button), 1 if pressed else 0)

    @staticmethod
    def wheel(dx: int, dy: int) -> "Event":
        return Event(WHEEL, int(dx), int(dy))

    @staticmethod
    def key(scancode: int, vk: int, pressed: bool, extended: bool = False) -> "Event":
        return Event(KEY, int(scancode) & 0xFFFF, int(vk) & 0xFFFF,
                     1 if pressed else 0, 1 if extended else 0)

    # ------------------------------------------------------------ convenience properties
    @property
    def pressed(self) -> bool:
        return bool(self.c)

    @property
    def extended(self) -> bool:
        return bool(self.d)

    def describe(self) -> str:  # for logging only
        if self.kind == MOTION:
            return "move %d,%d (d %+d,%+d)" % (self.a, self.b, self.c, self.d)
        if self.kind == BUTTON:
            return "button %s %s" % (BUTTON_NAMES.get(self.a, self.a),
                                     "down" if self.b else "up")
        if self.kind == WHEEL:
            return "wheel %+d,%+d" % (self.a, self.b)
        if self.kind == KEY:
            return "key scan=0x%02X vk=0x%02X %s%s" % (
                self.a, self.b, "down" if self.c else "up",
                " ext" if self.d else "")
        return "event#%d" % self.kind
