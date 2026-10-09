"""Image codec tests: PNG (four filter types / grayscale / palette) and Windows DIB
at every bit depth.

This is the complexity that "zero third-party dependencies" forces us to carry, so
every branch has to be pinned down: get one sign wrong in PNG unfiltering and the
pasted image comes out garbled; reverse the DIB bottom-up/BGR order and the image
arrives upside down. Neither raises an error -- they just fail silently -- so only
tests can catch them.
"""
from __future__ import annotations

import struct
import unittest
import zlib

from crosspc import image as IM


# ===========================================================================
# Helpers that hand-build PNGs (test-only) to feed the decoder valid and invalid input
# ===========================================================================
def _chunk(ctype: bytes, body: bytes, crc_ok: bool = True) -> bytes:
    crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
    if not crc_ok:
        crc ^= 0xDEADBEEF
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)


def build_png(w: int, h: int, color: int, rows, palette=None,
              depth: int = 8, interlace: int = 0, crc_ok: bool = True,
              truncate: bool = False) -> bytes:
    """rows: [(filter_type, raw bytes of that row)]"""
    out = bytearray(IM.PNG_SIG)
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, color, 0, 0,
                                       interlace), crc_ok)
    if palette:
        out += _chunk(b"PLTE", palette, crc_ok)
    raw = bytearray()
    for ftype, data in rows:
        raw.append(ftype)
        raw += data
    out += _chunk(b"IDAT", zlib.compress(bytes(raw)), crc_ok)
    out += _chunk(b"IEND", b"", crc_ok)
    data = bytes(out)
    return data[:len(data) - 6] if truncate else data


def rows_of(pixels: bytes, w: int, h: int, channels: int, ftype: int = 0):
    """Split a whole pixel buffer into [(filter, row bytes)], one entry per row --
    build_png needs one entry per row."""
    stride = w * channels
    return [(ftype, bytes(pixels[y * stride:(y + 1) * stride]))
            for y in range(h)]


def rgba_bytes(w: int, h: int, seed: int = 0) -> bytes:
    out = bytearray()
    for y in range(h):
        for x in range(w):
            out += bytes(((x * 7 + seed) % 256, (y * 11 + seed) % 256,
                          (x * y + seed) % 256, (x + y + seed) % 256))
    return bytes(out)


def filter_rows(pixels: bytes, w: int, h: int, channels: int, ftype: int):
    """Encode raw pixels with the given filter type into [(ftype, row bytes)]."""
    stride = w * channels
    rows = []
    prev = bytearray(stride)
    for y in range(h):
        line = bytearray(pixels[y * stride:(y + 1) * stride])
        out = bytearray(stride)
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if ftype == 0:
                pr = 0
            elif ftype == 1:
                pr = a
            elif ftype == 2:
                pr = b
            elif ftype == 3:
                pr = (a + b) >> 1
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            out[i] = (line[i] - pr) & 0xFF
        rows.append((ftype, bytes(out)))
        prev = line
    return rows


