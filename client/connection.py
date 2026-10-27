"""RFB client: protocol handling and decoding, running an asyncio loop on a worker thread.

All socket I/O happens on the worker thread (TLS objects are not safe to share
between threads); the GUI calls the thread-safe send_* methods.
"""
from __future__ import annotations

import asyncio
import io
import logging
import struct
import threading
import zlib
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image

from common import rfb
from common.security import KnownHosts, cert_fingerprint, client_tls_context, vnc_auth_response

log = logging.getLogger("pyvnc.client")

# quality level -> None means lossless (no JPEG)
QUALITY_PRESETS = {"Low (fast)": 2, "Medium": 5, "High": 7, "Best": 9, "Lossless": None}


class ConnectError(Exception):
    pass


@dataclass
class ConnectOptions:
    host: str
    port: int = 5900
    password: str = ""
    username: str = "pyvnc"
    quality_level: int | None = 6
    compress_level: int = 6
    allow_insecure: bool = False


@dataclass
class Callbacks:
    """Invoked from the worker thread."""
    on_connected: Callable[[str, np.ndarray], None] = lambda name, fb: None
    on_resize: Callable[[np.ndarray], None] = lambda fb: None
    on_update: Callable[[int, int, int, int], None] = lambda x, y, w, h: None
    on_clipboard: Callable[[str], None] = lambda text: None
    on_bell: Callable[[], None] = lambda: None
    on_closed: Callable[[str | None], None] = lambda error: None
    # (host_port, fingerprint, previously_known_fingerprint) -> accept?
    confirm_certificate: Callable[[str, str, str | None], bool] = lambda hp, fp, old: False
    on_security: Callable[[str], None] = lambda description: None


