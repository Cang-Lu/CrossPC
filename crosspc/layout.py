"""虚拟桌面几何模型。

CrossPC 把所有参与共享的屏幕放在同一个"虚拟坐标系"里:

        (0,0)                        server 的虚拟桌面(可能多显示器)
        +---------------------------+
        |          server           |            +---------------------+
        |      (1920 x 1080)        |  ------>   |  client "debian"    |
        +---------------------------+            |  rect=(1920,0,2560,1440)
                                                 +---------------------+

* server 的矩形固定放在 (0,0), 尺寸 = 它自己的虚拟桌面尺寸;
* 每个 client 有一个矩形, 由"相对位置设置"界面拖出来(或直接写配置);
* 鼠标在虚拟坐标里移动, 落在哪个矩形里就由哪台机器处理 —— 这就是
  "鼠标移到屏幕边缘继续移动就跑到另一台电脑"的全部原理;
* 矩形之间留缝隙也不会丢光标: resolve() 会把光标吸附到最近的矩形边缘。

本模块是纯计算, 不依赖任何平台, 因此可以完全用单元测试覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

ROLE_SERVER = "server"
ROLE_CLIENT = "client"

# 边缘方向
LEFT, RIGHT, TOP, BOTTOM = "left", "right", "top", "bottom"


@dataclass(frozen=True)
class Rect:
    """屏幕矩形。x/y 是虚拟坐标系里的左上角, 单位物理像素。"""

    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    # ------------------------------------------------------------ 边界属性
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
        """半开区间判定, 保证相邻矩形不会同时命中同一个点。"""
        return self.x <= px < self.right and self.y <= py < self.bottom

    def clamp(self, px: int, py: int) -> Tuple[int, int]:
        """把**虚拟坐标**的点夹到矩形内(闭区间, 最右下角是 right-1/bottom-1)。"""
        cx = min(max(px, self.x), max(self.x, self.right - 1))
        cy = min(max(py, self.y), max(self.y, self.bottom - 1))
        return cx, cy

    def clamp_local(self, lx: int, ly: int) -> Tuple[int, int]:
        """把**本机坐标**(左上角为 0,0)的点夹到 0..w-1 / 0..h-1。

        和 clamp() 的区别很容易搞混, 所以单独一个方法: 坐标一旦是"相对本机
        屏幕左上角"的, 就必须用这个, 否则会被顶到矩形在虚拟桌面里的位置上去。
        """
        cx = min(max(lx, 0), max(self.w - 1, 0))
        cy = min(max(ly, 0), max(self.h - 1, 0))
        return cx, cy

    def distance_to(self, px: int, py: int) -> float:
        cx, cy = self.clamp(px, py)
        return ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5

    def edge_at(self, px: int, py: int) -> Optional[str]:
        """点贴在该矩形的哪条边上(用于判断"想从哪边穿出去")。"""
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
        """本矩形相对 other 的贴边方向(共边或重叠即算贴住)。"""
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
                raise ValueError("矩形要 4 个数 [x,y,w,h], 收到 %r" % (d,))
            return Rect(*(int(v) for v in d))
        return Rect(int(d.get("x", 0)), int(d.get("y", 0)),
                    int(d.get("w", 0)), int(d.get("h", 0)))

    def __str__(self) -> str:
        return "(%d,%d %dx%d)" % (self.x, self.y, self.w, self.h)


@dataclass
class Machine:
    """一台参与共享的机器。"""

    name: str
    role: str
    rect: Rect
    host: str = ""                       # client 用: server 地址
    port: int = 0
    monitors: List[Rect] = field(default_factory=list)  # 该机自己的显示器布局

    @property
    def is_server(self) -> bool:
        return self.role == ROLE_SERVER

    def local_to_virtual(self, lx: int, ly: int) -> Tuple[int, int]:
        """该机桌面坐标 -> 虚拟坐标。"""
        return self.rect.x + lx, self.rect.y + ly

    def virtual_to_local(self, vx: int, vy: int) -> Tuple[int, int]:
        """虚拟坐标 -> 该机桌面坐标。"""
        return vx - self.rect.x, vy - self.rect.y


class Layout:
    """整张虚拟桌面的布局。server 一个 + client 若干。"""

    def __init__(self, server: Machine, clients: Iterable[Machine] = ()):
        self.server = server
        self.clients: List[Machine] = list(clients)

    # ------------------------------------------------------------ 查询
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
        """严格命中测试: 虚拟坐标落在哪个矩形里。"""
        for m in self.machines:
            if m.rect.contains(vx, vy):
                return m
        return None

    def resolve(self, vx: int, vy: int, prefer: Optional[Machine] = None
                ) -> Tuple[Machine, int, int, bool]:
        """把虚拟坐标解析成 (机器, 该机本地坐标, 是否被吸附回矩形内)。

        落在矩形之间的缝隙时, 吸附到最近的矩形, 光标永远不会"丢"。
        prefer 用于平局时优先保持当前机器, 避免在接缝处来回抖动。
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
                d -= 2.0          # 2 像素迟滞: 缝隙正中间时不要来回抖
            if best_d is None or d < best_d:
                best, best_d = m, d
        assert best is not None
        lx, ly = best.rect.clamp(vx, vy)
        lx, ly = best.virtual_to_local(lx, ly)
        return best, lx, ly, True

    # ------------------------------------------------------------ 边缘判断
    def exit_direction(self, machine: Machine, dx: int, dy: int,
                       lx: int, ly: int) -> Optional[str]:
        """在 machine 上, 光标贴边且继续往外推时, 返回推出的方向。

        lx/ly 是 machine 的本地坐标, dx/dy 是本次物理位移。
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
        """从 machine 沿 direction 出去, 会落到哪台机器上(可能没有)。

        先按"紧贴"探测; 配置里手写坐标时很容易差几个像素(或者故意留条缝),
        所以逐步放宽搜索距离到 tolerance 像素 —— 否则会出现"鼠标顶到边上
        就是过不去"这种非常难查的问题。界面里拖出来的位置是自动吸附的,
        正常 gap 为 0。
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
        lines = ["虚拟桌面 %s" % self.bounds()]
        for m in self.machines:
            tag = "server" if m.is_server else ("client@%s:%d" % (m.host, m.port)
                                                if m.host else "client")
            lines.append("  %-12s %-9s %s" % (m.name, tag, m.rect))
        return "\n".join(lines)


def relative_direction(old: Rect, new: Rect) -> str:
    """new 在 old 的哪一侧(取中心连线的主轴)。

    用于"控制权换机器"时决定从哪条边进入新机器。完全重叠/对角线布局时取
    位移更大的那个轴, 结果稳定且符合直觉。
    """
    dx = (new.x + new.w // 2) - (old.x + old.w // 2)
    dy = (new.y + new.h // 2) - (old.y + old.h // 2)
    if abs(dx) >= abs(dy):
        return RIGHT if dx >= 0 else LEFT
    return BOTTOM if dy >= 0 else TOP


def make_server(name: str, w: int, h: int,
                monitors: Optional[List[Rect]] = None) -> Machine:
    """server 的矩形: 本机虚拟桌面, 左上角归一到 (0,0)。"""
    return Machine(name=name, role=ROLE_SERVER, rect=Rect(0, 0, w, h),
                   monitors=list(monitors or []))


def make_client(name: str, x: int, y: int, w: int, h: int,
                host: str = "", port: int = 0) -> Machine:
    return Machine(name=name, role=ROLE_CLIENT, rect=Rect(x, y, w, h),
                   host=host, port=port)
