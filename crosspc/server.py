"""server-side application: the machine that captures the keyboard and mouse.

Process structure (thread overview):
  main thread      accepts connections + maintains state + shuts down
  crosspc-hook     low-level keyboard/mouse hook (inside the backend); its only
                   job is to hand events to _on_input
  link-tx-*/link-rx-*  one send/receive thread pair per client TCP link
  clipboard   clipboard polling
  discovery   UDP auto-discovery
  crosspc-watchdog hook liveness watchdog inside the backend

Safety baseline (the user's keyboard and mouse must keep working no matter what
goes wrong):
  1. takeover mode is entered only while "some client is actually connected";
  2. client link drops / heartbeat times out -> force_local immediately, take
     control back;
  3. hook thread dies -> the backend watchdog restores local input;
  4. Ctrl+Alt+F12 (configurable) takes control back unconditionally.
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
    """A connected (or currently handshaking) client."""

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
        return "%s(desktop %s)" % (self.name, self.desktop)


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

    # ------------------------------------------------------------ startup
    def run(self) -> int:
        self._prepare()
        self._listener = make_listener(self.bind, self.port)
        self._listener.settimeout(0.5)
        self.log.info("CrossPC server %s is ready, listening on %s:%d"
                      % (__version__, self.bind, self.port))
        self.log.info("Virtual desktop:\n%s" % self.layout.describe())
        for ip in _local_ips():
            self.log.info("LAN address: %s:%d (use this on the client)"
                          % (ip, self.port))
        if self.layout.clients:
            names = ", ".join(m.name for m in self.layout.clients)
            self.log.info("Waiting for client connections: %s" % names)
        else:
            self.log.info("No client in the config yet: any machine that knows "
                          "the port will register itself on connect")
        if not self.cfg.token:
            self.log.warn("No token configured, so any machine on the LAN can "
                          "connect (acceptable at home, set a token on public "
                          "networks)")

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
            self.log.info("Received Ctrl+C, exiting")
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
            self.log.info("The local virtual desktop's top-left corner is "
                          "%d,%d, so internal coordinates were normalized"
                          % (self.desktop.x, self.desktop.y))
        self.router = Router(self.layout, self.log.debug)
        self._panic, self._lock_key = make_hotkeys(self.cfg.hotkey_panic,
                                                   self.cfg.hotkey_lock)
        if self.dry_run:
            self.log.warn("--dry-run: not installing the keyboard/mouse hooks, "
                          "only verifying network/handshake/clipboard")
            return
        self.backend.start_capture(self._on_input)
        self.log.info("Keyboard/mouse capture started (%s)" % self.backend.caps())

    # ------------------------------------------------------------ main loop
    def _loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()
            except socket.timeout:
                # Nothing that goes wrong in the periodic tasks may kill the
                # main loop: this process is also the "guardian" of the user's
                # keyboard and mouse, and if it dies the user has to reboot to
                # get a usable pointer back.
                try:
                    self._tick()
                except Exception as exc:
                    self._tick_errors += 1
                    if self._tick_errors <= 3:
                        self.log.error("Periodic task failed (attempt %d): %s"
                                       % (self._tick_errors, exc))
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    self.log.warn("accept failed: %s" % exc)
                continue
            t = threading.Thread(target=self._handshake, args=(sock, addr),
                                 name="handshake-%s" % addr[0], daemon=True)
            t.start()

    def _tick(self) -> None:
        now = time.monotonic()
        # Dead-link backstop: in case on_close never ran, catch it again here
        with self._sessions_lock:
            dead = [s for s in self.sessions.values() if not s.alive]
        for s in dead:
            self._drop_session(s, "link closed")
        if self.router and self.router.remote:
            session = self._session_for(self.router.active)
            if session is None or not session.alive:
                self.log.warn("The client being controlled went offline, "
                              "taking control back now")
                self._force_local("client offline")
        if now - self._last_reload_check >= 2.0:
            self._last_reload_check = now
            self._maybe_reload_config()
        if self.stats and now - self._last_stats >= 10.0:
            self._last_stats = now
            self._log_stats()

    def _maybe_reload_config(self) -> None:
        """Reload the layout automatically after the GUI changes the config file.

        Without this, the user would have to restart the server every time they
        drag a machine to a new position -- and restarting the server means the
        clients have to reconnect, which is a poor experience. On reload,
        control is unconditionally taken back to this machine first, so that no
        intermediate state exists where "the cursor cannot be resolved to a
        machine" between the old and the new layout.
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
            self.log.warn("The config file changed but could not be read, "
                          "keeping the old layout: %s" % exc)
            return
        if self.router and self.router.remote:
            self._force_local("config changed")
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
            # Overwrite the guesses in the config with the sizes that already
            # connected clients reported
            with self._sessions_lock:
                for session in self.sessions.values():
                    self._register_client(session.name, session.desktop,
                                          session.machine.host)
        except Exception as exc:
            self.log.warn("Reloading the config failed (keeping the old "
                          "layout): %s" % exc)
            return
        self.log.info("Config reloaded:\n%s" % self.layout.describe())

    def _log_stats(self) -> None:
        with self._sessions_lock:
            parts = []
            for s in self.sessions.values():
                parts.append("%s: sent %d batches/dropped %d"
                             % (s.name, s.link.sent_events, s.link.dropped))
        self.log.info("Status: %s | %s" % (self.router.describe() if self.router
                                           else "not ready",
                                           "; ".join(parts) or "no clients"))

    def shutdown(self) -> None:
        self._stop.set()
        if self.router and self.router.remote:
            self._force_local("server exiting")
        if self._clipboard:
            self._clipboard.stop()
        if self._discovery:
            self._discovery.stop()
        with self._sessions_lock:
            for s in list(self.sessions.values()):
                try:
                    s.link.send_frame(control("bye", reason="server exiting"))
                except Exception:
                    pass
                s.link.close("server exiting")
            self.sessions.clear()
        try:
            self.backend.stop_capture()
        except Exception as exc:
            self.log.debug("Uninstalling capture failed: %s" % exc)
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
                self.log.info("Config updated: %s" % self.cfg.path)
            except Exception as exc:
                self.log.warn("Saving the config failed: %s" % exc)
        self.log.info("server stopped")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ handshake
    def _handshake(self, sock: socket.socket, addr) -> None:
        peer = "%s:%d" % (addr[0], addr[1])
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            reader = _read_one_frame(sock)
            if reader is None:
                self.log.warn("%s handshake timed out/no data" % peer)
                sock.close()
                return
            msg_type, payload = reader
            if msg_type != T_HELLO:
                sock.sendall(error_msg("the first message must be HELLO"))
                sock.close()
                return
            info = parse_json(payload)
            if info.get("magic") != MAGIC:
                raise ValueError("not a CrossPC client")
            if int(info.get("protocol", 0)) != PROTOCOL_VERSION:
                raise ValueError("protocol version mismatch (peer %s, local %d)"
                                 % (info.get("protocol"), PROTOCOL_VERSION))
            if self.cfg.token and str(info.get("token", "")) != self.cfg.token:
                raise ValueError("token mismatch")
            name = str(info.get("name") or addr[0])
            desk = info.get("desktop") or {}
            desktop = Rect(int(desk.get("x", 0)), int(desk.get("y", 0)),
                           int(desk.get("w", 0)), int(desk.get("h", 0)))
            if desktop.w <= 0 or desktop.h <= 0:
                raise ValueError("the client did not report its screen size")

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
                self.log.warn("%s reconnected, closing the old link"
                              % machine.name)
                old.link.close("replaced by a new connection")
            link.start()
            self.log.info("client connected: %s from %s, desktop %s, "
                          "position %s"
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
                            self.log.warn("The initial clipboard image was not "
                                          "sent: %s" % exc)
                    else:
                        link.send_frame(clipboard_msg(str(payload),
                                                      self.cfg.name))
        except Exception as exc:
            self.log.warn("Handshake failed (%s): %s" % (peer, exc))
            try:
                sock.sendall(error_msg(str(exc)))
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _register_client(self, name: str, desktop: Rect, host: str) -> Machine:
        """Register/update a client, giving it a position automatically if needed."""
        self._sizes.set(name, desktop.w, desktop.h)      # type: ignore[union-attr]
        self.cfg.remember_size(name, desktop.w, desktop.h)
        if name == self.layout.server.name:
            # A duplicate name is very dangerous: a lookup by name in the layout
            # finds the server itself, so "switch to the client" turns into
            # "switch to the server" and both the takeover state and the cursor
            # position get confused. Rename it outright here.
            # (Most common with two machines that share a hostname, or two
            # processes on one machine when testing.)
            new_name = "%s-%s" % (name, host.replace(".", "-") or "client")
            self.log.warn("The client name \"%s\" duplicates this machine's "
                          "name, automatically using \"%s\" instead. Consider "
                          "changing name in the client's config."
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
            self.log.warn("The new machine %s is not in the config, it was "
                          "placed automatically at the far right %s; run "
                          "crosspc gui to adjust the relative position"
                          % (name, machine.rect))
        if machine.rect.w != desktop.w or machine.rect.h != desktop.h:
            self.log.info("%s's screen resolution is %dx%d (the config says "
                          "%dx%d), updated to match reality"
                          % (name, desktop.w, desktop.h,
                             machine.rect.w, machine.rect.h))
            machine.rect = Rect(machine.rect.x, machine.rect.y,
                                desktop.w, desktop.h)
        return machine

    # ------------------------------------------------------------ link events
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
        self.log.warn("client %s disconnected: %s" % (session.name, reason))
        if self.router and self.router.active is session.machine:
            self.log.warn("The machine being controlled disconnected, taking "
                          "control back")
            self._force_local("link closed")
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
            self.log.info("client %s requested: %s" % (session.name, action))
            if action == "release" and self.router and \
                    self.router.active is session.machine:
                self._force_local("client released control on its own")
        elif msg_type == T_ERROR:
            info = parse_json(payload)
            self.log.warn("client %s reported an error: %s"
                          % (session.name, info.get("message")))

    # ------------------------------------------------------------ input routing
    def _on_input(self, ev: Event) -> None:
        """Called on the hook thread: must be very fast, must not raise, and
        must not do any IO."""
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
                self.log.error("Routing input failed (attempt %d): %s"
                               % (self._input_errors, exc))

    def _panic_action(self) -> None:
        self.log.warn("!! panic hotkey: taking control back")
        self._force_local("panic hotkey")

    def _toggle_lock(self) -> None:
        if self.router is None:
            return
        if self.router.remote:
            self.router.set_locked(not self.router.locked)
        else:
            self.log.info("Control is local right now, the lock hotkey only "
                          "works while controlling a remote machine")

    def _apply(self, actions: List[Action]) -> None:
        """Execute the actions given by the Router (may be called by the hook
        thread or by a link thread)."""
        for act in actions:
            try:
                if act.kind == "local":
                    self.backend.set_forwarding(False)
                    ax, ay = desktop_from_norm(self.desktop, act.x, act.y)
                    self.backend.set_cursor(ax, ay)
                elif act.kind == "enter":
                    session = self._session_for(act.machine)
                    if session is None or not session.alive:
                        self.log.warn("Wanted to switch to %s but it is not "
                                      "connected, staying local"
                                      % (act.machine.name if act.machine else "?"))
                        self._force_local("target offline")
                        return
                    park = self.backend.cursor()
                    self.backend.set_park_point(*park)
                    self.backend.set_forwarding(True)
                    session.link.send_event(Event.motion(act.x, act.y))
                    self.log.info("Mouse entered %s (local %d,%d), park point "
                                  "%d,%d"
                                  % (act.machine.name, act.x, act.y, park[0], park[1]))
                elif act.kind == "leave":
                    session = self._session_for(act.machine)
                    if session is not None and act.events:
                        session.link.send_events(act.events)
                elif act.kind == "remote":
                    session = self._session_for(act.machine)
                    if session is None or not session.alive:
                        self.log.warn("%s went offline, taking control back"
                                      % act.machine.name)
                        self._force_local("target offline")
                        return
                    session.link.send_events(act.events)
            except BackendError as exc:
                # The backend refused takeover: go back to this machine at once,
                # never leave the user stuck
                self.log.error("Backend error, taking control back: %s" % exc)
                try:
                    self._force_local("backend error")
                except Exception:
                    pass

    def _force_local(self, reason: str) -> None:
        """Take control back to this machine.

        The Router uses coordinates normalized so that the top-left corner is
        0,0, whereas the backend reports real desktop coordinates (negative when
        a secondary monitor sits to the left of the primary one), so everything
        must be normalized here before it reaches the Router.
        """
        if self.router is None:
            return
        try:
            park = norm_from_desktop(self.desktop, *self.backend.cursor())
        except Exception:
            park = None
        self._apply(self.router.force_local(reason, park))

    # ------------------------------------------------------------ clipboard
    def _broadcast_clipboard(self, kind: str, payload: object) -> None:
        if kind == "image":
            try:
                msg = clipboard_image(bytes(payload))   # type: ignore[arg-type]
            except ProtocolError as exc:
                self.log.warn("Image too large, not sent: %s" % exc)
                return
        else:
            msg = clipboard_msg(str(payload), self.cfg.name)
        with self._sessions_lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            s.link.send_frame(msg)


# ------------------------------------------------------------------ helpers
def _local_ips() -> List[str]:
    from .net import local_ipv4_addresses
    return local_ipv4_addresses()


def _read_one_frame(sock: socket.socket):
    """Read one complete frame synchronously during the handshake."""
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
