"""One RFB client connection: handshake, authentication and the message loop."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import socket
import ssl
import struct
import textwrap
import time
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from common import rfb
from common.security import vnc_auth_response
from common.transfer import (
    IncomingFile, pack_file_abort, pack_file_caps, read_file_body, receive_dir, transmit_files,
)
from server.capture import ScreenCapture, dirty_rects
from server.encoder import Encoder, desktop_size_update

log = logging.getLogger("pyvnc.session")

MAX_CUT_TEXT = 10 * 1024 * 1024


class ProtocolError(Exception):
    pass


class AuthFailed(Exception):
    pass


@dataclass
class ServerConfig:
    password: str
    ssl_context: ssl.SSLContext | None
    allow_insecure: bool
    fps: float
    forced_quality: int | None
    view_only: bool
    name: str
    file_dir: Path | None = None


class AuthGuard:
    """Slows down and then blocks password guessing from a single address."""

    MAX_FAILURES = 5
    WINDOW = 15 * 60

    def __init__(self):
        self._failures: dict[str, list[float]] = {}

    def _recent(self, ip: str) -> list[float]:
        now = time.monotonic()
        recent = [t for t in self._failures.get(ip, []) if now - t < self.WINDOW]
        self._failures[ip] = recent
        return recent

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.MAX_FAILURES

    def failed(self, ip: str) -> None:
        self._recent(ip).append(time.monotonic())

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)


class SessionEvents:
    """Hooks called on the server's event loop. Override what you need."""

    def session_started(self, session: "Session") -> None:
        pass

    def session_connected(self, session: "Session") -> None:
        pass

    def session_closed(self, session: "Session") -> None:
        pass

    def auth_failed(self, session: "Session") -> None:
        pass


