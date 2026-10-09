"""Image codec: PNG <-> RGBA <-> Windows DIB, standard library only (zlib).

Why it is needed:
    Images on the Windows clipboard are **CF_DIB** (BITMAPINFOHEADER + pixels,
    usually bottom-up and in BGR order), while xclip/wl-copy on the Linux side
    only understand PNG. So there has to be a DIB <-> PNG conversion layer in
    between, and since the project promises "zero third-party dependencies", we
    write it ourselves.

How performance is handled (this stays fast enough even in pure Python):
    * channel reordering uses **extended slice assignment** (`row[0::3] = r`),
      which is a C-level operation in CPython; a 1920x1080 24bpp DIB screenshot
      takes a few dozen milliseconds;
    * the most expensive part of PNG decoding, the "unfiltering", is byte by
      byte, so a fast path was added: when every filter is 0 the data is taken by
      slicing, and only rows that really use filter types 1..4 are restored byte
      by byte (a large image can take a second or two; callers can cap it first
      with pixel_cap).

Supported range: 8-bit-per-channel PNG (grayscale/RGB/palette/with alpha,
non-interlaced); DIB supports 1/4/8/16/24/32 bpp and BI_RGB / BI_BITFIELDS.
"""
from __future__ import annotations

import struct
import zlib
from typing import Optional, Tuple

PNG_SIG = b"\x89PNG\r\n\x1a\n"

#: maximum number of pixels allowed when decoding one image (protects against a
#: malformed or oversized image eating all the memory)
DEFAULT_PIXEL_CAP = 40_000_000


class ImageError(RuntimeError):
    """Image data is invalid or unsupported (the message is meant for logs/users)."""


# ===========================================================================
# PNG
# ===========================================================================
def png_size(data: bytes) -> Optional[Tuple[int, int]]:
    """Read width/height from the PNG header (without a full decode); None on failure."""
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
        raise ImageError("not PNG data (wrong signature)")
    off = len(PNG_SIG)
    total = len(data)
    while off + 8 <= total:
        (length,) = struct.unpack_from(">I", data, off)
        ctype = data[off + 4:off + 8]
        body_start = off + 8
        body_end = body_start + length
        if body_end + 4 > total:
            raise ImageError("PNG data is truncated (chunk %r)" % ctype)
        body = data[body_start:body_end]
        (crc,) = struct.unpack_from(">I", data, body_end)
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:
            raise ImageError("PNG chunk %r failed its CRC check (corrupt data)" % ctype)
        yield ctype, body
        off = body_end + 4


def decode_png(data: bytes, pixel_cap: int = DEFAULT_PIXEL_CAP
               ) -> Tuple[bytes, int, int]:
    """PNG -> (RGBA bytes, width, height)."""
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
        raise ImageError("PNG is missing IHDR")
    w, h, depth, color, comp, filt, interlace = ihdr
    if w <= 0 or h <= 0:
        raise ImageError("invalid PNG size: %dx%d" % (w, h))
    if w * h > pixel_cap:
        raise ImageError("PNG too large (%dx%d), above the processing limit" % (w, h))
    if interlace:
        raise ImageError("interlaced (Adam7) PNG is not supported yet")
    if depth != 8:
        raise ImageError("%d-bit PNG is not supported yet (only 8-bit)" % depth)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ImageError("unknown PNG color type %d" % color)
    if color == 3 and not palette:
        raise ImageError("palette PNG is missing PLTE")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageError("decompressing the PNG pixel data failed: %s" % exc) from exc
    stride = w * channels
    need = (stride + 1) * h
    if len(raw) < need:
        raise ImageError("PNG pixel data is incomplete (needs %d bytes, only %d present)"
                         % (need, len(raw)))
    pixels = _unfilter(raw, w, h, channels, stride)
    return _to_rgba(pixels, w, h, color, palette, trns), w, h


