"""Framebuffer rectangle encoders: Raw, Zlib and Tight (fill + JPEG)."""
from __future__ import annotations

import io
import struct
import zlib

import numpy as np
from PIL import Image

from common import rfb


class Encoder:
    """Per-connection encoder. Zlib keeps one stream for the whole connection."""

    def __init__(self, forced_quality: int | None = None):
        self.forced_quality = forced_quality
        self.pixel_format = rfb.NATIVE_PIXEL_FORMAT
        self.encodings: list[int] = []
        self.jpeg_quality: int | None = None
        self.zlib_level = 6
        self._zlib: zlib._Compress | None = None

    def configure(self, encodings: list[int] | None = None, pixel_format: rfb.PixelFormat | None = None) -> None:
        if encodings is not None:
            self.encodings = encodings
            self.jpeg_quality = rfb.jpeg_quality_from_encodings(encodings)
            level = rfb.compress_level_from_encodings(encodings)
            if level is not None:
                self.zlib_level = max(1, level)
        if pixel_format is not None:
            self.pixel_format = pixel_format

    def supports(self, encoding: int) -> bool:
        return encoding in self.encodings

    @property
    def tight_jpeg_quality(self) -> int | None:
        if self.jpeg_quality is None:
            return None
        return self.forced_quality if self.forced_quality is not None else self.jpeg_quality

    def _choose(self) -> int:
        for enc in self.encodings:
            if enc == rfb.ENC_TIGHT and self.pixel_format.supports_tpixel and self.tight_jpeg_quality is not None:
                return rfb.ENC_TIGHT
            if enc == rfb.ENC_ZLIB:
                return rfb.ENC_ZLIB
            if enc == rfb.ENC_RAW:
                return rfb.ENC_RAW
        return rfb.ENC_RAW

    def encode(self, frame: np.ndarray, rects: list[tuple[int, int, int, int]]) -> bytes:
        """Encode a full FramebufferUpdate message for the given rectangles."""
        enc = self._choose()
        out = [rfb.pack_fb_update_header(len(rects))]
        for x, y, w, h in rects:
            region = frame[y:y + h, x:x + w]
            if enc == rfb.ENC_TIGHT:
                out.append(self._tight(region, x, y, w, h))
            elif enc == rfb.ENC_ZLIB:
                out.append(self._zlib_rect(region, x, y, w, h))
            else:
                out.append(rfb.pack_rect_header(x, y, w, h, rfb.ENC_RAW))
                out.append(self.pixel_format.from_bgrx(region))
        return b"".join(out)

    def _zlib_rect(self, region: np.ndarray, x: int, y: int, w: int, h: int) -> bytes:
        if self._zlib is None:
            self._zlib = zlib.compressobj(self.zlib_level)
        data = self._zlib.compress(self.pixel_format.from_bgrx(region)) + self._zlib.flush(zlib.Z_SYNC_FLUSH)
        return rfb.pack_rect_header(x, y, w, h, rfb.ENC_ZLIB) + struct.pack(">I", len(data)) + data

    def _tight(self, region: np.ndarray, x: int, y: int, w: int, h: int) -> bytes:
        header = rfb.pack_rect_header(x, y, w, h, rfb.ENC_TIGHT)
        flat = region.view(np.uint32)[..., 0]
        first = flat[0, 0]
        if (flat == first).all():
            b, g, r = int(region[0, 0, 0]), int(region[0, 0, 1]), int(region[0, 0, 2])
            return header + bytes([rfb.TIGHT_FILL << 4, r, g, b])

        img = Image.frombytes("RGB", (w, h), np.ascontiguousarray(region).tobytes(), "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=max(1, min(self.tight_jpeg_quality, 95)))
        data = buf.getvalue()
        return header + bytes([rfb.TIGHT_JPEG << 4]) + rfb.pack_tight_compact_length(len(data)) + data


def desktop_size_update(width: int, height: int) -> bytes:
    return rfb.pack_fb_update_header(1) + rfb.pack_rect_header(0, 0, width, height, rfb.ENC_DESKTOP_SIZE)
