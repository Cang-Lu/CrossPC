"""Input routing state machine (pure logic, no platform, no network, fully unit-testable).

The only question one mouse movement has to answer is: "which machine is the
virtual cursor on right now?"

* lands on the server  -> let the local cursor follow (nothing needs to be done,
  the system has already moved it);
* lands on a client    -> send the event to that client, and "park" the local
  cursor in a screen corner so it does not run around;
* control changes machine -> first release every key held down on the old
  machine (stuck-key protection), then place the cursor on the entry edge of the
  new machine.

This class performs no actions; it only returns a list of Action objects, which
the App layer translates into side effects such as "move the local cursor" or
"send a network packet". That way a test can run the whole logic without a
keyboard, a mouse, or a network.
"""
from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence, Tuple

from .events import BUTTON, KEY, MOTION, WHEEL, Event
from .layout import BOTTOM, LEFT, RIGHT, TOP, Layout, Machine, Rect, relative_direction


class Action(NamedTuple):
    """Router's output.

    kind:
      local  move the local cursor to (x, y) (local desktop coordinates), and
             make sure forwarding mode has been left
      enter  enter machine: turn on forwarding mode, set the park point, and put
             the cursor at (x, y)
      leave  leave machine: first send it the key-release events in events, then
             turn forwarding mode off
      remote send events to machine
    """

    kind: str
    machine: Optional[Machine] = None
    x: int = 0
    y: int = 0
    events: Tuple[Event, ...] = ()


