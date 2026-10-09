"""Virtual desktop geometry model.

CrossPC puts every screen that takes part in the share into one "virtual
coordinate system":

        (0,0)                        the server's virtual desktop (may be multi-monitor)
        +---------------------------+
        |          server           |            +---------------------+
        |      (1920 x 1080)        |  ------>   |  client "debian"    |
        +---------------------------+            |  rect=(1920,0,2560,1440)
                                                 +---------------------+

* the server rectangle is pinned at (0,0), and its size = the server's own
  virtual desktop size;
* every client has a rectangle, dragged out in the "relative position" UI (or
  written straight into the config);
* the mouse moves in virtual coordinates, and whichever rectangle it lands in
  is the machine that handles it -- that is the entire principle behind "move
  the mouse to the edge of the screen, keep moving, and it ends up on the other
  PC";
* gaps between rectangles do not lose the cursor either: resolve() snaps the
  cursor to the edge of the nearest rectangle.

This module is pure computation and depends on no platform, so unit tests can
cover it completely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

ROLE_SERVER = "server"
ROLE_CLIENT = "client"

# edge directions
LEFT, RIGHT, TOP, BOTTOM = "left", "right", "top", "bottom"


@dataclass(frozen=True)
class Rect:
    """Screen rectangle. x/y is the top-left corner in virtual coordinates, in physical pixels."""

    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    # ------------------------------------------------------------ edge properties
    @property
    def left(self) -> int:
        return self.x

    @property
    def top(self) -> int:
        return self.y

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center(self) -> Tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def contains(self, px: int, py: int) -> bool:
        """Half-open interval test, so adjacent rectangles never both claim the same point."""
        return self.x <= px < self.right and self.y <= py < self.bottom

    def clamp(self, px: int, py: int) -> Tuple[int, int]:
        """Clamp a **virtual coordinate** point into the rectangle (closed interval, the bottom-right corner is right-1/bottom-1)."""
        cx = min(max(px, self.x), max(self.x, self.right - 1))
        cy = min(max(py, self.y), max(self.y, self.bottom - 1))
        return cx, cy

    def clamp_local(self, lx: int, ly: int) -> Tuple[int, int]:
        """Clamp a **local coordinate** point (top-left is 0,0) into 0..w-1 / 0..h-1.

        The difference from clamp() is very easy to get wrong, so it lives in a
        method of its own: as soon as the coordinates are "relative to the
        top-left corner of the local screen", this one must be used, otherwise
        the point gets pushed to wherever the rectangle sits in the virtual
        desktop.
        """
        cx = min(max(lx, 0), max(self.w - 1, 0))
        cy = min(max(ly, 0), max(self.h - 1, 0))
        return cx, cy

    def distance_to(self, px: int, py: int) -> float:
        cx, cy = self.clamp(px, py)
        return ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5

    def edge_at(self, px: int, py: int) -> Optional[str]:
        """Which edge of this rectangle the point is against (used to tell "which way it wants to cross out")."""
        if not (self.x <= px < self.right and self.y <= py < self.bottom):
            return None
        if px <= self.x:
            return LEFT
        if px >= self.right - 1:
            return RIGHT
        if py <= self.y:
            return TOP
        if py >= self.bottom - 1:
            return BOTTOM
        return None

    def touches(self, other: "Rect") -> Optional[str]:
        """The edge direction of this rectangle relative to other (sharing an edge or overlapping counts as touching)."""
        if self.right <= other.left and self.bottom > other.top and self.y < other.bottom:
            return LEFT
        if self.left >= other.right and self.bottom > other.top and self.y < other.bottom:
            return RIGHT
        if self.bottom <= other.top and self.right > other.x and self.x < other.right:
            return TOP
        if self.top >= other.bottom and self.right > other.x and self.x < other.right:
            return BOTTOM
        return None

    def union(self, other: "Rect") -> "Rect":
        x0, y0 = min(self.x, other.x), min(self.y, other.y)
        x1, y1 = max(self.right, other.right), max(self.bottom, other.bottom)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def as_dict(self) -> Dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @staticmethod
    def from_dict(d, default: "Rect" = None) -> "Rect":
        if not d:
            return default if default is not None else Rect()
        if isinstance(d, (list, tuple)):
            if len(d) != 4:
                raise ValueError("a rect needs 4 numbers [x,y,w,h], got %r" % (d,))
            return Rect(*(int(v) for v in d))
        return Rect(int(d.get("x", 0)), int(d.get("y", 0)),
                    int(d.get("w", 0)), int(d.get("h", 0)))

    def __str__(self) -> str:
        return "(%d,%d %dx%d)" % (self.x, self.y, self.w, self.h)


@dataclass
class Machine:
    """One machine taking part in the share."""

    name: str
    role: str
    rect: Rect
    host: str = ""                       # for a client: the server address
    port: int = 0
    monitors: List[Rect] = field(default_factory=list)  # that machine's own monitor layout

    @property
    def is_server(self) -> bool:
        return self.role == ROLE_SERVER

    def local_to_virtual(self, lx: int, ly: int) -> Tuple[int, int]:
        """That machine's desktop coordinates -> virtual coordinates."""
        return self.rect.x + lx, self.rect.y + ly

    def virtual_to_local(self, vx: int, vy: int) -> Tuple[int, int]:
        """Virtual coordinates -> that machine's desktop coordinates."""
        return vx - self.rect.x, vy - self.rect.y


