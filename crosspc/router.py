"""输入路由状态机(纯逻辑, 无平台无网络, 完全可单测)。

一次鼠标移动要回答的唯一问题是: "虚拟光标现在落在哪台机器上?"

* 落在 server 上  -> 让本机光标跟着动(不需要做任何事, 系统已经动了);
* 落在 client 上  -> 把事件发给那台 client, 同时把本机光标"停靠"在屏幕
  边角上避免乱跑;
* 控制权换机器时  -> 先把旧机器上按着的键全部抬起(防粘键), 再在新机器的
  进入边放置光标。

本类不执行动作, 只返回 Action 列表, 由 App 层翻译成"移动本机光标 / 发网络包"
等副作用。这样测试可以在没有键鼠、没有网络的情况下把整套逻辑跑一遍。
"""
from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence, Tuple

from .events import BUTTON, KEY, MOTION, WHEEL, Event
from .layout import BOTTOM, LEFT, RIGHT, TOP, Layout, Machine, Rect, relative_direction


class Action(NamedTuple):
    """Router 的输出。

    kind:
      local  把本机光标移到 (x, y)(本机桌面坐标), 并确保已退出转发模式
      enter  进入 machine: 打开转发模式、设置停靠点、把光标放到 (x, y)
      leave  离开 machine: 先给它补发 events 里的抬键事件, 再关掉转发模式
      remote 把 events 发给 machine
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
        self._pressed: List[Event] = []          # 已转发出去、还没抬起的键

    # ------------------------------------------------------------ 查询
    @property
    def remote(self) -> bool:
        return not self.active.is_server

    @property
    def active_name(self) -> str:
        return self.active.name

    def pressed_count(self) -> int:
        return len(self._pressed)

    def set_locked(self, locked: bool) -> None:
        """锁定后鼠标顶到边缘也不会离开当前机器(用热键切换)。"""
        self.locked = bool(locked)
        if self.locked and self.active.is_server:
            self.locked = False
        self._log("锁定状态: %s" % ("已锁定在 %s" % self.active.name
                                    if self.locked else "未锁定"))

    # ------------------------------------------------------------ 事件入口
    def on_event(self, ev: Event) -> List[Action]:
        if ev.kind == MOTION:
            return self._on_motion(ev)
        return self._on_other(ev)

    # ------------------------------------------------------------ 鼠标移动
    def _on_motion(self, ev: Event) -> List[Action]:
        if self.active.is_server:
            return self._motion_on_server(ev)
        return self._motion_on_client(ev)

    def _motion_on_server(self, ev: Event) -> List[Action]:
        """本机模式下本机光标位置是权威的, 只借用位移判断"想不想穿出去"。"""
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
            return []                      # 那边没有机器, 光标就停在边上
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
            # 还在同一台机器上(包括"落进缝隙又被吸回来"的情况)
            return [Action("remote", old, events=(Event.motion(lx, ly),))]
        # 注意: 不能因为 snapped 就赖在原机器上。鼠标快速甩过边缘时虚拟坐标
        # 可能一次跨出去很远, 落在所有矩形之外, resolve() 会把它吸附到**最近的**
        # 矩形 —— 那个矩形完全可能就是 server(这正是"鼠标从 client 边缘滑回来"
        # 的判定依据)。谁最近就归谁。
        _ = snapped
        return self._switch(target, relative_direction(old.rect, target.rect),
                            self.vx, self.vy)

    def _switch(self, target: Machine, direction: str,
                vx: int, vy: int) -> List[Action]:
        """把控制权从 self.active 切到 target。direction 是移动方向。"""
        old = self.active
        actions: List[Action] = []
        if not old.is_server:
            actions.append(self._leave_action(old))
        lx, ly = self._entry_position(target, direction, vx, vy)
        if target.is_server:
            # 回本机不是"进入某个 client", 而是 local: 关掉接管 + 把光标放到进入点
            actions.append(Action("local", x=lx, y=ly))
        else:
            actions.append(Action("enter", target, lx, ly))
        self.active = target
        self.vx, self.vy = target.local_to_virtual(lx, ly)
        self._log("控制权: %s -> %s (本地坐标 %d,%d)" % (old.name, target.name, lx, ly))
        return actions

    def _entry_position(self, target: Machine, direction: str,
                        vx: int, vy: int) -> Tuple[int, int]:
        """进入目标机器时光标应该落在哪: 穿越轴贴住进入边, 另一轴 1:1 映射。"""
        r = target.rect
        if direction == RIGHT:
            lx, ly = 0, vy - r.y
        elif direction == LEFT:
            lx, ly = r.w - 1, vy - r.y
        elif direction == TOP:
            lx, ly = vx - r.x, 0
        else:                                   # BOTTOM
            lx, ly = vx - r.x, r.h - 1
        # 这里是"本机坐标", 必须用 clamp_local(不能用 clamp, 那是虚拟坐标)
        return r.clamp_local(lx, ly)

    # ------------------------------------------------------------ 按键/滚轮
    def _on_other(self, ev: Event) -> List[Action]:
        if self.active.is_server:
            return []          # 本机模式下系统已经处理过了, 不能再转发
        if ev.kind == KEY:
            if ev.c:
                if ev not in self._pressed:
                    self._pressed.append(ev)
            elif ev in self._pressed:
                self._pressed.remove(ev)
        return [Action("remote", self.active, events=(ev,))]

    def _release_events(self) -> Tuple[Event, ...]:
        """给所有"按下未抬起"的键补发抬起事件。锁定键(CapsLock 等)不动。"""
        out = []
        for ev in self._pressed:
            out.append(Event.key(ev.a, ev.b, False, bool(ev.d)))
        self._pressed = []
        return tuple(out)

    def _leave_action(self, machine: Machine) -> Action:
        return Action("leave", machine, events=self._release_events())

    # ------------------------------------------------------------ 强制回本机
    def force_local(self, reason: str = "", park: Optional[Tuple[int, int]] = None
                    ) -> List[Action]:
        """立刻把控制权收回 server: 断线、看门狗、紧急热键都走这里。

        宁可"鼠标突然回到本机", 也不能让用户面对一台不听使唤的电脑。
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
        self._log("强制收回控制权%s" % ("(%s)" % reason if reason else ""))
        return actions

    def describe(self) -> str:
        return "当前控制: %s%s | 虚拟光标 %d,%d | 按住的键 %d" % (
            self.active.name, " (已锁定)" if self.locked else "",
            self.vx, self.vy, len(self._pressed))
