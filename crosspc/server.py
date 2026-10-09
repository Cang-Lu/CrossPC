"""server 端应用: 接键鼠的那台机器。

进程结构(线程一览):
  主线程      监听连接 + 维护状态 + 收尾
  crosspc-hook 低层键鼠钩子(后端内部), 只管把事件丢给 _on_input
  link-tx-*/link-rx-*  每个 client 一条 TCP 链路的收发线程
  clipboard   剪辑板轮询
  discovery   UDP 自动发现
  crosspc-watchdog 后端内部的钩子存活看门狗

安全底线(出任何问题都要保证用户的键鼠还能用):
  1. 只有"某台 client 真实连着"时才进入接管模式;
  2. client 链路断掉/心跳超时 -> 立刻 force_local, 收回控制权;
  3. 钩子线程死掉 -> 后端看门狗恢复本机输入;
  4. Ctrl+Alt+F12(可配) 无条件收回控制权。
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Dict, List, Optional

from . import __version__
from .backend.base import Backend, BackendError
from .clipboard import ClipboardSync
from .config import Config, ConfigError, SizeCache
from .events import KEY, MOTION, Event
from .hotkey import Hotkey, make_hotkeys
from .layout import Layout, Machine, Rect, make_client
from .net import Discovery, Link, make_listener
from .protocol import (MAGIC, PROTOCOL_VERSION, T_CLIPBOARD,
                       T_CLIPBOARD_IMAGE, T_CONTROL, T_ERROR, T_HELLO, T_INPUT,
                       clipboard_image, clipboard_text as clipboard_msg,
                       control, error as error_msg, hello_ack, parse_json,
                       ProtocolError)
from .router import Action, Router
from .util import Log, desktop_from_norm, norm_from_desktop

HANDSHAKE_TIMEOUT = 8.0


class Session:
    """一个已连接(或正在握手)的 client。"""

    def __init__(self, machine: Machine, link: Link, desktop: Rect, name: str):
        self.machine = machine
        self.link = link
        self.desktop = desktop
        self.name = name
        self.connected_at = time.monotonic()

    @property
    def alive(self) -> bool:
        return self.link.alive

    def __str__(self) -> str:
        return "%s(桌面 %s)" % (self.name, self.desktop)


class ServerApp:
    def __init__(self, cfg: Config, backend: Backend, log: Log,
                 port: Optional[int] = None, bind: Optional[str] = None,
                 stats: bool = False, dry_run: bool = False,
                 cache_path: Optional[str] = None):
        self.cfg = cfg
        self.backend = backend
        self.log = log
        self.port = int(port or cfg.port)
        self.bind = bind or cfg.bind
        self.stats = stats
        self.dry_run = dry_run
        self._cache_path = cache_path

        self.desktop = Rect()
        self.layout = Layout(Machine(cfg.name, "server", Rect()))
        self.router: Optional[Router] = None
        self.sessions: Dict[str, Session] = {}
        self._sessions_lock = threading.RLock()
        self._listener: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._discovery: Optional[Discovery] = None
        self._clipboard: Optional[ClipboardSync] = None
        self._panic: Optional[Hotkey] = None
        self._lock_key: Optional[Hotkey] = None
        self._sizes: Optional[SizeCache] = None
        self._last_stats = time.monotonic()
        self._last_reload_check = 0.0
        self._cfg_mtime: Optional[float] = None
        self._input_errors = 0
        self._tick_errors = 0
        self._dropped_logged = 0

    # ------------------------------------------------------------ 启动
    def run(self) -> int:
        self._prepare()
        self._listener = make_listener(self.bind, self.port)
        self._listener.settimeout(0.5)
        self.log.info("CrossPC server %s 已就绪, 监听 %s:%d"
                      % (__version__, self.bind, self.port))
        self.log.info("虚拟桌面:\n%s" % self.layout.describe())
        for ip in _local_ips():
            self.log.info("局域网地址: %s:%d (在 client 上用这个)"
                          % (ip, self.port))
        if self.layout.clients:
            names = ", ".join(m.name for m in self.layout.clients)
            self.log.info("等待 client 连接: %s" % names)
        else:
            self.log.info("配置里还没有 client: 任何知道端口的机器连上来自动登记")
        if not self.cfg.token:
            self.log.warn("配置里没设 token, 局域网内任何机器都能接入"
                          "(家用环境可以接受, 公共网络请设置 token)")

        self._discovery = Discovery(self.cfg.name, self.port,
                                    self.cfg.discovery_port, self.log)
        self._discovery.start()
        self._clipboard = ClipboardSync(
            self.backend, self._broadcast_clipboard, self.log,
            poll_ms=self.cfg.clipboard_poll_ms,
            max_bytes=self.cfg.clipboard_max_bytes,
            enabled=self.cfg.clipboard_enabled, name=self.cfg.name,
            max_image_bytes=self.cfg.clipboard_max_image_bytes,
            images=self.cfg.clipboard_images,
            prefer_image=(self.cfg.clipboard_prefer == "image"))
        self._clipboard.start()
        try:
            self._loop()
        except KeyboardInterrupt:
            self.log.info("收到 Ctrl+C, 正在退出")
        finally:
            self.shutdown()
        return 0

    def _prepare(self) -> None:
        self.backend.prepare()
        self.desktop = self.backend.desktop_rect()
        self.cfg.server_screen = (self.desktop.w, self.desktop.h)
        self._sizes = SizeCache(self._cache_path
                                or SizeCache.default_path(self.cfg.path))
        self.layout = self.cfg.build_layout(
            Rect(0, 0, self.desktop.w, self.desktop.h), self._sizes.sizes)
        if self.desktop.x or self.desktop.y:
            self.log.info("本机虚拟桌面左上角是 %d,%d, 对内部坐标已做归一"
                          % (self.desktop.x, self.desktop.y))
        self.router = Router(self.layout, self.log.debug)
        self._panic, self._lock_key = make_hotkeys(self.cfg.hotkey_panic,
                                                   self.cfg.hotkey_lock)
        if self.dry_run:
            self.log.warn("--dry-run: 不安装键鼠钩子, 只验证网络/握手/剪辑板")
            return
        self.backend.start_capture(self._on_input)
        self.log.info("键鼠捕获已启动(%s)" % self.backend.caps())

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()
            except socket.timeout:
                # 周期性任务出任何岔子都不能弄死主循环: 这个进程同时还是
                # 用户键鼠的"看守", 它挂了用户就得重启才能恢复手感。
                try:
                    self._tick()
                except Exception as exc:
                    self._tick_errors += 1
                    if self._tick_errors <= 3:
                        self.log.error("周期任务出错(第 %d 次): %s"
                                       % (self._tick_errors, exc))
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.log.warn("accept 失败: %s" % exc)
                continue
            t = threading.Thread(target=self._handshake, args=(sock, addr),
                                 name="handshake-%s" % addr[0], daemon=True)
            t.start()

    def _tick(self) -> None:
        now = time.monotonic()
        # 断线兜底: 万一 on_close 没跑到, 这里再兜一次
        with self._sessions_lock:
            dead = [s for s in self.sessions.values() if not s.alive]
        for s in dead:
            self._drop_session(s, "链路已断开")
        if self.router and self.router.remote:
            session = self._session_for(self.router.active)
            if session is None or not session.alive:
                self.log.warn("当前控制的 client 已离线, 立即收回控制权")
                self._force_local("client 离线")
        if now - self._last_reload_check >= 2.0:
            self._last_reload_check = now
            self._maybe_reload_config()
        if self.stats and now - self._last_stats >= 10.0:
            self._last_stats = now
            self._log_stats()

    def _maybe_reload_config(self) -> None:
        """配置文件被 GUI 改过后自动重新加载布局。

        没有这个功能的话, 用户每次在界面上挪一下位置都得重启 server —— 而
        server 重启意味着 client 要重连, 体验很差。重载时先无条件把控制权
        收回本机, 避免在新旧布局之间出现"光标算不清在哪台机器"的中间态。
        """
        path = self.cfg.path
        if not path or not os.path.exists(path):
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if self._cfg_mtime is None:
            self._cfg_mtime = mtime
            return
        if mtime <= self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            new = Config.load(path)
        except ConfigError as exc:
            self.log.warn("配置文件改了但读不动, 继续用旧布局: %s" % exc)
            return
        if self.router and self.router.remote:
            self._force_local("配置变更")
        self.cfg.clients = new.clients
        self.cfg.hotkey_panic = new.hotkey_panic
        self.cfg.hotkey_lock = new.hotkey_lock
        self.cfg.clipboard_enabled = new.clipboard_enabled
        self.cfg.clipboard_poll_ms = new.clipboard_poll_ms
        self.cfg.clipboard_max_bytes = new.clipboard_max_bytes
        self.cfg.clipboard_images = new.clipboard_images
        self.cfg.clipboard_max_image_bytes = new.clipboard_max_image_bytes
        self.cfg.clipboard_prefer = new.clipboard_prefer
        if self._clipboard is not None:
            self._clipboard.poll_ms = new.clipboard_poll_ms
            self._clipboard.max_bytes = new.clipboard_max_bytes
            self._clipboard.max_image_bytes = (new.clipboard_max_image_bytes
                                               if new.clipboard_images else 0)
            self._clipboard.prefer_image = (new.clipboard_prefer == "image")
        try:
            self.layout = self.cfg.build_layout(
                Rect(0, 0, self.desktop.w, self.desktop.h),
                self._sizes.sizes if self._sizes else {})
            self.router = Router(self.layout, self.log.debug)
            self._panic, self._lock_key = make_hotkeys(self.cfg.hotkey_panic,
                                                       self.cfg.hotkey_lock)
            # 已经在线的 client 用上报过的尺寸覆盖一遍配置里的猜测
            with self._sessions_lock:
                for session in self.sessions.values():
                    self._register_client(session.name, session.desktop,
                                          session.machine.host)
        except Exception as exc:
            self.log.warn("重新加载配置失败(继续用旧布局): %s" % exc)
            return
        self.log.info("配置已重新加载:\n%s" % self.layout.describe())

    def _log_stats(self) -> None:
        with self._sessions_lock:
            parts = []
            for s in self.sessions.values():
                parts.append("%s: 发 %d 批/丢 %d" % (s.name, s.link.sent_events,
                                                    s.link.dropped))
        self.log.info("状态: %s | %s" % (self.router.describe() if self.router
                                        else "未就绪",
                                        "; ".join(parts) or "无 client"))

    def shutdown(self) -> None:
        self._stop.set()
        if self.router and self.router.remote:
            self._force_local("server 退出")
        if self._clipboard:
            self._clipboard.stop()
        if self._discovery:
            self._discovery.stop()
        with self._sessions_lock:
            for s in list(self.sessions.values()):
                try:
                    s.link.send_frame(control("bye", reason="server 退出"))
                except Exception:
                    pass
                s.link.close("server 退出")
            self.sessions.clear()
        try:
            self.backend.stop_capture()
        except Exception as exc:
            self.log.debug("卸载捕获失败: %s" % exc)
        try:
            self.backend.close()
        except Exception:
            pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._sizes and self.cfg.path:
            try:
                self.cfg.save()
                self.log.info("配置已更新: %s" % self.cfg.path)
            except Exception as exc:
                self.log.warn("保存配置失败: %s" % exc)
        self.log.info("server 已停止")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 握手
    def _handshake(self, sock: socket.socket, addr) -> None:
        peer = "%s:%d" % (addr[0], addr[1])
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            reader = _read_one_frame(sock)
            if reader is None:
                self.log.warn("%s 握手超时/无数据" % peer)
                sock.close()
                return
            msg_type, payload = reader
            if msg_type != T_HELLO:
                sock.sendall(error_msg("第一条消息必须是 HELLO"))
                sock.close()
                return
            info = parse_json(payload)
            if info.get("magic") != MAGIC:
                raise ValueError("不是 CrossPC 客户端")
            if int(info.get("protocol", 0)) != PROTOCOL_VERSION:
                raise ValueError("协议版本不一致(对端 %s, 本机 %d)"
                                 % (info.get("protocol"), PROTOCOL_VERSION))
            if self.cfg.token and str(info.get("token", "")) != self.cfg.token:
                raise ValueError("token 不匹配")
            name = str(info.get("name") or addr[0])
            desk = info.get("desktop") or {}
            desktop = Rect(int(desk.get("x", 0)), int(desk.get("y", 0)),
                           int(desk.get("w", 0)), int(desk.get("h", 0)))
            if desktop.w <= 0 or desktop.h <= 0:
                raise ValueError("客户端没上报屏幕尺寸")

            machine = self._register_client(name, desktop, addr[0])
            sock.sendall(hello_ack(self.cfg.name, self.desktop.as_dict()))
            sock.settimeout(None)
            session_holder: Dict[str, Session] = {}
            link = Link(sock, lambda t, p: self._on_client_frame(session_holder,
                                                                 t, p),
                        log=self.log.debug, name=machine.name,
                        on_close=lambda reason: self._on_link_close(
                            session_holder, reason))
            session = Session(machine, link, desktop, machine.name)
            session_holder["session"] = session
            with self._sessions_lock:
                old = self.sessions.get(machine.name)
                self.sessions[machine.name] = session
            if old is not None:
                self.log.warn("%s 重新连接, 断开旧链路" % machine.name)
                old.link.close("被新连接替换")
            link.start()
            self.log.info("client 已连接: %s 来自 %s, 桌面 %s, 位置 %s"
                          % (machine.name, peer, desktop, machine.rect))
            cur = self._clipboard
            if cur is not None and cur.enabled:
                content = self.backend.clipboard_read(cur.max_image_bytes,
                                                      cur.prefer_image)
                if content is not None:
                    kind, payload = content
                    if kind == "image":
                        try:
                            link.send_frame(clipboard_image(bytes(payload)))
                        except ProtocolError as exc:
                            self.log.warn("首屏图片没发出去: %s" % exc)
                    else:
                        link.send_frame(clipboard_msg(str(payload),
                                                      self.cfg.name))
        except Exception as exc:
            self.log.warn("握手失败(%s): %s" % (peer, exc))
            try:
                sock.sendall(error_msg(str(exc)))
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _register_client(self, name: str, desktop: Rect, host: str) -> Machine:
        """登记/更新一台 client, 必要时自动给它找个位置。"""
        self._sizes.set(name, desktop.w, desktop.h)      # type: ignore[union-attr]
        self.cfg.remember_size(name, desktop.w, desktop.h)
        if name == self.layout.server.name:
            # 重名会非常危险: 布局里按名字查会查到 server 自己, 于是"切到 client"
            # 变成"切到 server", 接管状态和光标位置都会错乱。这里直接改名。
            # (最常见于两台机器 hostname 相同, 或者同机开两个进程做测试)
            new_name = "%s-%s" % (name, host.replace(".", "-") or "client")
            self.log.warn("client 名字「%s」和本机重名, 自动改用「%s」。"
                          "建议在 client 的配置里把 name 改成别的。"
                          % (name, new_name))
            name = new_name
        machine = self.layout.by_name(name)
        if machine is not None and machine.is_server:
            machine = None
        if machine is None:
            machine = self.cfg.build_layout(
                Rect(0, 0, self.desktop.w, self.desktop.h),
                self._sizes.sizes).by_name(name)         # type: ignore[union-attr]
            if machine is not None and machine.is_server:
                machine = None
        if machine is None:
            cfg_entry = self.cfg.client_by_name(name)
            host = cfg_entry.host if cfg_entry else host
            right = max(m.rect.right for m in self.layout.machines)
            machine = make_client(name, right, 0, desktop.w, desktop.h, host=host)
            self.layout.clients.append(machine)
            from .config import ClientEntry
            self.cfg.clients.append(ClientEntry(name=name, host=host,
                                                rect=machine.rect))
            self.log.warn("新机器 %s 未在配置里, 已自动摆到最右侧 %s; "
                          "请运行 crosspc gui 调整相对位置" % (name, machine.rect))
        if machine.rect.w != desktop.w or machine.rect.h != desktop.h:
            self.log.info("%s 的分辨率是 %dx%d(配置里是 %dx%d), 已按实际更新"
                          % (name, desktop.w, desktop.h,
                             machine.rect.w, machine.rect.h))
            machine.rect = Rect(machine.rect.x, machine.rect.y,
                                desktop.w, desktop.h)
        return machine

    # ------------------------------------------------------------ 链路事件
    def _session_for(self, machine: Optional[Machine]) -> Optional[Session]:
        if machine is None:
            return None
        with self._sessions_lock:
            for s in self.sessions.values():
                if s.machine is machine or s.name == machine.name:
                    return s
        return None

    def _on_link_close(self, holder: Dict[str, Session], reason: str) -> None:
        session = holder.get("session")
        if session is None:
            return
        self._drop_session(session, reason)

    def _drop_session(self, session: Session, reason: str) -> None:
        with self._sessions_lock:
            if self.sessions.get(session.name) is not session:
                return
            self.sessions.pop(session.name, None)
        self.log.warn("client %s 断开: %s" % (session.name, reason))
        if self.router and self.router.active is session.machine:
            self.log.warn("断开的是当前被控制的机器, 收回控制权")
            self._force_local("链路断开")
        for probe in (self._panic, self._lock_key):
            if probe:
                probe.reset()

    def _on_client_frame(self, holder: Dict[str, Session], msg_type: int,
                         payload: bytes) -> None:
        session = holder.get("session")
        if session is None:
            return
        if msg_type == T_CLIPBOARD:
            info = parse_json(payload)
            if info.get("origin") == self.cfg.name:
                return
            text = info.get("text")
            if isinstance(text, str) and self._clipboard is not None:
                self._clipboard.apply_remote("text", text)
        elif msg_type == T_CLIPBOARD_IMAGE:
            if self._clipboard is not None and payload:
                self._clipboard.apply_remote("image", payload)
        elif msg_type == T_CONTROL:
            info = parse_json(payload)
            action = info.get("action")
            self.log.info("client %s 请求: %s" % (session.name, action))
            if action == "release" and self.router and \
                    self.router.active is session.machine:
                self._force_local("client 主动放手")
        elif msg_type == T_ERROR:
            info = parse_json(payload)
            self.log.warn("client %s 报错: %s" % (session.name,
                                                 info.get("message")))

    # ------------------------------------------------------------ 输入路由
    def _on_input(self, ev: Event) -> None:
        """在钩子线程上被调用: 必须极快, 不能抛异常, 不能做 IO。"""
        try:
            if ev.kind == MOTION:
                nx, ny = norm_from_desktop(self.desktop, ev.a, ev.b)
                ev = Event.motion(nx, ny, ev.c, ev.d)
            if ev.kind == KEY:
                if self._panic is not None and self._panic.feed(ev):
                    self._panic_action()
                    return
                if self._lock_key is not None and self._lock_key.feed(ev):
                    self._toggle_lock()
                    return
            if self.cfg.debug_events:
                self.log.event(ev.describe())
            actions = self.router.on_event(ev)          # type: ignore[union-attr]
            if actions:
                self._apply(actions)
        except Exception as exc:
            self._input_errors += 1
            if self._input_errors <= 3:
                self.log.error("路由输入出错(第 %d 次): %s"
                               % (self._input_errors, exc))

    def _panic_action(self) -> None:
        self.log.warn("!! 紧急热键: 收回控制权")
        self._force_local("紧急热键")

    def _toggle_lock(self) -> None:
        if self.router is None:
            return
        if self.router.remote:
            self.router.set_locked(not self.router.locked)
        else:
            self.log.info("当前在本机, 锁定热键仅在控制远端时有效")

    def _apply(self, actions: List[Action]) -> None:
        """执行 Router 给出的动作(可能被钩子线程或链路线程调用)。"""
        for act in actions:
            try:
                if act.kind == "local":
                    self.backend.set_forwarding(False)
                    ax, ay = desktop_from_norm(self.desktop, act.x, act.y)
                    self.backend.set_cursor(ax, ay)
                elif act.kind == "enter":
                    session = self._session_for(act.machine)
                    if session is None or not session.alive:
                        self.log.warn("想切到 %s 但它没连着, 留在本机"
                                      % (act.machine.name if act.machine else "?"))
                        self._force_local("目标离线")
                        return
                    park = self.backend.cursor()
                    self.backend.set_park_point(*park)
                    self.backend.set_forwarding(True)
                    session.link.send_event(Event.motion(act.x, act.y))
                    self.log.info("鼠标进入 %s (本地 %d,%d), 停靠点 %d,%d"
                                  % (act.machine.name, act.x, act.y, park[0], park[1]))
                elif act.kind == "leave":
                    session = self._session_for(act.machine)
                    if session is not None and act.events:
                        session.link.send_events(act.events)
                elif act.kind == "remote":
                    session = self._session_for(act.machine)
                    if session is None or not session.alive:
                        self.log.warn("%s 已离线, 收回控制权" % act.machine.name)
                        self._force_local("目标离线")
                        return
                    session.link.send_events(act.events)
            except BackendError as exc:
                # 后端拒绝接管: 立刻回到本机, 绝不能把用户卡死
                self.log.error("后端错误, 收回控制权: %s" % exc)
                try:
                    self._force_local("后端错误")
                except Exception:
                    pass

    def _force_local(self, reason: str) -> None:
        """把控制权收回本机。

        Router 用的是"左上角归一到 0,0"的坐标, 而后端给的是真实桌面坐标
        (副屏在主屏左侧时会是负数), 所以这里必须先归一化再交给 Router。
        """
        if self.router is None:
            return
        try:
            park = norm_from_desktop(self.desktop, *self.backend.cursor())
        except Exception:
            park = None
        self._apply(self.router.force_local(reason, park))

    # ------------------------------------------------------------ 剪辑板
    def _broadcast_clipboard(self, kind: str, payload: object) -> None:
        if kind == "image":
            try:
                msg = clipboard_image(bytes(payload))   # type: ignore[arg-type]
            except ProtocolError as exc:
                self.log.warn("图片过大, 没发出去: %s" % exc)
                return
        else:
            msg = clipboard_msg(str(payload), self.cfg.name)
        with self._sessions_lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            s.link.send_frame(msg)


# ------------------------------------------------------------------ 小工具
def _local_ips() -> List[str]:
    from .net import local_ipv4_addresses
    return local_ipv4_addresses()


def _read_one_frame(sock: socket.socket):
    """握手期间同步读一个完整帧。"""
    from .protocol import FrameReader, ProtocolError
    reader = FrameReader()
    while True:
        try:
            data = sock.recv(65536)
        except (socket.timeout, OSError):
            return None
        if not data:
            return None
        try:
            frames = reader.feed(data)
        except ProtocolError:
            return None
        if frames:
            return frames[0]