def _unfilter(raw: bytes, w: int, h: int, channels: int,
              stride: int) -> bytes:
    """Unfilter. When every filter is 0 take the fast path (slicing), otherwise
    restore row by row."""
    row_len = stride + 1
    if raw[:row_len].startswith(b"\x00"):
        # probe for all-zero first: only worth scanning when the first row is 0
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
            raise ImageError("unknown PNG filter type %d" % ftype)
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
    if color == 0:                             # grayscale
        out[0::4] = pixels
        out[1::4] = pixels
        out[2::4] = pixels
        out[3::4] = b"\xff" * n
        return bytes(out)
    if color == 4:                             # grayscale + alpha
        out[0::4] = pixels[0::2]
        out[1::4] = pixels[0::2]
        out[2::4] = pixels[0::2]
        out[3::4] = pixels[1::2]
        return bytes(out)
    # color == 3: palette
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
    """RGBA -> PNG. Always uses filter 0, so identical input produces identical
    bytes (needed for clipboard deduplication)."""
    if len(rgba) < w * h * 4:
        raise ImageError("not enough pixel data: needs %d bytes, only %d present"
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
    """Whether transparency is really used (all 255 counts as opaque, which can
    take the 24bpp DIB path)."""
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
    """Windows DIB (the payload of CF_DIB / CF_DIBV5) -> (RGBA, width, height)."""
    if len(dib) < 12:
        raise ImageError("DIB data is too short")
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
                    raise ImageError("DIB is missing its color masks")
                r, g, b = struct.unpack_from("<III", dib, 40)
                masks = (r, g, b, 0)
                palette_off = 52
    else:
        raise ImageError("unknown DIB header size %d" % hdr_size)

    if w <= 0 or h == 0:
        raise ImageError("invalid DIB size: %dx%d" % (w, h))
    top_down = h < 0
    h = abs(h)
    if w * h > pixel_cap:
        raise ImageError("DIB too large (%dx%d), above the processing limit" % (w, h))
    if palette_off > len(dib):
        raise ImageError("the DIB header length exceeds the available data")

    palette = None
    if bpp <= 8:
        # Number of palette entries: prefer biClrUsed and fall back to 2^bpp when
        # it is absent (0). Too many entries would compute the pixel start wrong,
        # so the upper bound is hard-capped at 2^bpp.
        count = clr_used if 0 < clr_used <= (1 << bpp) else (1 << bpp)
        need = palette_off + count * palette_entry
        if len(dib) < need:
            raise ImageError("the DIB palette is incomplete")
        palette = []
        for i in range(count):
            off = palette_off + i * palette_entry
            b, g, r = dib[off], dib[off + 1], dib[off + 2]
            # The palette's 4th byte is a "reserved" field, not alpha: legacy
            # software leaves it 0, and using it as alpha would make the whole
            # image fully transparent. So it is always treated as opaque.
            palette.append(bytes((r, g, b, 255)))
        data_off = need
    elif bpp == 16 and masks is None:
        data_off = palette_off
        masks = (0x7C00, 0x03E0, 0x001F, 0)                # BI_RGB 16bpp = X1R5G5B5
    else:
        data_off = palette_off

    stride = ((w * bpp + 31) // 32) * 4
    if data_off + stride * h > len(dib):
        raise ImageError("DIB pixel data is incomplete (needs %d bytes, only %d present)"
                         % (data_off + stride * h, len(dib)))
    out = bytearray(w * h * 4)
    for y in range(h):
        src_y = y if top_down else (h - 1 - y)
        off = data_off + src_y * stride
        dst = y * w * 4
        if bpp == 32:
            row = dib[off:off + w * 4]
            if masks:
                # BI_BITFIELDS / BI_ALPHABITFIELDS: the masks decide where the
                # channels are, so BGRX must not be assumed. Without an alpha
                # mask the pixels count as opaque (0xFF).
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
                # An alpha mask exists but the whole row is 0: legacy software
                # forgetting to fill alpha in. Treat it as opaque, otherwise the
                # whole image turns fully transparent and nothing is visible.
                if masks[3] and not any(alphas):
                    out[dst + 3::4] = b"\xff" * w
                continue
            out[dst + 0:dst + w * 4:4] = row[2::4]        # R <- 3rd byte
            out[dst + 1:dst + w * 4:4] = row[1::4]
            out[dst + 2:dst + w * 4:4] = row[0::4]
            a = row[3::4]
            # 32bpp BI_RGB alpha is often 0 (an opaque screenshot); all-zero means opaque
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
            # build the whole row from the lookup table, far faster than per pixel
            idx = _row_indices(dib, off, w, bpp)
            out[dst:dst + w * 4] = b"".join([palette[i] for i in idx])
        else:
            raise ImageError("%d bpp DIB is not supported yet" % bpp)
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
    """RGBA -> CF_DIB payload (BITMAPINFOHEADER + bottom-up pixels).

    Defaults to 24bpp BI_RGB: the best compatibility (Paint/Office/legacy
    programs all understand it). Where transparency is needed we also write a
    registered "PNG" format, which programs that support it prefer.
    """
    if bpp not in (24, 32):
        raise ImageError("writing a DIB only supports 24/32 bpp")
    if len(rgba) < w * h * 4:
        raise ImageError("not enough pixel data")
    stride = ((w * bpp + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, bpp, BI_RGB,
                         stride * h, 2835, 2835, 0, 0)
    body = bytearray(stride * h)
    for y in range(h):
        src = y * w * 4
        dst = (h - 1 - y) * stride                 # DIB is bottom-up
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
    """PNG -> CF_DIB (24bpp; 32bpp when transparency is present, to keep alpha)."""
    rgba, w, h = decode_png(png, pixel_cap=pixel_cap)
    bpp = 32 if has_alpha(rgba) else 24
    return rgba_to_dib(rgba, w, h, bpp=bpp)


def png_from_dib(dib: bytes, pixel_cap: int = DEFAULT_PIXEL_CAP) -> bytes:
    """CF_DIB -> PNG."""
    rgba, w, h = dib_to_rgba(dib, pixel_cap=pixel_cap)
    return encode_png(rgba, w, h, alpha=has_alpha(rgba))
