"""CrossPC wire protocol: framing + message encoding/decoding.

Frame format (everything big-endian):
    +--------+--------+------------------+
    | len:u32| type:u8|   payload: len   |
    +--------+--------+------------------+

* The payload of control messages (HELLO/CLIPBOARD/CONTROL/PING) is UTF-8 JSON,
  which makes them easier to debug;
* input events travel in T_INPUT, whose payload is compact binary: at 1000Hz
  for the mouse we should not be wasting bandwidth and CPU on JSON.

Input batch payload:
    u16 count, then count records, each = u8 kind + fixed-size fields
      EV_MOTION  >hh   x, y       (client local coordinates, absolute)
      EV_BUTTON  >BB   button, pressed
      EV_WHEEL   >hh   dx, dy     (notches)
      EV_KEY     >HHB  scancode, vk, flags (bit0=pressed, bit1=E0 extended)
"""
from __future__ import annotations

import json
import struct
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import PROTOCOL_VERSION
from .events import BUTTON, KEY, MOTION, WHEEL, Event

MAGIC = "CrossPC"

# ------------------------------------------------------------------ frame types
T_HELLO = 1
T_HELLO_ACK = 2
T_ERROR = 3
T_INPUT = 4
T_CLIPBOARD = 5
T_CONTROL = 6
T_PING = 7
T_PONG = 8
#: Image clipboard: the payload is the raw PNG bytes (no JSON, so base64 cannot
#: inflate it by 33% for nothing)
T_CLIPBOARD_IMAGE = 9

FRAME_HEADER = struct.Struct(">IB")
_INPUT_COUNT = struct.Struct(">H")
_EV_MOTION = struct.Struct(">hh")
_EV_BUTTON = struct.Struct(">BB")
_EV_WHEEL = struct.Struct(">hh")
_EV_KEY = struct.Struct(">HHB")

EV_MOTION = 1
EV_BUTTON = 2
EV_WHEEL = 3
EV_KEY = 4

#: Per-frame limit, a defensive check (a malicious or garbled length field must
#: not make us allocate several gigabytes)
MAX_FRAME = 8 * 1024 * 1024
#: Clipboard text limit
MAX_CLIPBOARD = 1 * 1024 * 1024
#: Clipboard image limit (bytes of PNG in one frame)
MAX_IMAGE = 8 * 1024 * 1024
#: Valid range for one motion coordinate (transported as int16)
_COORD_MIN, _COORD_MAX = -32768, 32767

TYPE_NAMES = {
    T_HELLO: "HELLO", T_HELLO_ACK: "HELLO_ACK", T_ERROR: "ERROR",
    T_INPUT: "INPUT", T_CLIPBOARD: "CLIPBOARD", T_CONTROL: "CONTROL",
    T_PING: "PING", T_PONG: "PONG", T_CLIPBOARD_IMAGE: "CLIPBOARD_IMAGE",
}


class ProtocolError(RuntimeError):
    pass