class RFBClient:
    def __init__(self, options: ConnectOptions, callbacks: Callbacks):
        self.opts = options
        self.cb = callbacks
        self.fb = np.zeros((1, 1, 4), dtype=np.uint8)
        self.pf = rfb.NATIVE_PIXEL_FORMAT
        self.name = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._zlib = zlib.decompressobj()
        self._tight_streams = [zlib.decompressobj() for _ in range(4)]
        self._closing = False

    # -- thread management -------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="rfb-client", daemon=True)
        self._thread.start()

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        error = None
        try:
            self._loop.run_until_complete(self._run())
        except ConnectError as exc:
            error = str(exc)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            error = None if self._closing else f"Connection lost ({exc.__class__.__name__})"
        except OSError as exc:
            error = None if self._closing else f"Network error: {exc}"
        except Exception as exc:  # report anything unexpected to the user
            log.exception("client error")
            error = None if self._closing else f"Error: {exc}"
        finally:
            self._loop.close()
        self.cb.on_closed(error)

    def close(self) -> None:
        self._closing = True
        if self._loop is not None and self._writer is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._writer.close)
            except RuntimeError:
                pass

    def _send_threadsafe(self, data: bytes) -> None:
        loop, writer = self._loop, self._writer
        if loop is None or writer is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(writer.write, data)
        except RuntimeError:
            pass

    def send_key(self, keysym: int, down: bool) -> None:
        self._send_threadsafe(struct.pack(">BBxxI", rfb.MSG_KEY_EVENT, int(down), keysym))

    def send_pointer(self, mask: int, x: int, y: int) -> None:
        h, w = self.fb.shape[:2]
        x = max(0, min(int(x), w - 1))
        y = max(0, min(int(y), h - 1))
        self._send_threadsafe(struct.pack(">BBHH", rfb.MSG_POINTER_EVENT, mask, x, y))

    def send_clipboard(self, text: str) -> None:
        self._send_threadsafe(rfb.pack_cut_text(rfb.MSG_CLIENT_CUT_TEXT, text))

    # -- protocol ----------------------------------------------------------

    async def _read(self, n: int) -> bytes:
        return await self._reader.readexactly(n)

    async def _send(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def _run(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.opts.host, self.opts.port), timeout=15)
        except asyncio.TimeoutError:
            raise ConnectError(f"Timed out connecting to {self.opts.host}:{self.opts.port}")
        except OSError as exc:
            raise ConnectError(f"Cannot connect to {self.opts.host}:{self.opts.port}: {exc.strerror or exc}")

        await self._handshake()
        await self._client_init()
        self.cb.on_connected(self.name, self.fb)
        await self._send(self._set_pixel_format() + self._set_encodings() + self._update_request(False))
        await self._message_loop()

    async def _handshake(self) -> None:
        version = await self._read(12)
        if not version.startswith(b"RFB "):
            raise ConnectError("Not a VNC server")
        major, minor = int(version[4:7]), int(version[8:11])
        if major != 3:
            raise ConnectError(f"Unsupported protocol {version!r}")
        minor = 8 if minor >= 8 else (7 if minor == 7 else 3)
        await self._send(f"RFB 003.00{minor}\n".encode())

        if minor == 3:
            (sec_type,) = struct.unpack(">I", await self._read(4))
            types = [sec_type] if sec_type else []
            if not types:
                raise ConnectError(await self._read_reason())
            chosen = self._pick_security(types)
        else:
            count = (await self._read(1))[0]
            if count == 0:
                raise ConnectError(await self._read_reason())
            types = list(await self._read(count))
            chosen = self._pick_security(types)
            await self._send(bytes([chosen]))

        if chosen == rfb.SEC_VENCRYPT:
            await self._vencrypt()
        elif chosen == rfb.SEC_VNC_AUTH:
            self.cb.on_security("Unencrypted (VNC password auth)")
            await self._vnc_auth()
        else:
            self.cb.on_security("Unencrypted, no password")
            if minor < 8:
                return
        await self._security_result(minor)

    def _pick_security(self, types: list[int]) -> int:
        if rfb.SEC_VENCRYPT in types:
            return rfb.SEC_VENCRYPT
        if self.opts.allow_insecure:
            for t in (rfb.SEC_VNC_AUTH, rfb.SEC_NONE):
                if t in types:
                    return t
        raise ConnectError(
            "The server does not offer an encrypted connection (VeNCrypt/TLS). "
            f"Offered security types: {types}. Enable 'Allow unencrypted' only on a trusted LAN.")

    async def _read_reason(self) -> str:
        (length,) = struct.unpack(">I", await self._read(4))
        return (await self._read(length)).decode("utf-8", errors="replace")

    async def _vencrypt(self) -> None:
        major, minor = await self._read(2)
        if (major, minor) < (0, 2):
            raise ConnectError("Server VeNCrypt version too old")
        await self._send(b"\x00\x02")
        if (await self._read(1))[0] != 0:
            raise ConnectError("Server rejected VeNCrypt 0.2")
        count = (await self._read(1))[0]
        subtypes = list(struct.unpack(f">{count}I", await self._read(4 * count)))
        for preferred in (rfb.VENCRYPT_X509_PLAIN, rfb.VENCRYPT_X509_VNC, rfb.VENCRYPT_X509_NONE):
            if preferred in subtypes:
                subtype = preferred
                break
        else:
            raise ConnectError(f"No supported TLS mode offered (server offers {subtypes})")
        await self._send(struct.pack(">I", subtype))
        if (await self._read(1))[0] != 1:
            raise ConnectError("Server rejected the TLS mode")

        await self._writer.start_tls(client_tls_context())
        await self._verify_certificate()

        if subtype == rfb.VENCRYPT_X509_PLAIN:
            user = self.opts.username.encode("utf-8")
            pw = self.opts.password.encode("utf-8")
            await self._send(struct.pack(">II", len(user), len(pw)) + user + pw)
            self.cb.on_security("TLS encrypted (full-length password)")
        elif subtype == rfb.VENCRYPT_X509_VNC:
            await self._vnc_auth()
            self.cb.on_security("TLS encrypted (VNC password, 8 chars)")
        else:
            self.cb.on_security("TLS encrypted (no password)")

    async def _verify_certificate(self) -> None:
        ssl_obj = self._writer.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True) if ssl_obj else None
        if not der:
            raise ConnectError("Server did not present a certificate")
        fingerprint = cert_fingerprint(der)
        hosts = KnownHosts()
        known = hosts.get(self.opts.host, self.opts.port)
        if known == fingerprint:
            return
        host_port = f"{self.opts.host}:{self.opts.port}"
        accepted = await asyncio.get_running_loop().run_in_executor(
            None, self.cb.confirm_certificate, host_port, fingerprint, known)
        if not accepted:
            raise ConnectError("Certificate not trusted; connection cancelled")
        hosts.set(self.opts.host, self.opts.port, fingerprint)

    async def _vnc_auth(self) -> None:
        challenge = await self._read(16)
        await self._send(vnc_auth_response(self.opts.password, challenge))

    async def _security_result(self, minor: int) -> None:
        (result,) = struct.unpack(">I", await self._read(4))
        if result == 0:
            return
        reason = await self._read_reason() if minor >= 8 else "Authentication failed"
        raise ConnectError(f"Authentication failed: {reason}")

    async def _client_init(self) -> None:
        await self._send(b"\x01")
        w, h = struct.unpack(">HH", await self._read(4))
        rfb.PixelFormat.unpack(await self._read(16))
        (name_len,) = struct.unpack(">I", await self._read(4))
        self.name = (await self._read(name_len)).decode("utf-8", errors="replace")
        self.fb = np.zeros((h, w, 4), dtype=np.uint8)
        self.fb[..., 3] = 255

    def _set_pixel_format(self) -> bytes:
        return struct.pack(">B3x", rfb.MSG_SET_PIXEL_FORMAT) + self.pf.pack()

    def _set_encodings(self) -> bytes:
        encs = [rfb.ENC_TIGHT, rfb.ENC_ZLIB, rfb.ENC_COPYRECT, rfb.ENC_RAW,
                rfb.ENC_DESKTOP_SIZE, rfb.ENC_LAST_RECT,
                rfb.ENC_COMPRESS_LEVEL_0 + self.opts.compress_level]
        if self.opts.quality_level is not None:
            encs.append(rfb.ENC_JPEG_QUALITY_0 + self.opts.quality_level)
        return struct.pack(f">BxH{len(encs)}i", rfb.MSG_SET_ENCODINGS, len(encs), *encs)

    def _update_request(self, incremental: bool) -> bytes:
        h, w = self.fb.shape[:2]
        return struct.pack(">BBHHHH", rfb.MSG_FB_UPDATE_REQUEST, int(incremental), 0, 0, w, h)

    async def _message_loop(self) -> None:
        while True:
            msg_type = (await self._read(1))[0]
            if msg_type == rfb.MSG_FB_UPDATE:
                incremental = await self._framebuffer_update()
                await self._send(self._update_request(incremental))
            elif msg_type == rfb.MSG_SET_COLOUR_MAP:
                _, _, count = struct.unpack(">xHH", await self._read(5))
                await self._read(6 * count)
            elif msg_type == rfb.MSG_BELL:
                self.cb.on_bell()
            elif msg_type == rfb.MSG_SERVER_CUT_TEXT:
                (length,) = struct.unpack(">3xI", await self._read(7))
                self.cb.on_clipboard((await self._read(length)).decode("latin-1"))
            else:
                raise ConnectError(f"Unknown server message {msg_type}")

    # -- decoding ----------------------------------------------------------

    async def _framebuffer_update(self) -> bool:
        """Decode one update. Returns False if the next request must be non-incremental."""
        (count,) = struct.unpack(">xH", await self._read(3))
        dirty = None
        incremental = True
        for _ in range(count):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self._read(12))
            if enc == rfb.ENC_LAST_RECT:
                break
            if enc == rfb.ENC_DESKTOP_SIZE:
                self.fb = np.zeros((h, w, 4), dtype=np.uint8)
                self.fb[..., 3] = 255
                self.cb.on_resize(self.fb)
                incremental = False
                continue
            if enc == rfb.ENC_RAW:
                data = await self._read(w * h * self.pf.bytes_per_pixel)
                self._put(x, y, self.pf.to_bgrx(data, w, h))
            elif enc == rfb.ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", await self._read(4))
                self.fb[y:y + h, x:x + w] = self.fb[sy:sy + h, sx:sx + w].copy()
            elif enc == rfb.ENC_ZLIB:
                (length,) = struct.unpack(">I", await self._read(4))
                data = self._zlib.decompress(await self._read(length))
                self._put(x, y, self.pf.to_bgrx(data, w, h))
            elif enc == rfb.ENC_TIGHT:
                await self._tight(x, y, w, h)
            else:
                raise ConnectError(f"Server used unsupported encoding {enc}")
            dirty = (x, y, x + w, y + h) if dirty is None else (
                min(dirty[0], x), min(dirty[1], y), max(dirty[2], x + w), max(dirty[3], y + h))
        if dirty is not None:
            self.cb.on_update(dirty[0], dirty[1], dirty[2] - dirty[0], dirty[3] - dirty[1])
        return incremental

    def _put(self, x: int, y: int, pixels: np.ndarray) -> None:
        h, w = pixels.shape[:2]
        self.fb[y:y + h, x:x + w, :3] = pixels[..., :3]

    async def _compact_length(self) -> int:
        b = (await self._read(1))[0]
        n = b & 0x7F
        if b & 0x80:
            b = (await self._read(1))[0]
            n |= (b & 0x7F) << 7
            if b & 0x80:
                n |= (await self._read(1))[0] << 14
        return n

    async def _tight(self, x: int, y: int, w: int, h: int) -> None:
        ctl = (await self._read(1))[0]
        for i in range(4):
            if ctl & (1 << i):
                self._tight_streams[i] = zlib.decompressobj()
        kind = ctl >> 4

        if kind == rfb.TIGHT_FILL:
            r, g, b = await self._read(3)
            self.fb[y:y + h, x:x + w, 0] = b
            self.fb[y:y + h, x:x + w, 1] = g
            self.fb[y:y + h, x:x + w, 2] = r
            return
        if kind == rfb.TIGHT_JPEG:
            data = await self._read(await self._compact_length())
            img = Image.open(io.BytesIO(data)).convert("RGB")
            rgb = np.asarray(img)
            self.fb[y:y + h, x:x + w, 0] = rgb[..., 2]
            self.fb[y:y + h, x:x + w, 1] = rgb[..., 1]
            self.fb[y:y + h, x:x + w, 2] = rgb[..., 0]
            return
        if kind > rfb.TIGHT_MAX_SUBENCODING:
            raise ConnectError(f"Bad Tight subencoding {kind}")

        stream = self._tight_streams[kind & 0x03]
        filter_id = (await self._read(1))[0] if kind & rfb.TIGHT_EXPLICIT_FILTER else rfb.TIGHT_FILTER_COPY

        if filter_id == rfb.TIGHT_FILTER_PALETTE:
            colours = (await self._read(1))[0] + 1
            palette = np.frombuffer(await self._read(3 * colours), dtype=np.uint8).reshape(colours, 3)
            size = ((w + 7) // 8) * h if colours == 2 else w * h
            data = await self._tight_data(stream, size)
            if colours == 2:
                bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8).reshape(h, (w + 7) // 8), axis=1)
                indices = bits[:, :w]
            else:
                indices = np.frombuffer(data, dtype=np.uint8).reshape(h, w)
            rgb = palette[indices]
        elif filter_id == rfb.TIGHT_FILTER_GRADIENT:
            data = await self._tight_data(stream, w * h * 3)
            rgb = _undo_gradient(np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3))
        elif filter_id == rfb.TIGHT_FILTER_COPY:
            data = await self._tight_data(stream, w * h * 3)
            rgb = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3)
        else:
            raise ConnectError(f"Bad Tight filter {filter_id}")

        self.fb[y:y + h, x:x + w, 0] = rgb[..., 2]
        self.fb[y:y + h, x:x + w, 1] = rgb[..., 1]
        self.fb[y:y + h, x:x + w, 2] = rgb[..., 0]

    async def _tight_data(self, stream, size: int) -> bytes:
        if size < rfb.TIGHT_MIN_TO_COMPRESS:
            return await self._read(size)
        compressed = await self._read(await self._compact_length())
        data = stream.decompress(compressed)
        if len(data) < size:
            raise ConnectError("Short Tight data")
        return data[:size]


def _undo_gradient(diff: np.ndarray) -> np.ndarray:
    """Reverse the Tight gradient filter: pixel = diff + clamp(left + up - upleft)."""
    h, w, _ = diff.shape
    out = np.zeros((h, w, 3), dtype=np.int32)
    d = diff.astype(np.int32)
    for y in range(h):
        up = out[y - 1] if y else np.zeros((w, 3), dtype=np.int32)
        row = out[y]
        left = np.zeros(3, dtype=np.int32)
        upleft = np.zeros(3, dtype=np.int32)
        for x in range(w):
            pred = np.clip(left + up[x] - upleft, 0, 255)
            row[x] = (pred + d[y, x]) & 0xFF
            left = row[x]
            upleft = up[x]
    return out.astype(np.uint8)
