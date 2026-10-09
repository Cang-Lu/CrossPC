"""The 'one-key recall' for dangerous situations: parse and recognize the panic
and lock hotkeys.

Hotkeys are recognized on the server, and they too work on scancodes, so they
are unaffected by the input method or the keyboard layout. Once recognized, the
key is suppressed and is no longer forwarded to the remote side (so we do not
type an extra character on Debian).
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Set, Tuple

from .events import KEY, Event
from .keys import parse_key_name

KeyId = Tuple[int, bool]          # (scancode, whether it is E0 extended)


class HotkeyError(ValueError):
    pass


def parse_spec(spec: str) -> Tuple[List[KeyId], List[KeyId]]:
    """"ctrl+alt+f12" -> (list of modifier keys, list of trigger keys)."""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("hotkey is empty")
    keys: List[KeyId] = []
    for p in parts:
        k = parse_key_name(p)
        if k is None:
            raise HotkeyError("unknown hotkey name: %r" % p)
        keys.append(k)
    return keys[:-1], [keys[-1]]


class Hotkey:
    """Fire when all modifier keys are held down and then a trigger key is hit.

    A single-character trigger key such as "ctrl+alt+q" is also supported; if
    the trigger key is itself a modifier, the modifier set will contain it as
    well, which does not affect the decision.
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
        """Feed one key event; returns True on a hit (the caller should suppress it)."""
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
            self._down.clear()          # avoid retriggering while held down
            return True
        return False

    def reset(self) -> None:
        self._down.clear()


def make_hotkeys(panic: str, lock: str) -> Tuple[Optional[Hotkey], Optional[Hotkey]]:
    out = []
    for spec, name in ((panic, "panic release"), (lock, "lock/unlock")):
        if not spec:
            out.append(None)
            continue
        try:
            out.append(Hotkey(spec, name))
        except HotkeyError as exc:
            raise HotkeyError("hotkey %r is invalid: %s" % (spec, exc)) from exc
    return out[0], out[1]
