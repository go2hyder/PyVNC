"""RFB (VNC) protocol constants and message helpers shared by server and client.

Reference: RFC 6143 and the community RFB spec (rfbproto).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

PROTOCOL_VERSION = b"RFB 003.008\n"

# Security types
SEC_INVALID = 0
SEC_NONE = 1
SEC_VNC_AUTH = 2
SEC_VENCRYPT = 19

# VeNCrypt sub-types
VENCRYPT_PLAIN = 256
VENCRYPT_TLS_NONE = 257
VENCRYPT_TLS_VNC = 258
VENCRYPT_TLS_PLAIN = 259
VENCRYPT_X509_NONE = 260
VENCRYPT_X509_VNC = 261
VENCRYPT_X509_PLAIN = 262

# Client -> server message types
MSG_SET_PIXEL_FORMAT = 0
MSG_SET_ENCODINGS = 2
MSG_FB_UPDATE_REQUEST = 3
MSG_KEY_EVENT = 4
MSG_POINTER_EVENT = 5
MSG_CLIENT_CUT_TEXT = 6
MSG_CLIENT_FILES = 7

# Server -> client message types
MSG_FB_UPDATE = 0
MSG_SET_COLOUR_MAP = 1
MSG_BELL = 2
MSG_SERVER_CUT_TEXT = 3
MSG_SERVER_FILES = 4

# Encodings
ENC_RAW = 0
ENC_COPYRECT = 1
ENC_ZLIB = 6
ENC_TIGHT = 7

# Pseudo-encodings
ENC_DESKTOP_SIZE = -223
ENC_LAST_RECT = -224
ENC_CURSOR = -239
ENC_JPEG_QUALITY_0 = -32  # -32 .. -23 => quality level 0..9
ENC_COMPRESS_LEVEL_0 = -256  # -256 .. -247 => compression level 0..9
ENC_FINE_QUALITY_0 = -512  # TigerVNC: -512 .. -412 => JPEG quality 0..100
ENC_PYVNC_FILES = -307  # PyVNC file clipboard; ignored by other VNC viewers

# File clipboard ops carried by MSG_CLIENT_FILES / MSG_SERVER_FILES
FILE_CAPS = 0
FILE_BEGIN = 1
FILE_DATA = 2
FILE_END = 3
FILE_ABORT = 4
FILE_CHUNK = 64 * 1024
FILE_MAX_BYTES = 128 * 1024 * 1024

# Tight compression-control values
TIGHT_FILL = 0x08
TIGHT_JPEG = 0x09
TIGHT_MAX_SUBENCODING = 0x09
TIGHT_EXPLICIT_FILTER = 0x04
TIGHT_FILTER_COPY = 0
TIGHT_FILTER_PALETTE = 1
TIGHT_FILTER_GRADIENT = 2
TIGHT_MIN_TO_COMPRESS = 12

# JPEG quality used for each Tight quality level (matches TigerVNC's table).
JPEG_QUALITY_FOR_LEVEL = [15, 29, 41, 42, 62, 77, 79, 86, 92, 100]

# Button mask bits for PointerEvent
BTN_LEFT = 1 << 0
BTN_MIDDLE = 1 << 1
BTN_RIGHT = 1 << 2
BTN_WHEEL_UP = 1 << 3
BTN_WHEEL_DOWN = 1 << 4
BTN_WHEEL_LEFT = 1 << 5
BTN_WHEEL_RIGHT = 1 << 6


@dataclass(frozen=True)
class PixelFormat:
    bits_per_pixel: int = 32
    depth: int = 24
    big_endian: bool = False
    true_colour: bool = True
    red_max: int = 255
    green_max: int = 255
    blue_max: int = 255
    red_shift: int = 16
    green_shift: int = 8
    blue_shift: int = 0

    _STRUCT = struct.Struct(">BBBBHHHBBB3x")

    def pack(self) -> bytes:
        return self._STRUCT.pack(
            self.bits_per_pixel, self.depth, int(self.big_endian), int(self.true_colour),
            self.red_max, self.green_max, self.blue_max,
            self.red_shift, self.green_shift, self.blue_shift,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "PixelFormat":
        bpp, depth, be, tc, rmax, gmax, bmax, rs, gs, bs = cls._STRUCT.unpack(data)
        return cls(bpp, depth, bool(be), bool(tc), rmax, gmax, bmax, rs, gs, bs)

    @property
    def bytes_per_pixel(self) -> int:
        return self.bits_per_pixel // 8

    @property
    def is_native_bgrx(self) -> bool:
        """True when pixels are laid out in memory as B, G, R, X bytes."""
        if self.bits_per_pixel != 32 or not self.true_colour:
            return False
        if (self.red_max, self.green_max, self.blue_max) != (255, 255, 255):
            return False
        if self.big_endian:
            return (self.red_shift, self.green_shift, self.blue_shift) == (8, 16, 24)
        return (self.red_shift, self.green_shift, self.blue_shift) == (16, 8, 0)

    @property
    def supports_tpixel(self) -> bool:
        """Tight sends 3-byte RGB pixels when this holds (see rfbproto, Tight)."""
        return (
            self.true_colour and self.bits_per_pixel == 32 and self.depth == 24
            and (self.red_max, self.green_max, self.blue_max) == (255, 255, 255)
        )

    def _dtype(self) -> np.dtype:
        base = {8: np.uint8, 16: np.uint16, 32: np.uint32}[self.bits_per_pixel]
        return np.dtype(base).newbyteorder(">" if self.big_endian else "<")

    def from_bgrx(self, bgrx: np.ndarray) -> bytes:
        """Convert an (H, W, 4) uint8 B,G,R,X array into this pixel format."""
        if self.is_native_bgrx and not self.big_endian:
            return np.ascontiguousarray(bgrx).tobytes()
        b = bgrx[..., 0].astype(np.uint32)
        g = bgrx[..., 1].astype(np.uint32)
        r = bgrx[..., 2].astype(np.uint32)
        if self.red_max != 255:
            r = r * self.red_max // 255
        if self.green_max != 255:
            g = g * self.green_max // 255
        if self.blue_max != 255:
            b = b * self.blue_max // 255
        pix = (r << self.red_shift) | (g << self.green_shift) | (b << self.blue_shift)
        return pix.astype(self._dtype()).tobytes()

    def to_bgrx(self, data: bytes, width: int, height: int) -> np.ndarray:
        """Convert raw pixel bytes in this format into an (H, W, 4) B,G,R,X array."""
        if self.is_native_bgrx and not self.big_endian:
            arr = np.frombuffer(data, dtype=np.uint8, count=width * height * 4)
            return arr.reshape(height, width, 4)
        pix = np.frombuffer(data, dtype=self._dtype(), count=width * height)
        pix = pix.astype(np.uint32).reshape(height, width)
        out = np.empty((height, width, 4), dtype=np.uint8)
        out[..., 2] = ((pix >> self.red_shift) & self.red_max) * 255 // max(self.red_max, 1)
        out[..., 1] = ((pix >> self.green_shift) & self.green_max) * 255 // max(self.green_max, 1)
        out[..., 0] = ((pix >> self.blue_shift) & self.blue_max) * 255 // max(self.blue_max, 1)
        out[..., 3] = 255
        return out


NATIVE_PIXEL_FORMAT = PixelFormat()


def pack_server_init(width: int, height: int, pf: PixelFormat, name: str) -> bytes:
    name_b = name.encode("utf-8")
    return struct.pack(">HH", width, height) + pf.pack() + struct.pack(">I", len(name_b)) + name_b


def pack_rect_header(x: int, y: int, w: int, h: int, encoding: int) -> bytes:
    return struct.pack(">HHHHi", x, y, w, h, encoding)


def pack_fb_update_header(num_rects: int) -> bytes:
    return struct.pack(">BxH", MSG_FB_UPDATE, num_rects)


def pack_cut_text(msg_type: int, text: str) -> bytes:
    data = text.replace("\r\n", "\n").encode("latin-1", errors="replace")
    return struct.pack(">B3xI", msg_type, len(data)) + data


def pack_tight_compact_length(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    if n > 0x7F:
        out[0] |= 0x80
        out.append((n >> 7) & 0x7F)
        if n > 0x3FFF:
            out[1] |= 0x80
            out.append((n >> 14) & 0xFF)
    return bytes(out)


def pack_security_result(ok: bool, reason: str = "") -> bytes:
    if ok:
        return struct.pack(">I", 0)
    r = reason.encode("utf-8")
    return struct.pack(">II", 1, len(r)) + r


def jpeg_quality_from_encodings(encodings: list[int]) -> int | None:
    """Return the JPEG quality (0-100) requested by the client, if any."""
    for enc in encodings:
        if ENC_FINE_QUALITY_0 <= enc <= ENC_FINE_QUALITY_0 + 100:
            return enc - ENC_FINE_QUALITY_0
    for enc in encodings:
        if ENC_JPEG_QUALITY_0 <= enc <= ENC_JPEG_QUALITY_0 + 9:
            return JPEG_QUALITY_FOR_LEVEL[enc - ENC_JPEG_QUALITY_0]
    return None


def compress_level_from_encodings(encodings: list[int]) -> int | None:
    for enc in encodings:
        if ENC_COMPRESS_LEVEL_0 <= enc <= ENC_COMPRESS_LEVEL_0 + 9:
            return enc - ENC_COMPRESS_LEVEL_0
    return None


# X11 keysyms used by both sides
XK_BackSpace = 0xFF08
XK_Tab = 0xFF09
XK_Return = 0xFF0D
XK_Escape = 0xFF1B
XK_Delete = 0xFFFF
XK_Home = 0xFF50
XK_Left = 0xFF51
XK_Up = 0xFF52
XK_Right = 0xFF53
XK_Down = 0xFF54
XK_Page_Up = 0xFF55
XK_Page_Down = 0xFF56
XK_End = 0xFF57
XK_Insert = 0xFF63
XK_Menu = 0xFF67
XK_Help = 0xFF6A
XK_Num_Lock = 0xFF7F
XK_KP_Enter = 0xFF8D
XK_KP_Multiply = 0xFFAA
XK_KP_Add = 0xFFAB
XK_KP_Subtract = 0xFFAD
XK_KP_Decimal = 0xFFAE
XK_KP_Divide = 0xFFAF
XK_KP_0 = 0xFFB0
XK_KP_Equal = 0xFFBD
XK_F1 = 0xFFBE  # F1..F20 are consecutive
XK_Shift_L = 0xFFE1
XK_Shift_R = 0xFFE2
XK_Control_L = 0xFFE3
XK_Control_R = 0xFFE4
XK_Caps_Lock = 0xFFE5
XK_Meta_L = 0xFFE7
XK_Meta_R = 0xFFE8
XK_Alt_L = 0xFFE9
XK_Alt_R = 0xFFEA
XK_Super_L = 0xFFEB
XK_Super_R = 0xFFEC
XK_ISO_Level3_Shift = 0xFE03
XK_Print = 0xFF61
XK_Pause = 0xFF13
XK_Scroll_Lock = 0xFF14


def keysym_to_char(keysym: int) -> str | None:
    """Return the printable character for a keysym, or None for function keys."""
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if keysym & 0xFF000000 == 0x01000000:
        return chr(keysym & 0x00FFFFFF)
    return None


def char_to_keysym(ch: str) -> int:
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp
