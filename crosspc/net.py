"""网络层: TCP 链路(自动分帧/批量发送/心跳) + UDP 自动发现。

设计要点:
* 输入事件必须**批量**发: 鼠标 1000Hz 时一次 send 一个小包既费 CPU 也
  容易被 Nagle 拖慢, 所以发送线程会"把队列里现有的事件凑成一批立刻发",
  队列空了就马上 flush, 兼顾吞吐与延迟; TCP_NODELAY 关掉 Nagle。
* 发送永不阻塞调用方: 队列满了宁可丢事件也不能卡住 Windows 的钩子线程
  (钩子线程被卡住 = 用户键鼠失灵)。
* 心跳: 每秒 PING 一次, 5 秒收不到任何数据就判定断线, 上层据此立刻把
  控制权收回本机 —— 绝不能让用户对着一台没反应的电脑发呆。
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import PROTOCOL_VERSION
from .events import Event
from .protocol import (MAGIC, T_INPUT, T_PING, T_PONG, FrameReader, ProtocolError,
                       TYPE_NAMES, encode_input, frame, frame_json, parse_json,
                       ping as make_ping, pong as make_pong)

LogFn = Callable[[str], None]
FrameFn = Callable[[int, bytes], None]
CloseFn = Callable[[str], None]

#: 一批最多塞多少个输入事件
MAX_BATCH = 512
#: 发送队列长度(满了就丢, 防止拖死钩子线程)
TX_QUEUE_SIZE = 2048
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 5.0


class Link:
    """一条已建立连接的 TCP 链路(server 和 client 共用)。"""

    def __init__(self, sock: socket.socket, on_frame: FrameFn,
                 log: Optional[LogFn] = None, name: str = "",
                 on_close: Optional[CloseFn] = None,
                 heartbeat: float = HEARTBEAT_INTERVAL,
                 timeout: float = HEARTBEAT_TIMEOUT):
        self.sock = sock
        self.name = name or "?"
        self._on_frame = on_frame
        self._on_close = on_close
        self._log = log or (lambda m: None)
        self._hb = heartbeat
        self._timeout = timeout
        self._reader = FrameReader()
        self._tx: "queue.Queue[Optional[Tuple[str, object]]]" = queue.Queue(TX_QUEUE_SIZE)
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._last_rx = time.monotonic()
        self._last_tx = time.monotonic()
        self.dropped = 0
        self.sent_events = 0
        self.recv_events = 0
        self._threads: List[threading.Thread] = []

        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._send_loop, name="link-tx-%s" % self.name,
                             daemon=True),
            threading.Thread(target=self._recv_loop, name="link-rx-%s" % self.name,
                             daemon=True),
        ]
        for t in self._threads:
            t.start()

    @property
    def alive(self) -> bool:
        return not self._closed.is_set()

    def close(self, reason: str = "") -> None:
        first = not self._closed.is_set()
        self._closed.set()
        self._stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self._tx.put_nowait(None)
        except queue.Full:
            pass
        if first and self._on_close is not None:
            try:
                self._on_close(reason or "连接关闭")
            except Exception as exc:            # pragma: no cover
                self._log("关闭回调异常: %s" % exc)

    # ------------------------------------------------------------ 发送
    def send_frame(self, data: bytes) -> None:
        """发一个已经拼好的完整帧(protocol 模块里的 *_ 函数返回的就是它)。"""
        self._enqueue("raw", data)

    def send_json(self, msg_type: int, obj: Dict) -> None:
        self._enqueue("raw", frame_json(msg_type, obj))

    def send_event(self, ev: Event) -> None:
        self._enqueue("input", ev)

    def send_events(self, events: Sequence[Event]) -> None:
        for ev in events:
            self._enqueue("input", ev)

    def _enqueue(self, kind: str, payload: object) -> None:
        if self._closed.is_set():
            return
        try:
            self._tx.put_nowait((kind, payload))
        except queue.Full:
            # 丢事件优于阻塞钩子线程; 计数并偶尔提示
            self.dropped += 1
            if self.dropped in (1, 100, 1000):
                self._log("发送队列已满, 丢弃 %d 个事件(网络或对端太慢)"
                          % self.dropped)

    def _write(self, data: bytes) -> bool:
        try:
            self.sock.sendall(data)
            self._last_tx = time.monotonic()
            return True
        except OSError as exc:
            self.close("发送失败: %s" % exc)
            return False

    def _send_loop(self) -> None:
        batch: List[Event] = []
        while not self._stop.is_set():
            try:
                item = self._tx.get(timeout=0.2)
            except queue.Empty:
                self._idle_tick()
                continue
            if item is None:
                break
            kind, payload = item
            if kind == "input":
                batch.append(payload)                 # type: ignore[arg-type]
                # 把队列里已有的输入事件凑成一批(降低 send 次数), 但遇到
                # 控制帧必须先 flush, 否则控制消息会插到输入事件前面去。
                while len(batch) < MAX_BATCH:
                    try:
                        nk, np_ = self._tx.get_nowait()
                    except queue.Empty:
                        break
                    if nk == "input":
                        batch.append(np_)             # type: ignore[arg-type]
                    else:
                        self._flush(batch)
                        if np_ is None:
                            self._stop.set()
                            break
                        self._write(np_)              # type: ignore[arg-type]
                self._flush(batch)
            elif payload is None:
                break
            else:
                self._write(payload)                  # type: ignore[arg-type]
        self._flush(batch)

    def _flush(self, batch: List[Event]) -> None:
        if not batch:
            return
        events = list(batch)
        batch.clear()
        try:
            data = frame(T_INPUT, encode_input(events))
        except Exception as exc:                  # pragma: no cover
            self._log("输入编码失败: %s" % exc)
            return
        if self._write(data):
            self.sent_events += len(events)

    def _idle_tick(self) -> None:
        now = time.monotonic()
        if now - self._last_tx >= self._hb:
            self._write(make_ping(0))
        if now - self._last_rx > self._timeout:
            self.close("心跳超时(对端 %.0f 秒无响应)" % self._timeout)

    # ------------------------------------------------------------ 接收
    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.sock.recv(65536)
            except OSError as exc:
                if not self._stop.is_set():
                    self.close("接收失败: %s" % exc)
                return
            if not data:
                self.close("对端关闭了连接")
                return
            self._last_rx = time.monotonic()
            try:
                frames = self._reader.feed(data)
            except ProtocolError as exc:
                self.close("协议错误: %s" % exc)
                return
            for msg_type, payload in frames:
                if msg_type == T_PING:
                    self.send_frame(make_pong())
                    continue
                if msg_type == T_PONG:
                    continue
                if msg_type == T_INPUT:
                    self.recv_events += 1
                try:
                    self._on_frame(msg_type, payload)
                except Exception as exc:
                    self._log("处理 %s 消息出错: %s"
                              % (TYPE_NAMES.get(msg_type, msg_type), exc))


# ------------------------------------------------------------------ 传输工厂
def make_socket(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))
    sock.settimeout(None)
    return sock


def make_listener(bind: str, port: int, backlog: int = 8) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind, port))
    sock.listen(backlog)
    return sock


# ------------------------------------------------------------------ 自动发现
DISCOVERY_REQUEST = "CrossPC-DISCOVER-%d" % PROTOCOL_VERSION


def beacon_payload(name: str, port: int, extra: Optional[Dict] = None) -> bytes:
    obj = {"magic": MAGIC, "protocol": PROTOCOL_VERSION, "role": "server",
           "name": name, "port": port}
    if extra:
        obj.update(extra)
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def parse_beacon(data: bytes) -> Optional[Dict]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("magic") != MAGIC:
        return None
    return obj


class Discovery:
    """UDP 自动发现: server 一边应答探测包一边广播自己的存在。

    局域网里 UDP 广播通常没问题, 但企业网络/防火墙可能挡掉, 所以配置文件
    里手写 IP 永远是可靠的兜底方案。
    """

    def __init__(self, name: str, port: int, discovery_port: int,
                 log: Optional[LogFn] = None, broadcast_interval: float = 2.0):
        self.name = name
        self.port = port
        self.discovery_port = discovery_port
        self._log = log or (lambda m: None)
        self._interval = broadcast_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self.peers: Dict[str, Dict] = {}

    def start(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", self.discovery_port))
            sock.settimeout(0.5)
        except OSError as exc:
            self._log("UDP 发现不可用(%s), 请在配置里手写对方 IP" % exc)
            return
        self._sock = sock
        self._thread = threading.Thread(target=self._loop, name="discovery",
                                        daemon=True)
        self._thread.start()
        self._log("自动发现已启动(UDP %d): 局域网内的 client 可以自动找到本机"
                  % self.discovery_port)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _loop(self) -> None:
        payload = beacon_payload(self.name, self.port)
        last_broadcast = 0.0
        assert self._sock is not None
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_broadcast >= self._interval:
                last_broadcast = now
                try:
                    self._sock.sendto(payload, ("255.255.255.255", self.discovery_port))
                except OSError:
                    pass
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            text = data.decode("utf-8", "replace")
            if text.startswith(DISCOVERY_REQUEST):
                try:
                    self._sock.sendto(payload, addr)
                except OSError:
                    pass
            else:
                obj = parse_beacon(data)
                if obj and obj.get("name") != self.name:
                    self.peers[obj.get("name", addr[0])] = obj


def discover(timeout: float = 3.0, discovery_port: int = 39988
             ) -> List[Dict]:
    """主动找 server: 广播探测包, 收集应答。"""
    found: Dict[str, Dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.4)
    try:
        request = ("%s|%d" % (DISCOVERY_REQUEST, PROTOCOL_VERSION)).encode("utf-8")
        try:
            sock.sendto(request, ("255.255.255.255", discovery_port))
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            obj = parse_beacon(data)
            if obj:
                obj["address"] = addr[0]
                found[obj.get("name") or addr[0]] = obj
    finally:
        sock.close()
    return list(found.values())


def local_ipv4_addresses() -> List[str]:
    """本机所有非环回 IPv4(给用户看"该在另一台机器上填哪个地址")。"""
    out: List[str] = []

    def add(ip: str) -> None:
        if ip and not ip.startswith("127.") and ip not in out:
            out.append(ip)

    # 这个 UDP "connect" 不发任何包, 只是让内核选出默认出口地址
    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 53))
                add(s.getsockname()[0])
            finally:
                s.close()
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return out