# ===========================================================================
class TestPng(unittest.TestCase):
    def test_encode_decode_roundtrip_rgba(self):
        for (w, h) in ((1, 1), (3, 2), (17, 9), (64, 33)):
            src = rgba_bytes(w, h, seed=w)
            png = IM.encode_png(src, w, h)
            self.assertEqual(IM.png_size(png), (w, h))
            back, bw, bh = IM.decode_png(png)
            self.assertEqual((bw, bh), (w, h))
            self.assertEqual(back, src, "%dx%d roundtrip mismatch" % (w, h))

    def test_encode_without_alpha_drops_alpha(self):
        src = rgba_bytes(4, 3)
        png = IM.encode_png(src, 4, 3, alpha=False)
        back, w, h = IM.decode_png(png)
        self.assertEqual(back[3::4], b"\xff" * 12)
        self.assertEqual(back[0::4], src[0::4])

    def test_encode_is_deterministic(self):
        """Identical input must produce identical bytes: clipboard dedup relies on content hashing."""
        src = rgba_bytes(20, 20)
        self.assertEqual(IM.encode_png(src, 20, 20), IM.encode_png(src, 20, 20))

    def test_all_filter_types_decode_correctly(self):
        """Verify each of the four filters Sub/Up/Average/Paeth one by one."""
        w, h, ch = 9, 7, 3
        pixels = bytes((x * 3 + y * 5 + i) % 256
                       for y in range(h) for x in range(w) for i in range(ch))
        for ftype in (0, 1, 2, 3, 4):
            rows = filter_rows(pixels, w, h, ch, ftype)
            png = build_png(w, h, 2, rows)
            rgba, bw, bh = IM.decode_png(png)
            self.assertEqual((bw, bh), (w, h), "filter %d" % ftype)
            # compare against the manually expanded RGB
            expect = bytearray()
            for i in range(w * h):
                expect += pixels[i * 3:i * 3 + 3] + b"\xff"
            self.assertEqual(rgba, bytes(expect), "filter %d reconstruction wrong" % ftype)

    def test_gray_and_gray_alpha(self):
        w, h = 5, 3
        gray = bytes((x * 20 + y) % 256 for y in range(h) for x in range(w))
        png = build_png(w, h, 0, rows_of(gray, w, h, 1))
        rgba, _, _ = IM.decode_png(png)
        self.assertEqual(rgba[0::4], gray)
        self.assertEqual(rgba[3::4], b"\xff" * (w * h))

        ga = bytes(v for y in range(h) for x in range(w)
                   for v in ((x * 9 + y) % 256, 128))
        png = build_png(w, h, 4, rows_of(ga, w, h, 2))
        rgba, _, _ = IM.decode_png(png)
        self.assertEqual(rgba[0::4], ga[0::2])
        self.assertEqual(rgba[3::4], b"\x80" * (w * h))

    def test_palette_png(self):
        palette = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))
        idx = bytes((0, 1, 2, 1))
        png = build_png(4, 1, 3, [(0, idx)], palette=palette)
        rgba, w, h = IM.decode_png(png)
        self.assertEqual((w, h), (4, 1))
        self.assertEqual(rgba[0:4], b"\xff\x00\x00\xff")
        self.assertEqual(rgba[4:8], b"\x00\xff\x00\xff")
        self.assertEqual(rgba[8:12], b"\x00\x00\xff\xff")
        self.assertEqual(rgba[12:16], b"\x00\xff\x00\xff")

    def test_rejects_bad_signature(self):
        with self.assertRaises(IM.ImageError):
            IM.decode_png(b"not a png at all")

    def test_rejects_bad_crc(self):
        png = build_png(2, 2, 6, [(0, rgba_bytes(2, 2))], crc_ok=False)
        with self.assertRaises(IM.ImageError):
            IM.decode_png(png)

    def test_rejects_truncated(self):
        png = build_png(2, 2, 6, [(0, rgba_bytes(2, 2))], truncate=True)
        with self.assertRaises(IM.ImageError):
            IM.decode_png(png)

    def test_rejects_interlaced_and_16bit(self):
        rows = [(0, rgba_bytes(2, 2))]
        with self.assertRaises(IM.ImageError):
            IM.decode_png(build_png(2, 2, 6, rows, interlace=1))
        with self.assertRaises(IM.ImageError):
            IM.decode_png(build_png(2, 2, 6, rows, depth=16))

    def test_rejects_oversize_by_pixel_cap(self):
        rows = [(0, b"\x00" * (100 * 100 * 4))]
        png = build_png(100, 100, 6, rows)
        with self.assertRaises(IM.ImageError):
            IM.decode_png(png, pixel_cap=1000)

    def test_rejects_missing_idat(self):
        png = bytearray(IM.PNG_SIG)
        png += _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
        png += _chunk(b"IEND", b"")
        with self.assertRaises(IM.ImageError):
            IM.decode_png(bytes(png))

    def test_has_alpha(self):
        self.assertFalse(IM.has_alpha(b"\x01\x02\x03\xff" * 4))
        self.assertTrue(IM.has_alpha(b"\x01\x02\x03\xfe" * 4))

    def test_png_size_bad_input(self):
        self.assertIsNone(IM.png_size(b""))
        self.assertIsNone(IM.png_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32))


