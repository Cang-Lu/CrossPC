"""client 端应用: 没有键鼠的那台机器。

职责只有三件事:
  1. 连上 server(手写地址, 或者用 UDP 自动发现);
  2. 收到输入事件就注入本机(X11 用 XTest, Wayland/无 X 时用 uinput);
  3. 双向同步剪辑板。

断开连接时必须 release_all(): 否则远端松开鼠标的那一刻链路刚好断了, 本机
就会留下一个"一直按着 Ctrl"的状态, 那比连不上更难受。
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

    # ------------------------------------------------------------ 启动
    def run(self) -> int:
        # Linux+uinput 走的是"绝对定位设备", 必须知道本机分辨率。后端统一从
        # 环境变量 CROSSPC_SCREEN 读取, 所以这里把配置里的 screen 落到环境变量
        # (显式设置的 CROSSPC_SCREEN 优先, 便于临时覆盖)。
        if self.cfg.screen and not os.environ.get("CROSSPC_SCREEN"):
            os.environ["CROSSPC_SCREEN"] = "%dx%d" % self.cfg.screen
            self.log.info("按配置指定本机屏幕为 %dx%d"
                          % self.cfg.screen)
        self.backend.prepare()
        if not self.backend.supports_inject:
            raise BackendError("%s 后端不支持注入输入, 不能当 client"
                               % self.backend.name)
        self.desktop = self.backend.desktop_rect()
        self.log.info("CrossPC client %s 启动, 本机桌面 %s, 注入方式 %s"
                      % (__version__, self.desktop, self.backend.caps()))
        self.log.info("本机名字: %s(server 配置里 clients[].name 要和它一致)"
                      % self.cfg.name)
        if self.desktop.w <= 0 or self.desktop.h <= 0:
            raise BackendError("拿不到本机屏幕尺寸, 请在环境变量 CROSSPC_SCREEN "
                               "里写明, 例如 CROSSPC_SCREEN=2560x1440")
        try:
            self._connect_loop()
        except KeyboardInterrupt:
            self.log.info("收到 Ctrl+C, 正在退出")
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        self._stop.set()
        if self._clipboard:
            self._clipboard.stop()
        if self._link:
            self._link.close("client 退出")
            self._link = None
        try:
            self.backend.release_all()
        except Exception:
            pass
        try:
            self.backend.close()
        except Exception:
            pass
        self.log.info("client 已停止")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 连接
    def _connect_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            host, port = self._target()
            if host is None:
                if self.once:
                    self.log.error("没有发现 server, 退出")
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
                self.log.warn("连接 %s:%d 失败: %s" % (host, port, exc))
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
        self.log.info("没写 server 地址, 用 UDP 自动发现(%d 端口)..."
                      % self.cfg.discovery_port)
        found = discover(3.0, self.cfg.discovery_port)
        if not found:
            self.log.warn("没发现 server。检查: 1) server 是否已启动 "
                          "2) 防火墙是否放行 UDP %d 3) 或者直接用 --host 指定 IP"
                          % self.cfg.discovery_port)
            return None, self.port
        best = found[0]
        host = best.get("address") or best.get("host")
        port = int(best.get("port") or self.port)
        self.log.info("发现 server 「%s」 在 %s:%d"
                      % (best.get("name", "?"), host, port))
        self.host = host
        self.port = port
        return host, port

    def _session(self, host: str, port: int) -> None:
        self.log.info("正在连接 %s:%d ..." % (host, port))
        sock = make_socket(host, port, timeout=HANDSHAKE_TIMEOUT)
        try:
            sock.sendall(hello(self.cfg.name, self.cfg.token,
                               self.desktop.as_dict(),
                               [m.as_dict() for m in self.backend.monitors()],
                               __version__))
            reply = _read_one_frame(sock)
            if reply is None:
                raise RuntimeError("握手超时: server 没有回应")
            msg_type, payload = reply
            info = parse_json(payload)
            if msg_type == T_ERROR or not info.get("ok", msg_type == T_HELLO_ACK):
                raise RuntimeError("server 拒绝: %s" % info.get("message")
                                   or info.get("reason") or "未知原因")
            if msg_type != T_HELLO_ACK:
                raise RuntimeError("握手消息类型不对: %s" % msg_type)
            if int(info.get("protocol", 0)) != PROTOCOL_VERSION:
                raise RuntimeError("协议版本不一致")
            server_name = info.get("name")
            self.log.info("已连接到 server 「%s」, 桌面 %s"
                          % (server_name, info.get("desktop")))
            if server_name and server_name == self.cfg.name:
                self.log.warn("本机名字和 server 一样(都是「%s」), server 会自动给"
                              "本机改名; 建议在配置里设置 \"name\" 区分开"
                              % self.cfg.name)
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

        self.log.info("就绪: 把 server 上的鼠标移到本机所在的屏幕边缘即可")
        try:
            while not self._stop.is_set() and link.alive:
                time.sleep(0.5)
                if self._link is None:
                    break
        finally:
            if self._clipboard:
                self._clipboard.stop()
                self._clipboard = None
            link.close("会话结束")
            self._link = None

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # ------------------------------------------------------------ 收到输入
    def _on_frame(self, msg_type: int, payload: bytes) -> None:
        if msg_type == T_INPUT:
            try:
                events = decode_input(payload)
            except Exception as exc:
                self.log.warn("输入帧解析失败: %s" % exc)
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
                self.log.info("server 说再见: %s" % info.get("reason", ""))
        elif msg_type == T_ERROR:
            info = parse_json(payload)
            self.log.warn("server 报错: %s" % info.get("message"))

    def _inject(self, events) -> None:
        inject = self.backend.inject
        for ev in events:
            if ev.kind == MOTION:
                x, y = desktop_from_norm(self.desktop, ev.a, ev.b)
                ev = Event.motion(x, y)
            try:
                inject(ev)
            except BackendError as exc:
                self.log.error("注入失败: %s" % exc)
                return
            except Exception as exc:
                self.log.warn("注入事件出错(%s): %s" % (ev.describe(), exc))
                return
            self._injected += 1
        if self.cfg.debug_events and events:
            self.log.event("注入 %d 个事件, 累计 %d" % (len(events), self._injected))

    def _on_close(self, reason: str) -> None:
        self.log.warn("与 server 的连接断开: %s" % reason)
        try:
            self.backend.release_all()      # 防粘键, 见模块文档
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
                self.log.warn("图片过大, 没发出去: %s" % exc)
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
