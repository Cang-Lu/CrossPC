"""图片编解码: PNG <-> RGBA <-> Windows DIB, 只用标准库(zlib)。

为什么需要它:
    Windows 剪辑板里的图片是 **CF_DIB**(BITMAPINFOHEADER + 像素, 通常自下而上、
    BGR 排列), 而 Linux 那边的 xclip/wl-copy 只认 PNG。所以中间必须有一个
    DIB <-> PNG 的转换层, 而项目承诺"零第三方依赖", 就自己写。

性能上的做法(纯 Python 也能跑得动):
    * 通道重排用**扩展切片赋值**(`row[0::3] = r`), 这在 CPython 里是 C 级操作;
      DIB 24bpp 截图 1920x1080 大约几十毫秒;
    * PNG 解码最贵的"反滤波"是逐字节的, 所以加了快路径: 全是 filter 0 时直接
      切片取数据; 只有真正用了 1~4 号滤波的行才逐字节还原(大图会慢一两秒,
      调用方可以先用 pixel_cap 限制)。

支持范围: 8 位色深的 PNG(灰度/RGB/调色板/带 alpha, 非隔行),
DIB 支持 1/4/8/16/24/32 bpp 与 BI_RGB / BI_BITFIELDS。
"""
from __future__ import annotations

import struct
import zlib
from typing import Optional, Tuple

PNG_SIG = b"\x89PNG\r\n\x1a\n"

#: 解码一张图最多允许多少像素(防御畸形/超大图把内存吃光)
DEFAULT_PIXEL_CAP = 40_000_000


class ImageError(RuntimeError):
    """图片数据不合法或不支持(信息面向日志/用户)。"""


# ===========================================================================
# PNG
# ===========================================================================
def png_size(data: bytes) -> Optional[Tuple[int, int]]:
    """从 PNG 头里读出宽高(不完整解码), 失败返回 None。"""
    if len(data) < 24 or not data.startswith(PNG_SIG):
        return None
    if data[12:16] != b"IHDR":
        return None
    try:
        w, h = struct.unpack_from(">II", data, 16)
    except struct.error:
        return None
    return (int(w), int(h))


def _iter_chunks(data: bytes):
    if not data.startswith(PNG_SIG):
        raise ImageError("不是 PNG 数据(签名不对)")
    off = len(PNG_SIG)
    total = len(data)
    while off + 8 <= total:
        (length,) = struct.unpack_from(">I", data, off)
        ctype = data[off + 4:off + 8]
        body_start = off + 8
        body_end = body_start + length
        if body_end + 4 > total:
            raise ImageError("PNG 数据被截断(块 %r)" % ctype)
        body = data[body_start:body_end]
        (crc,) = struct.unpack_from(">I", data, body_end)
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise ImageError("PNG 块 %r 校验失败(数据坏了)" % ctype)
        yield ctype, body
        off = body_end + 4


def decode_png(data: bytes, pixel_cap: int = DEFAULT_PIXEL_CAP
               ) -> Tuple[bytes, int, int]:
    """PNG -> (RGBA 字节, 宽, 高)。"""
    idat = bytearray()
    ihdr = None
    palette = None
    trns = None
    for ctype, body in _iter_chunks(data):
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"tRNS":
            trns = body
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise ImageError("PNG 缺少 IHDR")
    w, h, depth, color, comp, filt, interlace = ihdr
    if w <= 0 or h <= 0:
        raise ImageError("PNG 尺寸非法: %dx%d" % (w, h))
    if w * h > pixel_cap:
        raise ImageError("PNG 太大(%dx%d), 超过处理上限" % (w, h))
    if interlace:
        raise ImageError("暂不支持隔行(Adam7)PNG")
    if depth != 8:
        raise ImageError("暂不支持 %d 位色深的 PNG(只支持 8 位)" % depth)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ImageError("不认识的 PNG 颜色类型 %d" % color)
    if color == 3 and not palette:
        raise ImageError("调色板 PNG 缺少 PLTE")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageError("PNG 像素数据解压失败: %s" % exc) from exc
    stride = w * channels
    need = (stride + 1) * h
    if len(raw) < need:
        raise ImageError("PNG 像素数据不完整(需要 %d 字节, 只有 %d)"
                         % (need, len(raw)))
    pixels = _unfilter(raw, w, h, channels, stride)
    return _to_rgba(pixels, w, h, color, palette, trns), w, h