# ===========================================================================
class TestDib(unittest.TestCase):
    def test_24bpp_roundtrip_and_row_order(self):
        """DIB is bottom-up + BGR: flipping it vertically or writing RGB instead must
        both be things the tests catch."""
        w, h = 3, 2
        rgba = bytes((
            255, 0, 0, 255,       # (0,0) red
            0, 255, 0, 255,       # (1,0) green
            0, 0, 255, 255,      # (2,0) blue
            10, 20, 30, 255,      # (0,1)
            40, 50, 60, 255,      # (1,1)
            70, 80, 90, 255,      # (2,1)
        ))
        dib = IM.rgba_to_dib(rgba, w, h, bpp=24)
        self.assertEqual(struct.unpack_from("<I", dib, 0)[0], 40)
        # the first pixel row in the file should be the **last row** of the source
        # image, and it should be BGR
        row0 = dib[40:40 + w * 3]
        self.assertEqual(row0[0:3], bytes((30, 20, 10)))
        self.assertEqual(row0[3:6], bytes((60, 50, 40)))
        back, bw, bh = IM.dib_to_rgba(dib)
        self.assertEqual((bw, bh), (w, h))
        self.assertEqual(back, rgba)

    def test_32bpp_roundtrip_keeps_alpha(self):
        w, h = 2, 2
        rgba = bytes((1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16))
        dib = IM.rgba_to_dib(rgba, w, h, bpp=32)
        back, _, _ = IM.dib_to_rgba(dib)
        self.assertEqual(back, rgba)

    def test_32bpp_all_zero_alpha_treated_as_opaque(self):
        """Common in Windows screenshots: 32bpp but with all-zero alpha, which must be
        treated as opaque, otherwise the whole image becomes transparent."""
        w, h = 2, 1
        rgba = bytes((10, 20, 30, 0, 40, 50, 60, 0))
        dib = IM.rgba_to_dib(rgba, w, h, bpp=32)
        back, _, _ = IM.dib_to_rgba(dib)
        self.assertEqual(back[3::4], b"\xff\xff")
        self.assertEqual(back[0:4], bytes((10, 20, 30, 255)))

    def test_top_down_dib(self):
        w, h = 2, 2
        rgba = bytes((1, 1, 1, 255, 2, 2, 2, 255, 3, 3, 3, 255, 4, 4, 4, 255))
        dib = bytearray(IM.rgba_to_dib(rgba, w, h, bpp=24))
        struct.pack_into("<i", dib, 8, -h)          # negative biHeight = top-down
        # bottom-up data must now be read as top-down => the row order flips
        back, _, _ = IM.dib_to_rgba(bytes(dib))
        self.assertEqual(back[0:4], bytes((3, 3, 3, 255)))
        self.assertEqual(back[8:12], bytes((1, 1, 1, 255)))

    def test_8bpp_palette(self):
        w, h = 2, 1
        header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 8, 0,
                             ((w + 3) // 4) * 4 * h, 0, 0, 0, 0)
        palette = bytearray(256 * 4)
        palette[0:4] = bytes((0, 0, 255, 0))        # index 0 -> red (stored as BGR)
        palette[4:8] = bytes((0, 255, 0, 0))        # index 1 -> green
        row = bytes((1, 0)) + b"\x00\x00"           # padded to 4 bytes
        back, bw, bh = IM.dib_to_rgba(header + bytes(palette) + row)
        self.assertEqual((bw, bh), (2, 1))
        self.assertEqual(back[0:4], bytes((0, 255, 0, 255)))
        self.assertEqual(back[4:8], bytes((255, 0, 0, 255)))

    def test_1bpp_and_4bpp_palette(self):
        # 1bpp must have only 2 palette entries (biClrUsed=2): extra ones shift the
        # pixel start offset
        palette = bytearray(2 * 4)
        palette[0:4] = bytes((0, 0, 0, 0))
        palette[4:8] = bytes((255, 255, 255, 0))
        header = struct.pack("<IiiHHIIiiII", 40, 8, 1, 1, 1, 0, 4, 0, 0, 2, 0)
        # DIB rows are 4-byte aligned; 1bpp x 8 = 1 byte, so 3 padding bytes are required
        data = bytes((0b10110001,)) + b"\x00\x00\x00"
        back, w, h = IM.dib_to_rgba(header + bytes(palette) + data)
        self.assertEqual((w, h), (8, 1))
        expect = [255, 0, 255, 255, 0, 0, 0, 255]
        self.assertEqual(list(back[0::4]), expect)

    def test_clr_used_zero_means_full_palette(self):
        """With biClrUsed=0 the palette length is computed as 2^bpp (standard behaviour)."""
        palette = bytearray(2 * 4)
        palette[0:4] = bytes((0, 0, 0, 0))
        palette[4:8] = bytes((9, 9, 9, 0))
        header = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 1, 0, 4, 0, 0, 0, 0)
        back, _, _ = IM.dib_to_rgba(header + bytes(palette)
                                    + bytes((0b10000000,)) + b"\x00\x00\x00")
        self.assertEqual(back[0:4], bytes((9, 9, 9, 255)))

    def test_4bpp_palette(self):
        palette = bytearray(16 * 4)
        for i in range(16):
            palette[i * 4:i * 4 + 4] = bytes((i * 16, i * 16, i * 16, 0))
        header = struct.pack("<IiiHHIIiiII", 40, 4, 1, 1, 4, 0, 4, 0, 0, 16, 0)
        data = bytes((0x12, 0xF0)) + b"\x00\x00"      # indices 1,2,15,0
        back, w, h = IM.dib_to_rgba(header + bytes(palette) + data)
        self.assertEqual((w, h), (4, 1))
        self.assertEqual(list(back[0::4]), [16, 32, 240, 0])

    def test_16bpp_555(self):
        header = struct.pack("<IiiHHIIiiII", 40, 2, 1, 1, 16, 0, 4, 0, 0, 0, 0)
        # 0x7C00 = pure red, 0x001F = pure blue
        pixels = struct.pack("<HH", 0x7C00, 0x001F)
        back, w, h = IM.dib_to_rgba(header + pixels)
        self.assertEqual(back[0:4], bytes((255, 0, 0, 255)))
        self.assertEqual(back[4:8], bytes((0, 0, 255, 255)))

    def test_bitfields_masks(self):
        """32bpp with explicit masks (BI_BITFIELDS): the channel order must not be
        taken for granted.

        Only 3 masks are given here (no alpha), so the fourth byte must be ignored:
        alpha can only be 255, and 0x80 must not be read as opacity -- that would
        make the image half transparent.
        """
        w, h = 2, 1
        # masks: R=0x000000FF G=0x0000FF00 B=0x00FF0000
        header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 3,
                             w * 4 * h, 0, 0, 0, 0)
        masks = struct.pack("<III", 0x000000FF, 0x0000FF00, 0x00FF0000)
        px = struct.pack("<II", 0xFF0000FF, 0x8000FF00)   # pure red; high bits not covered by any mask = 0x80
        back, bw, bh = IM.dib_to_rgba(header + masks + px)
        self.assertEqual(back[0:4], bytes((255, 0, 0, 255)))
        self.assertEqual(back[4:8], bytes((0, 255, 0, 255)))

    def test_v5_header_with_alpha_mask(self):
        w, h = 1, 1
        # BITMAPV5HEADER (124 bytes), with the masks inside the header
        header = struct.pack("<IiiHHIIiiII", 124, w, h, 1, 32, 3,
                             w * 4 * h, 0, 0, 0, 0)
        header += struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF,
                              0xFF000000)
        header += b"\x00" * (124 - len(header))
        px = struct.pack("<I", 0x80402010)          # A=0x80 R=0x40 G=0x20 B=0x10
        back, _, _ = IM.dib_to_rgba(header + px)
        self.assertEqual(back[0:4], bytes((0x40, 0x20, 0x10, 0x80)))

    def test_core_header(self):
        header = struct.pack("<IHHHH", 12, 2, 1, 1, 24)
        px = bytes((3, 2, 1, 6, 5, 4)) + b"\x00\x00"    # two BGR pixels + padding
        back, w, h = IM.dib_to_rgba(header + px)
        self.assertEqual((w, h), (2, 1))
        self.assertEqual(back[0:4], bytes((1, 2, 3, 255)))
        self.assertEqual(back[4:8], bytes((4, 5, 6, 255)))

    def test_rejects_short_and_unknown(self):
        with self.assertRaises(IM.ImageError):
            IM.dib_to_rgba(b"\x01\x02")
        with self.assertRaises(IM.ImageError):
            IM.dib_to_rgba(struct.pack("<I", 28) + b"\x00" * 24)

    def test_rejects_truncated_pixels(self):
        header = struct.pack("<IiiHHIIiiII", 40, 100, 100, 1, 24, 0, 0, 0, 0, 0, 0)
        with self.assertRaises(IM.ImageError):
            IM.dib_to_rgba(header + b"\x00" * 16)

    def test_pixel_cap(self):
        header = struct.pack("<IiiHHIIiiII", 40, 2000, 2000, 1, 24, 0, 0, 0, 0, 0, 0)
        with self.assertRaises(IM.ImageError):
            IM.dib_to_rgba(header + b"\x00" * 100, pixel_cap=1000)

    def test_rejects_bad_bpp_on_encode(self):
        with self.assertRaises(IM.ImageError):
            IM.rgba_to_dib(b"\x00" * 16, 2, 2, bpp=8)