class Router:
    def __init__(self, layout: Layout, log=None):
        self.layout = layout
        self._log = log or (lambda m: None)
        self.active: Machine = layout.server
        self.vx, self.vy = layout.server.rect.center
        self.locked = False
        self._pressed: List[Event] = []          # keys already forwarded but not yet released

    # ------------------------------------------------------------ queries
    @property
    def remote(self) -> bool:
        return not self.active.is_server

    @property
    def active_name(self) -> str:
        return self.active.name

    def pressed_count(self) -> int:
        return len(self._pressed)

    def set_locked(self, locked: bool) -> None:
        """Once locked, the mouse will not leave the current machine even when pushed against an edge (toggled with a hotkey)."""
        self.locked = bool(locked)
        if self.locked and self.active.is_server:
            self.locked = False
        self._log("lock state: %s" % ("locked to %s" % self.active.name
                                      if self.locked else "unlocked"))

    # ------------------------------------------------------------ event entry point
    def on_event(self, ev: Event) -> List[Action]:
        if ev.kind == MOTION:
            return self._on_motion(ev)
        return self._on_other(ev)

    # ------------------------------------------------------------ mouse motion
    def _on_motion(self, ev: Event) -> List[Action]:
        if self.active.is_server:
            return self._motion_on_server(ev)
        return self._motion_on_client(ev)

    def _motion_on_server(self, ev: Event) -> List[Action]:
        """In local mode the local cursor position is authoritative, and the delta is borrowed only to tell "does it want to cross out"."""
        srv = self.layout.server
        lx, ly = ev.a, ev.b
        self.vx, self.vy = srv.local_to_virtual(lx, ly)
        if not (ev.c or ev.d):
            return []
        if self.locked:
            return []
        direction = self.layout.exit_direction(srv, ev.c, ev.d, lx, ly)
        if direction is None:
            return []
        target = self.layout.neighbour(srv, direction, lx, ly)
        if target is None or target.is_server:
            return []                      # no machine over there, so the cursor just stops at the edge
        return self._switch(target, direction, self.vx + ev.c, self.vy + ev.d)

    def _motion_on_client(self, ev: Event) -> List[Action]:
        old = self.active
        self.vx += ev.c
        self.vy += ev.d
        if self.locked:
            lx, ly = old.rect.clamp(self.vx, self.vy)
            self.vx, self.vy = old.local_to_virtual(lx, ly)
            return [Action("remote", old, events=(Event.motion(lx, ly),))]

        target, lx, ly, snapped = self.layout.resolve(self.vx, self.vy, prefer=old)
        if target is old:
            # still on the same machine (including "fell into a gap and got
            # snapped back")
            return [Action("remote", old, events=(Event.motion(lx, ly),))]
        # Note: do not hang on to the old machine just because of snapped. When
        # the mouse is flicked past an edge quickly, the virtual coordinates may
        # jump far out in a single step and land outside every rectangle;
        # resolve() then snaps them to the **nearest** rectangle -- and that
        # rectangle may perfectly well be the server (which is exactly the
        # criterion for "the mouse slid back in from the edge of the client").
        # Whoever is nearest owns it.
        _ = snapped
        return self._switch(target, relative_direction(old.rect, target.rect),
                            self.vx, self.vy)

    def _switch(self, target: Machine, direction: str,
                vx: int, vy: int) -> List[Action]:
        """Hand control over from self.active to target. direction is the direction of travel."""
        old = self.active
        actions: List[Action] = []
        if not old.is_server:
            actions.append(self._leave_action(old))
        lx, ly = self._entry_position(target, direction, vx, vy)
        if target.is_server:
            # Going back to the local machine is not "entering a client" but
            # local: turn takeover off + put the cursor at the entry point
            actions.append(Action("local", x=lx, y=ly))
        else:
            actions.append(Action("enter", target, lx, ly))
        self.active = target
        self.vx, self.vy = target.local_to_virtual(lx, ly)
        self._log("control: %s -> %s (local coordinates %d,%d)"
                  % (old.name, target.name, lx, ly))
        return actions

    def _entry_position(self, target: Machine, direction: str,
                        vx: int, vy: int) -> Tuple[int, int]:
        """Where the cursor should land when entering the target machine: the crossing axis sticks to the entry edge, the other axis maps 1:1."""
        r = target.rect
        if direction == RIGHT:
            lx, ly = 0, vy - r.y
        elif direction == LEFT:
            lx, ly = r.w - 1, vy - r.y
        elif direction == TOP:
            lx, ly = vx - r.x, 0
        else:                                   # BOTTOM
            lx, ly = vx - r.x, r.h - 1
        # These are "local coordinates", so clamp_local must be used (not clamp,
        # which is for virtual coordinates)
        return r.clamp_local(lx, ly)

    # ------------------------------------------------------------ keys / wheel
    def _on_other(self, ev: Event) -> List[Action]:
        if self.active.is_server:
            return []          # in local mode the system already handled it, so it must not be forwarded
        if ev.kind == KEY:
            if ev.c:
                if ev not in self._pressed:
                    self._pressed.append(ev)
            elif ev in self._pressed:
                self._pressed.remove(ev)
        return [Action("remote", self.active, events=(ev,))]

    def _release_events(self) -> Tuple[Event, ...]:
        """Send the missing release event for every key that is down but not released. Lock keys (CapsLock etc.) are left alone."""
        out = []
        for ev in self._pressed:
            out.append(Event.key(ev.a, ev.b, False, bool(ev.d)))
        self._pressed = []
        return tuple(out)

    def _leave_action(self, machine: Machine) -> Action:
        return Action("leave", machine, events=self._release_events())

    # ------------------------------------------------------------ forced return to the local machine
    def force_local(self, reason: str = "", park: Optional[Tuple[int, int]] = None
                    ) -> List[Action]:
        """Take control back to the server at once: disconnects, the watchdog and
        the panic hotkey all go through here.

        Better to have "the mouse suddenly jumps back to the local machine" than
        to leave the user facing a computer that will not obey.
        """
        if self.active.is_server:
            return []
        old = self.active
        actions = [self._leave_action(old)]
        if park is not None:
            lx, ly = self.layout.server.rect.clamp(*park)
        else:
            lx, ly = self.layout.server.rect.clamp(self.vx, self.vy)
        actions.append(Action("local", x=lx, y=ly))
        self.active = self.layout.server
        self.vx, self.vy = self.layout.server.local_to_virtual(lx, ly)
        self.locked = False
        self._log("forced control recall%s" % ("(%s)" % reason if reason else ""))
        return actions

    def describe(self) -> str:
        return "active: %s%s | virtual cursor %d,%d | keys held %d" % (
            self.active.name, " (locked)" if self.locked else "",
            self.vx, self.vy, len(self._pressed))