class Session:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 config: ServerConfig, capture: ScreenCapture, capture_executor: Executor,
                 injector, clipboard, guard: AuthGuard, events: "SessionEvents | None" = None):
        self.events = events or SessionEvents()
        self.connected = False
        self.r = reader
        self.w = writer
        self.config = config
        self.capture = capture
        self.capture_executor = capture_executor
        self.injector = injector
        self.clipboard = clipboard
        self.guard = guard
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.ip = peer[0]
        self.peer = f"{peer[0]}:{peer[1]}"
        self.minor_version = 8

        self.encoder = Encoder(config.forced_quality)
        self.update_requested = asyncio.Event()
        self.full_update = True
        self.prev_frame: np.ndarray | None = None
        self.frame_size: tuple[int, int] | None = None
        self.capture_error: str | None = None
        self._notice_sent = False
        self._notice_cache: tuple[str, np.ndarray] | None = None
        self._incoming = IncomingFile(config.file_dir or receive_dir())
        self._discard_file = False
        self._file_batch: list[str] = []
        self._last_file_end = 0.0
        self._send_reject: str | None = None
        self._files_announced = False
        self._pending_files: list[str] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        sock = self.w.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tasks: list[asyncio.Task] = []
        self.events.session_started(self)
        try:
            if self.guard.blocked(self.ip):
                log.warning("%s blocked after repeated authentication failures", self.peer)
                return
            await self._handshake()
            await self._init()
            log.info("%s connected", self.peer)
            self.connected = True
            self.events.session_connected(self)
            tasks = [asyncio.create_task(self._update_loop()), asyncio.create_task(self._clipboard_loop())]
            for t in tasks:
                t.add_done_callback(self._on_task_done)
            await self._message_loop()
        except AuthFailed as exc:
            log.warning("%s authentication failed: %s", self.peer, exc)
            self.events.auth_failed(self)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError) as exc:
            log.info("%s disconnected (%s)", self.peer, exc.__class__.__name__)
        except ProtocolError as exc:
            log.warning("%s protocol error: %s", self.peer, exc)
        except Exception:
            log.exception("%s session failed", self.peer)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            self._incoming.abort()
            if self.injector is not None:
                self.injector.release_all()
            self.w.close()
            try:
                await self.w.wait_closed()
            except Exception:
                pass
            log.info("%s closed", self.peer)
            self.events.session_closed(self)

    def disconnect(self) -> None:
        """Close the connection; must be called on the server's event loop."""
        self.w.close()

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        log.warning("%s closing: %s", self.peer, task.exception())
        self.w.close()

    async def _send(self, data: bytes) -> None:
        self.w.write(data)
        await self.w.drain()

    # -- handshake ---------------------------------------------------------

    async def _handshake(self) -> None:
        await self._send(rfb.PROTOCOL_VERSION)
        version = await self.r.readexactly(12)
        if not version.startswith(b"RFB 003."):
            raise ProtocolError(f"bad version {version!r}")
        self.minor_version = int(version[8:11])
        if self.minor_version < 7:
            reason = b"RFB 3.3 is not supported; use a 3.7+ viewer"
            await self._send(struct.pack(">II", 0, len(reason)) + reason)
            raise ProtocolError("client speaks RFB 3.3")
        self.minor_version = min(self.minor_version, 8)

        types = []
        if self.config.ssl_context is not None:
            types.append(rfb.SEC_VENCRYPT)
        if self.config.allow_insecure:
            types.append(rfb.SEC_VNC_AUTH)
        await self._send(bytes([len(types), *types]))
        choice = (await self.r.readexactly(1))[0]
        if choice not in types:
            raise ProtocolError(f"client chose unsupported security type {choice}")

        if choice == rfb.SEC_VENCRYPT:
            ok = await self._vencrypt()
        else:
            ok = await self._vnc_auth()
        await self._finish_auth(ok)

    async def _vencrypt(self) -> bool:
        await self._send(b"\x00\x02")
        if await self.r.readexactly(2) != b"\x00\x02":
            await self._send(b"\x01")
            raise ProtocolError("unsupported VeNCrypt version")
        subtypes = [rfb.VENCRYPT_X509_PLAIN, rfb.VENCRYPT_X509_VNC]
        await self._send(b"\x00" + bytes([len(subtypes)]) + struct.pack(f">{len(subtypes)}I", *subtypes))
        (subtype,) = struct.unpack(">I", await self.r.readexactly(4))
        if subtype not in subtypes:
            await self._send(b"\x00")
            raise ProtocolError(f"unsupported VeNCrypt subtype {subtype}")
        await self._send(b"\x01")
        await self.w.start_tls(self.config.ssl_context)

        if subtype == rfb.VENCRYPT_X509_PLAIN:
            ulen, plen = struct.unpack(">II", await self.r.readexactly(8))
            if ulen > 1024 or plen > 1024:
                raise ProtocolError("credentials too long")
            await self.r.readexactly(ulen)
            password = await self.r.readexactly(plen)
            return hmac.compare_digest(password, self.config.password.encode("utf-8"))
        return await self._vnc_auth()

    async def _vnc_auth(self) -> bool:
        challenge = os.urandom(16)
        await self._send(challenge)
        response = await self.r.readexactly(16)
        return hmac.compare_digest(response, vnc_auth_response(self.config.password, challenge))

    async def _finish_auth(self, ok: bool) -> None:
        if ok:
            self.guard.succeeded(self.ip)
            await self._send(rfb.pack_security_result(True))
            return
        self.guard.failed(self.ip)
        await asyncio.sleep(2)
        if self.minor_version >= 8:
            await self._send(rfb.pack_security_result(False, "Authentication failed"))
        else:
            await self._send(struct.pack(">I", 1))
        raise AuthFailed("wrong password")

    async def _init(self) -> None:
        await self.r.readexactly(1)  # shared flag: multiple viewers are always allowed
        frame = await self._try_grab()
        if frame is None:
            frame = self._notice_frame(self.capture_error or "Screen capture failed")
        h, w = frame.shape[:2]
        self.frame_size = (w, h)
        name = self.config.name
        if self.capture_error:
            name += " (screen capture failed)"
        await self._send(rfb.pack_server_init(w, h, rfb.NATIVE_PIXEL_FORMAT, name))

    # -- client messages ---------------------------------------------------

    async def _message_loop(self) -> None:
        r = self.r
        while True:
            msg_type = (await r.readexactly(1))[0]
            if msg_type == rfb.MSG_SET_PIXEL_FORMAT:
                data = await r.readexactly(19)
                pf = rfb.PixelFormat.unpack(data[3:])
                if not pf.true_colour:
                    raise ProtocolError("colour-map pixel formats are not supported")
                self.encoder.configure(pixel_format=pf)
                self.full_update = True
            elif msg_type == rfb.MSG_SET_ENCODINGS:
                (count,) = struct.unpack(">xH", await r.readexactly(3))
                encodings = list(struct.unpack(f">{count}i", await r.readexactly(4 * count)))
                self.encoder.configure(encodings=encodings)
                if self.encoder.supports(rfb.ENC_PYVNC_FILES) and not self._files_announced:
                    self._files_announced = True
                    await self._send(pack_file_caps(rfb.MSG_SERVER_FILES))
            elif msg_type == rfb.MSG_FB_UPDATE_REQUEST:
                incremental, _x, _y, _w, _h = struct.unpack(">BHHHH", await r.readexactly(9))
                if not incremental:
                    self.full_update = True
                self.update_requested.set()
            elif msg_type == rfb.MSG_KEY_EVENT:
                down, keysym = struct.unpack(">B2xI", await r.readexactly(7))
                if not self.config.view_only:
                    self.injector.key(keysym, bool(down))
            elif msg_type == rfb.MSG_POINTER_EVENT:
                mask, x, y = struct.unpack(">BHH", await r.readexactly(5))
                if not self.config.view_only:
                    self.injector.pointer(mask, self._to_screen_points(x, y))
            elif msg_type == rfb.MSG_CLIENT_CUT_TEXT:
                (length,) = struct.unpack(">3xI", await r.readexactly(7))
                if length > MAX_CUT_TEXT:
                    raise ProtocolError("clipboard text too large")
                text = (await r.readexactly(length)).decode("latin-1")
                if self.clipboard is not None and not self.config.view_only:
                    self.clipboard.set(text)
            elif msg_type == rfb.MSG_CLIENT_FILES:
                await self._on_client_file()
            else:
                raise ProtocolError(f"unknown message type {msg_type}")

    def _to_screen_points(self, x: int, y: int) -> tuple[float, float]:
        """Map framebuffer pixels to global screen coordinates (points, not Retina pixels)."""
        mon = self.capture.monitor
        fw, fh = self.frame_size or (mon["width"], mon["height"])
        return (mon["left"] + x * mon["width"] / fw, mon["top"] + y * mon["height"] / fh)

    # -- framebuffer updates -----------------------------------------------

    async def _grab(self) -> np.ndarray:
        return await asyncio.get_running_loop().run_in_executor(self.capture_executor, self.capture.grab)

    async def _try_grab(self) -> np.ndarray | None:
        """Grab a frame. A capture failure is reported once and does not drop the connection."""
        try:
            frame = await self._grab()
        except Exception as exc:
            message = _capture_failure_text(exc)
            if message != self.capture_error:
                log.exception("%s screen capture failed", self.peer)
                self.capture_error = message
                self._notice_sent = False
            return None
        if self.capture_error:
            log.info("%s screen capture recovered", self.peer)
            self.capture_error = None
            self._notice_sent = False
            self.full_update = True
            self.prev_frame = None
        return frame

    def _notice_frame(self, message: str) -> np.ndarray:
        if self._notice_cache and self._notice_cache[0] == message:
            return self._notice_cache[1]
        frame = _render_notice(message)
        self._notice_cache = (message, frame)
        return frame

    async def _update_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = 1.0 / self.config.fps
        while True:
            await self.update_requested.wait()
            started = loop.time()
            frame = await self._try_grab()
            if frame is None:
                frame = self._notice_frame(self.capture_error or "Screen capture failed")
                if self._notice_sent and (frame.shape[1], frame.shape[0]) == self.frame_size:
                    await asyncio.sleep(max(0.0, interval - (loop.time() - started)))
                    continue
            h, w = frame.shape[:2]

            if (w, h) != self.frame_size:
                if not self.encoder.supports(rfb.ENC_DESKTOP_SIZE):
                    raise ConnectionResetError("screen resolution changed and viewer cannot resize")
                self.frame_size = (w, h)
                self.prev_frame = None
                self.full_update = True
                await self._send(desktop_size_update(w, h))
                self.update_requested.clear()
                continue

            if self.full_update or self.prev_frame is None:
                rects = [(0, 0, w, h)]
            else:
                rects = dirty_rects(self.prev_frame, frame)

            if rects:
                data = await loop.run_in_executor(None, self.encoder.encode, frame, rects)
                await self._send(data)
                self.prev_frame = frame
                self.full_update = False
                self._notice_sent = self.capture_error is not None
                self.update_requested.clear()

            await asyncio.sleep(max(0.0, interval - (loop.time() - started)))

    async def _clipboard_loop(self) -> None:
        if self.clipboard is None:
            return
        while True:
            await asyncio.sleep(0.5)
            item = self.clipboard.poll()
            if isinstance(item, list):
                if item:
                    self._pending_files = item
            elif item:
                await self._send(rfb.pack_cut_text(rfb.MSG_SERVER_CUT_TEXT, item))
            if self._pending_files and self.encoder.supports(rfb.ENC_PYVNC_FILES):
                paths = self._pending_files
                self._pending_files = None
                self._send_reject = None
                await transmit_files(
                    self._send, rfb.MSG_SERVER_FILES, paths,
                    reject=lambda: self._send_reject,
                    on_status=lambda text: log.info("%s %s", self.peer, text),
                )

    async def _on_client_file(self) -> None:
        try:
            op = (await self.r.readexactly(1))[0]
            body = await read_file_body(self.r.readexactly, op)
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc
        if op == rfb.FILE_BEGIN:
            if time.monotonic() - self._last_file_end > 1.0:
                self._file_batch = []
                self._incoming.reset_batch()
            if self.config.view_only or self.clipboard is None:
                self._discard_file = True
                self._incoming.abort()
                await self._send(pack_file_abort(rfb.MSG_SERVER_FILES, "File paste is disabled"))
                return
            error = self._incoming.begin(body["name"], body["size"])
            if error:
                self._discard_file = True
                self._incoming.abort()
                await self._send(pack_file_abort(rfb.MSG_SERVER_FILES, error))
                return
            self._discard_file = False
            log.info("%s receiving %s", self.peer, body["name"])
            return
        if op == rfb.FILE_DATA:
            if self._discard_file:
                return
            error = self._incoming.add(body["data"])
            if error:
                self._discard_file = True
                await self._send(pack_file_abort(rfb.MSG_SERVER_FILES, error))
            return
        if op == rfb.FILE_END:
            if self._discard_file:
                self._discard_file = False
                return
            path = self._incoming.end()
            if path is None:
                log.warning("%s file transfer was incomplete", self.peer)
                return
            self._last_file_end = time.monotonic()
            target = self._incoming.clipboard_target or path
            if str(target) not in self._file_batch:
                self._file_batch.append(str(target))
            if self.clipboard is not None:
                self.clipboard.set_files(list(self._file_batch))
            log.info("%s received %s", self.peer, target.name)
            return
        if op == rfb.FILE_ABORT:
            if self._incoming.active:
                self._incoming.abort()
                self._discard_file = False
                log.info("%s file transfer cancelled: %s", self.peer, body["reason"])
            else:
                self._send_reject = body["reason"] or "File paste was refused"
            return
        raise ProtocolError(f"bad file operation {op}")


