"""CrossPC 线协议: 分帧 + 消息编解码。

帧格式(全部大端):
    +--------+--------+------------------+
    | len:u32| type:u8|   payload: len   |
    +--------+--------+------------------+

* 控制类消息(HELLO/CLIPBOARD/CONTROL/PING)的 payload 是 UTF-8 JSON, 好调试;
* 输入事件走 T_INPUT, payload 是紧凑二进制: 鼠标 1000Hz 下也不至于把
  带宽和 CPU 浪费在 JSON 上。

输入批次 payload:
    u16 count, 然后 count 条记录, 每条 = u8 kind + 定长字段
      EV_MOTION  >hh   x, y       (client 本地坐标, 绝对定位)
      EV_BUTTON  >BB   button, pressed
      EV_WHEEL   >hh   dx, dy     (格数)
      EV_KEY     >HHB  scancode, vk, flags(bit0=按下, bit1=E0 扩展)
"""
from __future__ import annotations

import json
import struct
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import PROTOCOL_VERSION
from .events import BUTTON, KEY, MOTION, WHEEL, Event

MAGIC = "CrossPC"

# ------------------------------------------------------------------ 帧类型
T_HELLO = 1
T_HELLO_ACK = 2
T_ERROR = 3
T_INPUT = 4
T_CLIPBOARD = 5
T_CONTROL = 6
T_PING = 7
T_PONG = 8
#: 图片剪辑板: payload 就是原始 PNG 字节(不走 JSON, 免得 base64 白涨 33%)
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

#: 单帧上限, 防御性检查(恶意/错乱的长度字段不要让我们分配几个 G)
MAX_FRAME = 8 * 1024 * 1024
#: 剪辑板文本上限
MAX_CLIPBOARD = 1 * 1024 * 1024
#: 剪辑板图片上限(一帧 PNG 的字节数)
MAX_IMAGE = 8 * 1024 * 1024
#: 一组运动坐标的合法范围(int16 传输)
_COORD_MIN, _COORD_MAX = -32768, 32767

TYPE_NAMES = {
    T_HELLO: "HELLO", T_HELLO_ACK: "HELLO_ACK", T_ERROR: "ERROR",
    T_INPUT: "INPUT", T_CLIPBOARD: "CLIPBOARD", T_CONTROL: "CONTROL",
    T_PING: "PING", T_PONG: "PONG", T_CLIPBOARD_IMAGE: "CLIPBOARD_IMAGE",
}


class ProtocolError(RuntimeError):
    pass


# ------------------------------------------------------------------ 分帧
def frame(msg_type: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_FRAME:
        raise ProtocolError("消息过长: %d 字节" % len(payload))
    return FRAME_HEADER.pack(len(payload), msg_type) + payload


def frame_json(msg_type: int, obj: Dict[str, Any]) -> bytes:
    return frame(msg_type, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


class FrameReader:
    """增量拆帧: 收到 TCP 字节流, 吐出 (type, payload)。"""

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
                raise ProtocolError("帧长度异常: %d" % length)
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
        raise ProtocolError("JSON 解析失败: %s" % exc) from exc
    if not isinstance(obj, dict):
        raise ProtocolError("消息体必须是对象")
    return obj


# ------------------------------------------------------------------ 输入批次
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
        raise ProtocolError("输入帧过短")
    (count,) = _INPUT_COUNT.unpack_from(payload, 0)
    off = _INPUT_COUNT.size
    out: List[Event] = []
    for _ in range(count):
        if off >= len(payload):
            raise ProtocolError("输入帧截断(缺 kind)")
        kind = payload[off]
        off += 1
        # 长度不对时 struct 会抛 struct.error, 对上层来说都属于"协议坏了",
        # 统一成 ProtocolError, 免得调用方要 catch 两种异常
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
                raise ProtocolError("未知输入事件类型 %d" % kind)
        except struct.error as exc:
            raise ProtocolError("输入帧数据不完整: %s" % exc) from exc
    return out


# ------------------------------------------------------------------ 控制消息构造
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
    """图片剪辑板帧: payload 直接放 PNG 字节。

    图片不带 origin 字段 —— 防回环靠内容哈希(上层已经按哈希去重), 不需要它,
    而 base64 进 JSON 会让体积白涨三分之一。
    """
    if len(png) > MAX_IMAGE:
        raise ProtocolError("图片过大: %d 字节(上限 %d)" % (len(png), MAX_IMAGE))
    return frame(T_CLIPBOARD_IMAGE, png)


def control(action: str, **kw: Any) -> bytes:
    body = {"action": action}
    body.update(kw)
    return frame_json(T_CONTROL, body)


def ping(seq: int = 0) -> bytes:
    return frame_json(T_PING, {"seq": seq})


def pong(seq: int = 0) -> bytes:
    return frame_json(T_PONG, {"seq": seq})