def _unfilter(raw: bytes, w: int, h: int, channels: int,
              stride: int) -> bytes:
    """反滤波。全是 filter 0 时走快路径(切片), 否则逐行还原。"""
    row_len = stride + 1
    if raw[:row_len].startswith(b"\x00"):
        # 先探测是否全 0: 只在第一行是 0 时才值得扫一遍
        if all(raw[y * row_len] == 0 for y in range(h)):
            return b"".join(raw[y * row_len + 1:y * row_len + row_len]
                            for y in range(h))
    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += row_len
        if ftype == 0:
            pass
        elif ftype == 1:                       # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:                       # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                       # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                       # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa = p - a if p >= a else a - p
                pb = p - b if p >= b else b - p
                pc = p - c if p >= c else c - p
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise ImageError("未知 PNG 滤波类型 %d" % ftype)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def _to_rgba(pixels: bytes, w: int, h: int, color: int,
             palette: Optional[bytes], trns: Optional[bytes]) -> bytes:
    n = w * h
    out = bytearray(n * 4)
    if color == 6:                             # RGBA
        return bytes(pixels[:n * 4])
    if color == 2:                             # RGB
        out[0::4] = pixels[0::3]
        out[1::4] = pixels[1::3]
        out[2::4] = pixels[2::3]
        out[3::4] = b"\xff" * n
        return bytes(out)
    if color == 0:                             # 灰度
        out[0::4] = pixels
        out[1::4] = pixels
        out[2::4] = pixels
        out[3::4] = b"\xff" * n
        return bytes(out)
    if color == 4:                             # 灰度 + alpha
        out[0::4] = pixels[0::2]
        out[1::4] = pixels[0::2]
        out[2::4] = pixels[0::2]
        out[3::4] = pixels[1::2]
        return bytes(out)
    # color == 3: 调色板
    assert palette is not None
    table = []
    for i in range(0, len(palette) - 2, 3):
        alpha = 255
        if trns is not None and i // 3 < len(trns):
            alpha = trns[i // 3]
        table.append(bytes((palette[i], palette[i + 1], palette[i + 2], alpha)))
    while len(table) < 256:
        table.append(b"\x00\x00\x00\xff")
    out = bytearray(b"".join([table[b] for b in pixels[:n]]))
    return bytes(out)


def encode_png(rgba: bytes, w: int, h: int, alpha: bool = True) -> bytes:
    """RGBA -> PNG。固定用 filter 0, 保证同样输入产生同样字节(剪辑板去重要用)。"""
    if len(rgba) < w * h * 4:
        raise ImageError("像素数据不足: 需要 %d 字节, 只有 %d"
                         % (w * h * 4, len(rgba)))
    color = 6 if alpha else 2
    stride = w * (4 if alpha else 3)
    raw = bytearray((stride + 1) * h)
    for y in range(h):
        src = y * w * 4
        dst = y * (stride + 1)
        raw[dst] = 0                                   # filter: None
        if alpha:
            raw[dst + 1:dst + 1 + stride] = rgba[src:src + stride]
        else:
            row = rgba[src:src + w * 4]
            rgb = bytearray(w * 3)
            rgb[0::3] = row[0::4]
            rgb[1::3] = row[1::4]
            rgb[2::3] = row[2::4]
            raw[dst + 1:dst + 1 + stride] = rgb
    out = bytearray(PNG_SIG)

    def chunk(ctype: bytes, body: bytes) -> None:
        out.extend(struct.pack(">I", len(body)))
        out.extend(ctype)
        out.extend(body)
        out.extend(struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))

    chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0))
    chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    chunk(b"IEND", b"")
    return bytes(out)


def has_alpha(rgba: bytes) -> bool:
    """是否真的用了透明(全 255 就当作不透明, 可以走 24bpp DIB)。"""
    return not all(v == 255 for v in rgba[3::4])


# ===========================================================================
# Windows DIB
# ===========================================================================
BI_RGB = 0
BI_BITFIELDS = 3
BI_ALPHABITFIELDS = 6


def _mask_shift_bits(mask: int) -> Tuple[int, int]:
    if not mask:
        return 0, 0
    shift = 0
    while not (mask >> shift) & 1:
        shift += 1
    bits = 0
    m = mask >> shift
    while m & 1:
        bits += 1
        m >>= 1
    return shift, bits


