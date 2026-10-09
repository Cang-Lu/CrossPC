"""危险情况下的"一键收回": 解析并识别紧急/锁定热键。

热键在 server 上识别, 用的也是扫描码, 所以不吃输入法/键盘布局的影响。
识别到之后这个按键会被吞掉, 不会再转发到远端(免得在 Debian 上多打一个字)。
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Set, Tuple

from .events import KEY, Event
from .keys import parse_key_name

KeyId = Tuple[int, bool]          # (扫描码, 是否 E0 扩展)


class HotkeyError(ValueError):
    pass


def parse_spec(spec: str) -> Tuple[List[KeyId], List[KeyId]]:
    """"ctrl+alt+f12" -> (修饰键列表, 触发键列表)。"""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("热键为空")
    keys: List[KeyId] = []
    for p in parts:
        k = parse_key_name(p)
        if k is None:
            raise HotkeyError("不认识的热键名: %r" % p)
        keys.append(k)
    return keys[:-1], [keys[-1]]


class Hotkey:
    """按住全部修饰键再按触发键 => 触发。

    也支持写成 "ctrl+alt+q" 这种单字符触发键; 若触发键本身就是修饰键,
    则修饰键集合里会同时包含它, 不影响判断。
    """

    def __init__(self, spec: str, name: str = ""):
        self.spec = spec or ""
        self.name = name or self.spec
        self.mods, self.triggers = parse_spec(self.spec)
        self._down: Set[KeyId] = set()

    def __bool__(self) -> bool:
        return bool(self.spec)

    def __str__(self) -> str:
        return self.spec

    def feed(self, ev: Event) -> bool:
        """喂一个按键事件; 命中返回 True(调用方应吞掉该事件)。"""
        if ev.kind != KEY:
            return False
        key: KeyId = (ev.a, bool(ev.d))
        if not ev.c:
            self._down.discard(key)
            return False
        self._down.add(key)
        if key not in self.triggers:
            return False
        if all(m in self._down for m in self.mods):
            self._down.clear()          # 防止长按重复触发
            return True
        return False

    def reset(self) -> None:
        self._down.clear()


def make_hotkeys(panic: str, lock: str) -> Tuple[Optional[Hotkey], Optional[Hotkey]]:
    out = []
    for spec, name in ((panic, "紧急收回"), (lock, "锁定/解锁")):
        if not spec:
            out.append(None)
            continue
        try:
            out.append(Hotkey(spec, name))
        except HotkeyError as exc:
            raise HotkeyError("热键 %r 无效: %s" % (spec, exc)) from exc
    return out[0], out[1]