# ------------------------------------------------------------------ framing
def frame(msg_type: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_FRAME:
        raise ProtocolError("message too long: %d bytes" % len(payload))
    return FRAME_HEADER.pack(len(payload), msg_type) + payload


def frame_json(msg_type: int, obj: Dict[str, Any]) -> bytes:
    return frame(msg_type, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


class FrameReader:
    """Incremental de-framing: takes the TCP byte stream and yields (type, payload)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        self._buf += data
        out: List[Tuple[int, bytes]] = []
        while True:
            if len(self._buf) < FRAME_HEADER.size:
                break
            length, msg_type = FRAME_HEADER.unpack_from(self._buf, 0)
            if length > MAX_FRAME:
                raise ProtocolError("bad frame length: %d" % length)
            end = FRAME_HEADER.size + length
            if len(self._buf) < end:
                break
            out.append((msg_type, bytes(self._buf[FRAME_HEADER.size:end])))
            del self._buf[:end]
        return out

    def pending(self) -> int:
        return len(self._buf)


def parse_json(payload: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise ProtocolError("JSON parse failed: %s" % exc) from exc
    if not isinstance(obj, dict):
        raise ProtocolError("message body must be an object")
    return obj


# ------------------------------------------------------------------ input batch
def encode_input(events: Iterable[Event]) -> bytes:
    body = bytearray()
    count = 0
    for ev in events:
        k = ev.kind
        if k == MOTION:
            x = min(max(int(ev.a), _COORD_MIN), _COORD_MAX)
            y = min(max(int(ev.b), _COORD_MIN), _COORD_MAX)
            body += bytes((EV_MOTION,)) + _EV_MOTION.pack(x, y)
        elif k == BUTTON:
            body += bytes((EV_BUTTON,)) + _EV_BUTTON.pack(int(ev.a) & 0xFF,
                                                        1 if ev.b else 0)
        elif k == WHEEL:
            body += bytes((EV_WHEEL,)) + _EV_WHEEL.pack(
                min(max(int(ev.a), -32768), 32767),
                min(max(int(ev.b), -32768), 32767))
        elif k == KEY:
            flags = (1 if ev.c else 0) | (2 if ev.d else 0)
            body += bytes((EV_KEY,)) + _EV_KEY.pack(int(ev.a) & 0xFFFF,
                                                    int(ev.b) & 0xFFFF, flags)
        else:
            continue
        count += 1
    return _INPUT_COUNT.pack(count) + bytes(body)


def decode_input(payload: bytes) -> List[Event]:
    if len(payload) < _INPUT_COUNT.size:
        raise ProtocolError("input frame too short")
    (count,) = _INPUT_COUNT.unpack_from(payload, 0)
    off = _INPUT_COUNT.size
    out: List[Event] = []
    for _ in range(count):
        if off >= len(payload):
            raise ProtocolError("input frame truncated (missing kind)")
        kind = payload[off]
        off += 1
        # On a wrong length struct raises struct.error, which from the upper
        # layer's point of view also means "the protocol is broken", so unify it
        # into ProtocolError and spare the caller from catching two exception
        # types
        try:
            if kind == EV_MOTION:
                x, y = _EV_MOTION.unpack_from(payload, off)
                off += _EV_MOTION.size
                out.append(Event.motion(x, y))
            elif kind == EV_BUTTON:
                btn, pressed = _EV_BUTTON.unpack_from(payload, off)
                off += _EV_BUTTON.size
                out.append(Event.button(btn, bool(pressed)))
            elif kind == EV_WHEEL:
                dx, dy = _EV_WHEEL.unpack_from(payload, off)
                off += _EV_WHEEL.size
                out.append(Event.wheel(dx, dy))
            elif kind == EV_KEY:
                scan, vk, flags = _EV_KEY.unpack_from(payload, off)
                off += _EV_KEY.size
                out.append(Event.key(scan, vk, bool(flags & 1), bool(flags & 2)))
            else:
                raise ProtocolError("unknown input event kind %d" % kind)
        except struct.error as exc:
            raise ProtocolError("incomplete input frame data: %s" % exc) from exc
    return out


# ------------------------------------------------------------------ control message builders
def hello(name: str, token: str, desktop: Dict[str, int],
          monitors: Optional[List[Dict[str, int]]] = None,
          version: str = "") -> bytes:
    return frame_json(T_HELLO, {
        "magic": MAGIC, "protocol": PROTOCOL_VERSION, "app": version,
        "name": name, "token": token, "desktop": desktop,
        "monitors": monitors or [],
    })


def hello_ack(name: str, desktop: Dict[str, int], ok: bool = True,
              reason: str = "") -> bytes:
    return frame_json(T_HELLO_ACK, {
        "magic": MAGIC, "protocol": PROTOCOL_VERSION, "ok": ok,
        "name": name, "desktop": desktop, "reason": reason,
    })


def error(msg: str) -> bytes:
    return frame_json(T_ERROR, {"message": msg})


def clipboard_text(text: str, origin: str = "") -> bytes:
    return frame_json(T_CLIPBOARD, {"kind": "text", "text": text,
                                    "origin": origin})


def clipboard_image(png: bytes) -> bytes:
    """Image clipboard frame: the payload holds the PNG bytes directly.

    The image carries no origin field -- loop prevention relies on the content
    hash (the layer above already deduplicates by hash), so it is not needed,
    while putting base64 into JSON would inflate the size by a third for nothing.
    """
    if len(png) > MAX_IMAGE:
        raise ProtocolError("image too large: %d bytes (limit %d)"
                            % (len(png), MAX_IMAGE))
    return frame(T_CLIPBOARD_IMAGE, png)


def control(action: str, **kw: Any) -> bytes:
    body = {"action": action}
    body.update(kw)
    return frame_json(T_CONTROL, body)


def ping(seq: int = 0) -> bytes:
    return frame_json(T_PING, {"seq": seq})


def pong(seq: int = 0) -> bytes:
    return frame_json(T_PONG, {"seq": seq})
