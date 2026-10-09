"""跨平台归一化输入事件。

所有后端都把自己的原始输入翻译成这里的 Event, 上层(Router)只处理 Event,
线协议也只传输 Event。字段含义随 kind 变化, 用 a/b/c/d 四个槽位以保持
对象轻量(鼠标移动可达 1000Hz, 不能用字典或 dataclass)。

    MOTION  a=屏幕坐标 x, b=屏幕坐标 y, c=本次物理位移 dx, d=本次物理位移 dy
    BUTTON  a=按键号(见 BTN_*), b=1 按下 / 0 抬起
    WHEEL   a=水平滚轮格数(右为正), b=垂直滚轮格数(远离用户/上滚为正)
    KEY     a=PC 扫描码(set 1, 不含 E0 前缀), b=Windows 虚拟键码(VK_*, 可 0),
            c=1 按下 / 0 抬起, d=1 表示该键带 E0 扩展前缀

坐标约定: 始终是"物理像素"的屏幕坐标, 且以本机虚拟桌面左上角为原点时
可能为负(副屏在主屏左侧时), 具体由 Rect 描述, 不要假设从 0 开始。
"""
from __future__ import annotations

from typing import NamedTuple

# ---------------------------------------------------------------- 事件类型
MOTION = 1
BUTTON = 2
WHEEL = 3
KEY = 4

# ---------------------------------------------------------------- 鼠标按键
# 采用 X11 的按键编号(1/2/3 左中右, 8/9 侧键), 因为它能同时映射到
# Windows 的 SendInput 和 Linux 的 XTestFakeButtonEvent, 无需再定义中间态。
BTN_LEFT = 1
BTN_MIDDLE = 2
BTN_RIGHT = 3
BTN_BACK = 8
BTN_FORWARD = 9

BUTTON_NAMES = {
    BTN_LEFT: "左键",
    BTN_MIDDLE: "中键",
    BTN_RIGHT: "右键",
    BTN_BACK: "后退键",
    BTN_FORWARD: "前进键",
}

KIND_NAMES = {MOTION: "MOTION", BUTTON: "BUTTON", WHEEL: "WHEEL", KEY: "KEY"}


class Event(NamedTuple):
    kind: int
    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0

    # ------------------------------------------------------------ 构造器
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

    # ------------------------------------------------------------ 便捷属性
    @property
    def pressed(self) -> bool:
        return bool(self.c)

    @property
    def extended(self) -> bool:
        return bool(self.d)

    def describe(self) -> str:  # 仅用于日志
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
