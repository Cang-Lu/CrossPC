"""client-side application: the machine without the keyboard and mouse.

It has only three responsibilities:
  1. connect to the server (address given by hand, or found via UDP
     auto-discovery);
  2. inject incoming input events locally (XTest under X11, uinput under
     Wayland or when X is absent);
  3. synchronize the clipboard in both directions.

release_all() must run whenever the connection drops: otherwise, if the link
happens to break at the very moment the remote side releases a mouse button,
this machine is left in a "Ctrl is still held down" state, which is worse than
simply failing to connect.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Optional, Tuple

from . import __version__
from .backend.base import Backend, BackendError
from .clipboard import ClipboardSync
from .config import Config
from .events import MOTION, Event
from .layout import Rect
from .net import Link, discover, make_socket
from .protocol import (MAGIC, PROTOCOL_VERSION, T_CLIPBOARD,
                       T_CLIPBOARD_IMAGE, T_CONTROL, T_ERROR, T_HELLO_ACK,
                       T_INPUT, ProtocolError, clipboard_image,
                       clipboard_text as clipboard_msg, decode_input, hello,
                       parse_json)
from .util import Log, desktop_from_norm

HANDSHAKE_TIMEOUT = 8.0
RECONNECT_STEPS = (1.0, 2.0, 3.0, 5.0, 8.0, 10.0)


class ClientApp:
    def __init__(self, cfg: Config, backend: Backend, log: Log,
                 host: Optional[str] = None, port: Optional[int] = None,
                 once: bool = False, no_clipboard: bool = False):
        self.cfg = cfg
        self.backend = backend
        self.log = log
        self.host = host or cfg.server_host
        self.port = int(port or cfg.port)
        self.once = once
        self.desktop = Rect()
        self._stop = threading.Event()
        self._link: Optional[Link] = None
        self._clipboard: Optional[ClipboardSync] = None
        self._no_clipboard = no_clipboard
        self._injected = 0
        self._last_stats = time.monotonic()

    # ------------------------------------------------------------ startup
    def run(self) -> int:
        # Linux+uinput uses an "absolute positioning device", so it must know
        # this machine's screen resolution. The backend uniformly reads it from
        # the CROSSPC_SCREEN environment variable, so the screen from the config
        # is pushed into the environment here (an explicitly set CROSSPC_SCREEN
        # wins, which makes temporary overrides easy).
        if self.cfg.screen and not os.environ.get("CROSSPC_SCREEN"):
            os.environ["CROSSPC_SCREEN"] = "%dx%d" % self.cfg.screen
            self.log.info("Using the screen size from the config: %dx%d"
                          % self.cfg.screen)
        self.backend.prepare()
        if not self.backend.supports_inject:
            raise BackendError("%s backend does not support injecting input, "
                               "it cannot act as a client"
                               % self.backend.name)
        self.desktop = self.backend.desktop_rect()
        self.log.info("CrossPC client %s starting, local desktop %s, "
                      "injection method %s"
                      % (__version__, self.desktop, self.backend.caps()))
        self.log.info("Local name: %s (clients[].name in the server config "
                      "must match it)" % self.cfg.name)
        if self.desktop.w <= 0 or self.desktop.h <= 0:
            raise BackendError("Could not obtain the local screen size, please "
                               "set it in the CROSSPC_SCREEN environment "
                               "variable, e.g. CROSSPC_SCREEN=2560x1440")
        try:
            self._connect_loop()
        except KeyboardInterrupt:
            self.log.info("Received Ctrl+C, exiting")
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        self._stop.set()
        if self._clipboard:
            self._clipboard.stop()
        if self._link:
            self._link.close("client exiting")
            self._link = None
        try:
            self.backend.release_all()
        except Exception:
            pass
        try:
            self.backend.close()
        except Exception:
            pass
        self.log.info("client stopped")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ connecting
    def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            host, port = self._target()
            if host is None:
                if self.once:
                    self.log.error("No server discovered, exiting")
                    return
                self._sleep(RECONNECT_STEPS[min(attempt, len(RECONNECT_STEPS) - 1)])
                attempt += 1
                continue
            try:
                self._session(host, port)
                attempt = 0
            except BackendError:
                raise
            except Exception as exc:
                self.log.warn("Connecting to %s:%d failed: %s"
                              % (host, port, exc))
                if self.once:
                    return
                self._sleep(RECONNECT_STEPS[min(attempt, len(RECONNECT_STEPS) - 1)])
                attempt += 1
            else:
                if self.once:
                    return
                self._sleep(1.0)

    def _target(self) -> Tuple[Optional[str], int]:
        if self.host:
            return self.host, self.port
        self.log.info("No server address configured, falling back to UDP "
                      "auto-discovery (port %d)..." % self.cfg.discovery_port)
        found = discover(3.0, self.cfg.discovery_port)
        if not found:
            self.log.warn("No server discovered. Check: 1) whether the server "
                          "is running 2) whether the firewall allows UDP %d "
                          "3) or pass the IP directly with --host"
                          % self.cfg.discovery_port)
            return None, self.port
        best = found[0]
        host = best.get("address") or best.get("host")
        port = int(best.get("port") or self.port)
        self.log.info("Discovered server \"%s\" at %s:%d"
                      % (best.get("name", "?"), host, port))
        self.host = host
        self.port = port
        return host, port

    def _session(self, host: str, port: int) -> None:
        self.log.info("Connecting to %s:%d ..." % (host, port))
        sock = make_socket(host, port, timeout=HANDSHAKE_TIMEOUT)
        try:
            sock.sendall(hello(self.cfg.name, self.cfg.token,
                               self.desktop.as_dict(),
                               [m.as_dict() for m in self.backend.monitors()],
                               __version__))
            reply = _read_one_frame(sock)
            if reply is None:
                raise RuntimeError("handshake timed out: the server did not "
                                   "respond")
            msg_type, payload = reply
            info = parse_json(payload)
            if msg_type == T_ERROR or not info.get("ok", msg_type == T_HELLO_ACK):
                raise RuntimeError("server refused: %s" % info.get("message")
                                   or info.get("reason") or "unknown reason")
            if msg_type != T_HELLO_ACK:
                raise RuntimeError("wrong handshake message type: %s" % msg_type)
            if int(info.get("protocol", 0)) != PROTOCOL_VERSION:
                raise RuntimeError("protocol version mismatch")
            server_name = info.get("name")
            self.log.info("Connected to server \"%s\", desktop %s"
                          % (server_name, info.get("desktop")))
            if server_name and server_name == self.cfg.name:
                self.log.warn("This machine has the same name as the server "
                              "(both \"%s\"), the server will rename it "
                              "automatically; consider setting a distinct "
                              "\"name\" in the config" % self.cfg.name)
        except Exception:
            sock.close()
            raise
        sock.settimeout(None)
        link = Link(sock, self._on_frame, log=self.log.debug,
                    name="server", on_close=self._on_close)
        self._link = link
        link.start()

        if not self._no_clipboard:
            self._clipboard = ClipboardSync(
                self.backend, self._send_clipboard, self.log,
                poll_ms=self.cfg.clipboard_poll_ms,
                max_bytes=self.cfg.clipboard_max_bytes,
                enabled=self.cfg.clipboard_enabled, name=self.cfg.name,
                max_image_bytes=self.cfg.clipboard_max_image_bytes,
                images=self.cfg.clipboard_images,
                prefer_image=(self.cfg.clipboard_prefer == "image"))
            self._clipboard.start()

        self.log.info("Ready: move the mouse on the server to the screen edge "
                      "where this machine sits")
        try:
            while not self._stop.is_set() and link.alive:
                time.sleep(0.5)
                if self._link is None:
                    break
        finally:
            if self._clipboard:
                self._clipboard.stop()
                self._clipboard = None
            link.close("session ended")
            self._link = None

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # ------------------------------------------------------------ input received
    def _on_frame(self, msg_type: int, payload: bytes) -> None:
        if msg_type == T_INPUT:
            try:
                events = decode_input(payload)
            except Exception as exc:
                self.log.warn("Failed to parse the input frame: %s" % exc)
                return
            self._inject(events)
        elif msg_type == T_CLIPBOARD:
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
            if info.get("action") == "bye":
                self.log.info("server said goodbye: %s" % info.get("reason", ""))
        elif msg_type == T_ERROR:
            info = parse_json(payload)
            self.log.warn("server reported an error: %s" % info.get("message"))

    def _inject(self, events) -> None:
        inject = self.backend.inject
        for ev in events:
            if ev.kind == MOTION:
                x, y = desktop_from_norm(self.desktop, ev.a, ev.b)
                ev = Event.motion(x, y)
            try:
                inject(ev)
            except BackendError as exc:
                self.log.error("Injection failed: %s" % exc)
                return
            except Exception as exc:
                self.log.warn("Injecting an event failed (%s): %s"
                              % (ev.describe(), exc))
                return
            self._injected += 1
        if self.cfg.debug_events and events:
            self.log.event("Injected %d events, %d in total"
                           % (len(events), self._injected))

    def _on_close(self, reason: str) -> None:
        self.log.warn("Connection to the server dropped: %s" % reason)
        try:
            self.backend.release_all()      # stuck-key protection, see module doc
        except Exception:
            pass

    def _send_clipboard(self, kind: str, payload: object) -> None:
        link = self._link
        if link is None or not link.alive:
            return
        if kind == "image":
            try:
                link.send_frame(clipboard_image(bytes(payload)))  # type: ignore
            except ProtocolError as exc:
                self.log.warn("Image too large, not sent: %s" % exc)
        else:
            link.send_frame(clipboard_msg(str(payload), self.cfg.name))


def _read_one_frame(sock: socket.socket):
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
