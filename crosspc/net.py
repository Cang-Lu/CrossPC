"""Network layer: TCP link (automatic framing / batched sending / heartbeat) + UDP discovery.

Design points:
* Input events must be sent in **batches**: at 1000Hz for the mouse, one send per
  tiny packet both burns CPU and gets dragged down by Nagle, so the send thread
  "gathers the events already sitting in the queue into one batch and sends them
  at once" and flushes immediately once the queue runs empty, balancing
  throughput against latency; TCP_NODELAY turns Nagle off.
* Sending never blocks the caller: when the queue is full it is better to drop
  events than to stall the Windows hook thread (a stalled hook thread = the
  user's keyboard and mouse stop responding).
* Heartbeat: one PING per second; if nothing at all is received for 5 seconds the
  link is declared down and the layer above immediately takes control back to
  the local machine -- the user must never be left staring at a computer that
  does not respond.
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

#: Most input events that fit in one batch
MAX_BATCH = 512
#: Send queue length (drop when full, so the hook thread is never dragged down)
TX_QUEUE_SIZE = 2048
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_TIMEOUT = 5.0


class Link:
    """One established TCP link (shared by the server and the client)."""

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

    # ------------------------------------------------------------ lifecycle
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
                self._on_close(reason or "connection closed")
            except Exception as exc:            # pragma: no cover
                self._log("close callback raised: %s" % exc)

    # ------------------------------------------------------------ sending
    def send_frame(self, data: bytes) -> None:
        """Send one complete, already assembled frame (which is what the *_ functions in the protocol module return)."""
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
            # Dropping events beats blocking the hook thread; count them and
            # mention it now and then
            self.dropped += 1
            if self.dropped in (1, 100, 1000):
                self._log("send queue full, dropped %d events (network or peer "
                          "too slow)" % self.dropped)

    def _write(self, data: bytes) -> bool:
        try:
            self.sock.sendall(data)
            self._last_tx = time.monotonic()
            return True
        except OSError as exc:
            self.close("send failed: %s" % exc)
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
                # Gather the input events already in the queue into one batch
                # (fewer sends), but a control frame must be flushed first,
                # otherwise the control message would cut in ahead of the input
                # events.
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
            self._log("input encoding failed: %s" % exc)
            return
        if self._write(data):
            self.sent_events += len(events)

    def _idle_tick(self) -> None:
        now = time.monotonic()
        if now - self._last_tx >= self._hb:
            self._write(make_ping(0))
        if now - self._last_rx > self._timeout:
            self.close("heartbeat timeout (no response from the peer for %.0f "
                       "seconds)" % self._timeout)

    # ------------------------------------------------------------ receiving
    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.sock.recv(65536)
            except OSError as exc:
                if not self._stop.is_set():
                    self.close("receive failed: %s" % exc)
                return
            if not data:
                self.close("the peer closed the connection")
                return
            self._last_rx = time.monotonic()
            try:
                frames = self._reader.feed(data)
            except ProtocolError as exc:
                self.close("protocol error: %s" % exc)
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
                    self._log("error handling %s message: %s"
                              % (TYPE_NAMES.get(msg_type, msg_type), exc))


# ------------------------------------------------------------------ transport factories
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


# ------------------------------------------------------------------ automatic discovery
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
    """UDP discovery: the server answers probe packets while broadcasting its own
    presence.

    UDP broadcast is usually fine on a LAN, but corporate networks and firewalls
    may block it, so a hand-written IP in the config file is always the reliable
    fallback.
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
            self._log("UDP discovery unavailable (%s); write the peer's IP into "
                      "the config by hand" % exc)
            return
        self._sock = sock
        self._thread = threading.Thread(target=self._loop, name="discovery",
                                        daemon=True)
        self._thread.start()
        self._log("discovery started (UDP %d): clients on the LAN can find this "
                  "machine automatically" % self.discovery_port)

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
    """Actively look for a server: broadcast a probe packet and collect the replies."""
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
    """All local non-loopback IPv4 addresses (to show the user "which address to enter on the other machine")."""
    out: List[str] = []

    def add(ip: str) -> None:
        if ip and not ip.startswith("127.") and ip not in out:
            out.append(ip)

    # This UDP "connect" sends no packet at all; it only makes the kernel pick
    # the default outbound address
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