def dib_to_rgba(dib: bytes, pixel_cap: int = DEFAULT_PIXEL_CAP
                ) -> Tuple[bytes, int, int]:
    """Windows DIB(CF_DIB / CF_DIBV5 的内容) -> (RGBA, 宽, 高)。"""
    if len(dib) < 12:
        raise ImageError("DIB 数据太短")
    (hdr_size,) = struct.unpack_from("<I", dib, 0)
    masks: Optional[Tuple[int, int, int, int]] = None
    palette_off = hdr_size
    palette_entry = 4
    if hdr_size == 12:                                   # BITMAPCOREHEADER
        w, h, planes, bpp = struct.unpack_from("<HHHH", dib, 4)
        compression = BI_RGB
        palette_entry = 3
        clr_used = 0
    elif hdr_size >= 40:
        (w, h, planes, bpp, compression, _size_image,
         _xppm, _yppm, clr_used, _clr_imp) = struct.unpack_from("<iiHHIIiiII",
                                                                 dib, 4)
        if compression in (BI_BITFIELDS, BI_ALPHABITFIELDS):
            if hdr_size >= 52:
                rgba_mask = struct.unpack_from("<IIII", dib, 40) \
                    if hdr_size >= 56 else None
                rgb_masks = struct.unpack_from("<III", dib, 40)
                masks = (rgb_masks[0], rgb_masks[1], rgb_masks[2],
                         rgba_mask[3] if rgba_mask else 0)
            else:
                if len(dib) < 52:
                    raise ImageError("DIB 缺少颜色掩码")
                r, g, b = struct.unpack_from("<III", dib, 40)
                masks = (r, g, b, 0)
                palette_off = 52
    else:
        raise ImageError("不认识的 DIB 头大小 %d" % hdr_size)

    if w <= 0 or h == 0:
        raise ImageError("DIB 尺寸非法: %dx%d" % (w, h))
    top_down = h < 0
    h = abs(h)
    if w * h > pixel_cap:
        raise ImageError("DIB 太大(%dx%d), 超过处理上限" % (w, h))
    if palette_off > len(dib):
        raise ImageError("DIB 头长度超出数据范围")

    palette = None
    if bpp <= 8:
        # 调色板项数: 优先用 biClrUsed, 没写(0)就按 2^bpp。写多了会算错像素起点,
        # 所以上限硬卡在 2^bpp。
        count = clr_used if 0 < clr_used <= (1 << bpp) else (1 << bpp)
        need = palette_off + count * palette_entry
        if len(dib) < need:
            raise ImageError("DIB 调色板不完整")
        palette = []
        for i in range(count):
            off = palette_off + i * palette_entry
            b, g, r = dib[off], dib[off + 1], dib[off + 2]
            # 调色板第 4 字节是"保留"字段, 不是 alpha: 老软件里它是 0,
            # 若当 alpha 用会把整幅图变成全透明。所以一律按不透明处理。
            palette.append(bytes((r, g, b, 255)))
        data_off = need
    elif bpp == 16 and masks is None:
        data_off = palette_off
        masks = (0x7C00, 0x03E0, 0x001F, 0)                # BI_RGB 16bpp = X1R5G5B5
    else:
        data_off = palette_off

    stride = ((w * bpp + 31) // 32) * 4
    if data_off + stride * h > len(dib):
        raise ImageError("DIB 像素数据不完整(需要 %d 字节, 只有 %d)"
                         % (data_off + stride * h, len(dib)))
    out = bytearray(w * h * 4)
    for y in range(h):
        src_y = y if top_down else (h - 1 - y)
        off = data_off + src_y * stride
        dst = y * w * 4
        if bpp == 32:
            row = dib[off:off + w * 4]
            if masks:
                # BI_BITFIELDS / BI_ALPHABITFIELDS: 通道位置由掩码说了算,
                # 不能想当然按 BGRX 读。没有 alpha 掩码时按不透明处理(0xFF)。
                rs, rb = _mask_shift_bits(masks[0])
                gs, gb = _mask_shift_bits(masks[1])
                bs, bb = _mask_shift_bits(masks[2])
                as_, ab = _mask_shift_bits(masks[3])
                vals = struct.unpack_from("<%dI" % w, row, 0)
                alphas = bytearray()
                for x, v in enumerate(vals):
                    d = dst + x * 4
                    out[d] = _scale((v & masks[0]) >> rs, rb)
                    out[d + 1] = _scale((v & masks[1]) >> gs, gb)
                    out[d + 2] = _scale((v & masks[2]) >> bs, bb)
                    a = _scale((v & masks[3]) >> as_, ab) if masks[3] else 255
                    out[d + 3] = a
                    alphas.append(a)
                # 有 alpha 掩码但整行都是 0: 这是"忘了填 alpha"的老软件行为,
                # 按不透明处理, 否则整幅图会变成全透明什么都看不见
                if masks[3] and not any(alphas):
                    out[dst + 3::4] = b"\xff" * w
                continue
            out[dst + 0:dst + w * 4:4] = row[2::4]        # R <- 第 3 字节
            out[dst + 1:dst + w * 4:4] = row[1::4]
            out[dst + 2:dst + w * 4:4] = row[0::4]
            a = row[3::4]
            # 32bpp BI_RGB 的 alpha 常常是 0(截图为不透明), 全 0 就按不透明处理
            out[dst + 3:dst + w * 4:4] = a if any(a) else b"\xff" * w
        elif bpp == 24:
            row = dib[off:off + w * 3]
            out[dst + 0:dst + w * 4:4] = row[2::3]
            out[dst + 1:dst + w * 4:4] = row[1::3]
            out[dst + 2:dst + w * 4:4] = row[0::3]
            out[dst + 3:dst + w * 4:4] = b"\xff" * w
        elif bpp == 16:
            assert masks is not None
            rs, rb = _mask_shift_bits(masks[0])
            gs, gb = _mask_shift_bits(masks[1])
            bs, bb = _mask_shift_bits(masks[2])
            vals = struct.unpack_from("<%dH" % w, dib, off)
            for x, v in enumerate(vals):
                d = dst + x * 4
                out[d] = _scale((v & masks[0]) >> rs, rb)
                out[d + 1] = _scale((v & masks[1]) >> gs, gb)
                out[d + 2] = _scale((v & masks[2]) >> bs, bb)
                out[d + 3] = 255
        elif bpp in (1, 4, 8):
            assert palette is not None
            # 用查表拼整行, 比逐像素快得多
            idx = _row_indices(dib, off, w, bpp)
            out[dst:dst + w * 4] = b"".join([palette[i] for i in idx])
        else:
            raise ImageError("暂不支持 %d bpp 的 DIB" % bpp)
    return bytes(out), w, h


def _scale(value: int, bits: int) -> int:
    if bits >= 8:
        return (value >> (bits - 8)) & 0xFF
    if bits <= 0:
        return 0
    return value * 255 // ((1 << bits) - 1)


def _row_indices(dib: bytes, off: int, w: int, bpp: int):
    row = dib[off:off + ((w * bpp + 7) // 8)]
    if bpp == 8:
        return row[:w]
    if bpp == 4:
        out = bytearray(w)
        for i in range(w):
            byte = row[i // 2]
            out[i] = (byte >> 4) if i % 2 == 0 else (byte & 0x0F)
        return out
    out = bytearray(w)                                  # bpp == 1
    for i in range(w):
        out[i] = (row[i // 8] >> (7 - i % 8)) & 1
    return out


def rgba_to_dib(rgba: bytes, w: int, h: int, bpp: int = 24) -> bytes:
    """RGBA -> CF_DIB 内容(BITMAPINFOHEADER + 自下而上像素)。

    默认 24bpp BI_RGB: 兼容性最好(画图/Office/老程序都认)。需要透明度的场景
    我们同时还会写一个注册格式 "PNG", 由支持它的程序优先使用。
    """
    if bpp not in (24, 32):
        raise ImageError("DIB 写出只支持 24/32 bpp")
    if len(rgba) < w * h * 4:
        raise ImageError("像素数据不足")
    stride = ((w * bpp + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, bpp, BI_RGB,
                         stride * h, 2835, 2835, 0, 0)
    body = bytearray(stride * h)
    for y in range(h):
        src = y * w * 4
        dst = (h - 1 - y) * stride                 # DIB 自下而上
        row = rgba[src:src + w * 4]
        if bpp == 24:
            rgb = bytearray(w * 3)
            rgb[0::3] = row[2::4]                  # B
            rgb[1::3] = row[1::4]                  # G
            rgb[2::3] = row[0::4]                  # R
            body[dst:dst + w * 3] = rgb
        else:
            bgra = bytearray(w * 4)
            bgra[0::4] = row[2::4]
            bgra[1::4] = row[1::4]
            bgra[2::4] = row[0::4]
            bgra[3::4] = row[3::4]
            body[dst:dst + w * 4] = bgra
    return header + bytes(body)


def dib_from_png(png: bytes, pixel_cap: int = DEFAULT_PIXEL_CAP) -> bytes:
    """PNG -> CF_DIB(24bpp; 有透明时用 32bpp 以保留 alpha)。"""
    rgba, w, h = decode_png(png, pixel_cap=pixel_cap)
    bpp = 32 if has_alpha(rgba) else 24
    return rgba_to_dib(rgba, w, h, bpp=bpp)


def png_from_dib(dib: bytes, pixel_cap: int = DEFAULT_PIXEL_CAP) -> bytes:
    """CF_DIB -> PNG。"""
    rgba, w, h = dib_to_rgba(dib, pixel_cap=pixel_cap)
    return encode_png(rgba, w, h, alpha=has_alpha(rgba))