class Layout:
    """The layout of the whole virtual desktop. One server plus any number of clients."""

    def __init__(self, server: Machine, clients: Iterable[Machine] = ()):
        self.server = server
        self.clients: List[Machine] = list(clients)

    # ------------------------------------------------------------ queries
    @property
    def machines(self) -> List[Machine]:
        return [self.server] + self.clients

    def by_name(self, name: str) -> Optional[Machine]:
        for m in self.machines:
            if m.name == name:
                return m
        return None

    def bounds(self) -> Rect:
        r = self.server.rect
        for m in self.clients:
            r = r.union(m.rect)
        return r

    def machine_at(self, vx: int, vy: int) -> Optional[Machine]:
        """Strict hit test: which rectangle the virtual coordinates fall into."""
        for m in self.machines:
            if m.rect.contains(vx, vy):
                return m
        return None

    def resolve(self, vx: int, vy: int, prefer: Optional[Machine] = None
                ) -> Tuple[Machine, int, int, bool]:
        """Resolve virtual coordinates into (machine, local coordinates on it,
        whether it had to be snapped back inside a rectangle).

        When the point falls into a gap between rectangles it snaps to the
        nearest rectangle, so the cursor is never "lost". prefer keeps the
        current machine on a tie, avoiding jitter back and forth at a seam.
        """
        hit = self.machine_at(vx, vy)
        if hit is not None:
            lx, ly = hit.virtual_to_local(vx, vy)
            return hit, lx, ly, False

        best: Optional[Machine] = None
        best_d = None
        for m in self.machines:
            d = m.rect.distance_to(vx, vy)
            if prefer is not None and m is prefer:
                d -= 2.0          # 2-pixel hysteresis: do not jitter right in the middle of a gap
            if best_d is None or d < best_d:
                best, best_d = m, d
        assert best is not None
        lx, ly = best.rect.clamp(vx, vy)
        lx, ly = best.virtual_to_local(lx, ly)
        return best, lx, ly, True

    # ------------------------------------------------------------ edge decisions
    def exit_direction(self, machine: Machine, dx: int, dy: int,
                       lx: int, ly: int) -> Optional[str]:
        """On machine, when the cursor is against an edge and keeps being pushed
        outwards, return the direction it is pushed towards.

        lx/ly are local coordinates on machine, dx/dy is the physical delta for
        this event.
        """
        r = machine.rect
        vx, vy = machine.local_to_virtual(lx, ly)
        edge = r.edge_at(vx, vy)
        if edge is None:
            return None
        if edge == LEFT and dx < 0:
            return LEFT
        if edge == RIGHT and dx > 0:
            return RIGHT
        if edge == TOP and dy < 0:
            return TOP
        if edge == BOTTOM and dy > 0:
            return BOTTOM
        return None

    def neighbour(self, machine: Machine, direction: str,
                  lx: int, ly: int, tolerance: int = 128) -> Optional[Machine]:
        """Going out of machine along direction, which machine would we land on
        (possibly none).

        We probe for a perfect touch first; hand-written coordinates in the
        config are easily a few pixels off (or deliberately leave a gap), so the
        search distance is relaxed step by step up to tolerance pixels --
        otherwise "the mouse reaches the edge and simply cannot get through"
        turns into a very hard-to-diagnose problem. Positions dragged out in the
        UI are snapped automatically, so a normal gap is 0.
        """
        vx, vy = machine.local_to_virtual(lx, ly)
        if direction == LEFT:
            sx, sy = -1, 0
        elif direction == RIGHT:
            sx, sy = 1, 0
        elif direction == TOP:
            sx, sy = 0, -1
        elif direction == BOTTOM:
            sx, sy = 0, 1
        else:
            return None
        for step in (2, 8, 32, max(tolerance, 32)):
            m = self.machine_at(vx + sx * step, vy + sy * step)
            if m is not None and m is not machine:
                return m
        return None

    def describe(self) -> str:
        lines = ["virtual desktop %s" % self.bounds()]
        for m in self.machines:
            tag = "server" if m.is_server else ("client@%s:%d" % (m.host, m.port)
                                                if m.host else "client")
            lines.append("  %-12s %-9s %s" % (m.name, tag, m.rect))
        return "\n".join(lines)


def relative_direction(old: Rect, new: Rect) -> str:
    """Which side of old new is on (the dominant axis of the line between their
    centers).

    Used when control changes machines, to decide which edge to enter the new
    machine from. With a fully overlapping or diagonal layout it takes the axis
    with the larger offset, which is both stable and intuitive.
    """
    dx = (new.x + new.w // 2) - (old.x + old.w // 2)
    dy = (new.y + new.h // 2) - (old.y + old.h // 2)
    if abs(dx) >= abs(dy):
        return RIGHT if dx >= 0 else LEFT
    return BOTTOM if dy >= 0 else TOP


def make_server(name: str, w: int, h: int,
                monitors: Optional[List[Rect]] = None) -> Machine:
    """The server rectangle: the local virtual desktop, normalized so the top-left corner is (0,0)."""
    return Machine(name=name, role=ROLE_SERVER, rect=Rect(0, 0, w, h),
                   monitors=list(monitors or []))


def make_client(name: str, x: int, y: int, w: int, h: int,
                host: str = "", port: int = 0) -> Machine:
    return Machine(name=name, role=ROLE_CLIENT, rect=Rect(x, y, w, h),
                   host=host, port=port)