def _capture_failure_text(exc: Exception) -> str:
    detail = str(exc).strip() or exc.__class__.__name__
    return (
        f"{detail}\n\n"
        "Allow Screen Recording for PyVNC Server in System Settings > "
        "Privacy & Security, then quit and reopen the app."
    )


def _render_notice(message: str) -> np.ndarray:
    """A small framebuffer the viewer can show when capture is unavailable."""
    width, height = 960, 540
    img = Image.new("RGB", (width, height), (28, 28, 28))
    draw = ImageDraw.Draw(img)
    title = ImageFont.load_default(size=28)
    body = ImageFont.load_default(size=18)
    draw.text((36, 36), "PyVNC cannot capture the screen", font=title, fill=(255, 96, 96))
    y = 100
    for paragraph in message.splitlines():
        lines = textwrap.wrap(paragraph, width=78) or [""]
        for line in lines:
            draw.text((36, y), line, font=body, fill=(230, 230, 230))
            y += 28
        y += 8
    rgb = np.asarray(img)
    frame = np.empty((height, width, 4), dtype=np.uint8)
    frame[..., 0] = rgb[..., 2]
    frame[..., 1] = rgb[..., 1]
    frame[..., 2] = rgb[..., 0]
    frame[..., 3] = 255
    return frame