# ===========================================================================
class TestBridges(unittest.TestCase):
    def test_dib_to_png_to_dib(self):
        # seed=3 keeps the alpha channel from being all zero: all-zero alpha cannot
        # be told apart from "no alpha" and gets treated as opaque (an intentional
        # compatibility behaviour, tested separately below)
        for (w, h) in ((1, 1), (5, 4), (40, 30)):
            rgba = rgba_bytes(w, h, seed=3)
            dib = IM.rgba_to_dib(rgba, w, h, bpp=32)
            png = IM.png_from_dib(dib)
            self.assertEqual(IM.png_size(png), (w, h))
            dib2 = IM.dib_from_png(png)
            back, bw, bh = IM.dib_to_rgba(dib2)
            self.assertEqual((bw, bh), (w, h))
            self.assertEqual(back, rgba, "%dx%d bridge roundtrip mismatch" % (w, h))

    def test_opaque_image_uses_24bpp(self):
        w, h = 4, 4
        rgba = rgba_bytes(w, h)
        rgba = bytes(rgba[0::4]) + b"" if False else bytes(
            b for i in range(0, len(rgba), 4) for b in (rgba[i], rgba[i + 1],
                                                        rgba[i + 2], 255))
        png = IM.encode_png(rgba, w, h)
        dib = IM.dib_from_png(png)
        self.assertEqual(struct.unpack_from("<H", dib, 14)[0], 24)

    def test_transparent_image_uses_32bpp(self):
        w, h = 2, 2
        rgba = rgba_bytes(w, h)
        png = IM.encode_png(rgba, w, h)             # seeded data: alpha is not all 255
        dib = IM.dib_from_png(png)
        self.assertEqual(struct.unpack_from("<H", dib, 14)[0], 32)

    def test_png_to_dib_truncated_png_raises(self):
        with self.assertRaises(IM.ImageError):
            IM.dib_from_png(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)


if __name__ == "__main__":
    unittest.main()
