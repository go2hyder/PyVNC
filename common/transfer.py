"""PyVNC file clipboard: framing, safe names, and the receive folder."""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import Awaitable, Callable

from common import rfb

NAME_MAX = 1024
_FORBIDDEN = '<>:"|?*'


def receive_dir() -> Path:
    """Folder that received files are written to."""
    home = Path.home()
    parent = home / "Downloads"
    if not parent.is_dir():
        parent = home / "downloads"
    path = parent / "PyVNC"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str) -> str:
    """Basename only, with characters the destination filesystem rejects removed."""
    base = name.replace("\\", "/").split("/")[-1]
    base = "".join(ch for ch in base if ch not in _FORBIDDEN and ord(ch) >= 32).strip(" .")
    if base in ("", ".", ".."):
        return "file"
    return base[:180]


def unique_path(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base = safe_name(name)
    path = directory / base
    if not path.exists():
        return path
    stem = Path(base).stem
    suffix = Path(base).suffix
    n = 2
    while True:
        candidate = directory / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def read_at(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)


def pack_file_begin(msg_type: int, name: str, size: int) -> bytes:
    raw = safe_name(name).encode("utf-8")[:NAME_MAX]
    return struct.pack(">BBH", msg_type, rfb.FILE_BEGIN, len(raw)) + raw + struct.pack(">Q", size)


def pack_file_data(msg_type: int, data: bytes) -> bytes:
    return struct.pack(">BBI", msg_type, rfb.FILE_DATA, len(data)) + data


def pack_file_end(msg_type: int) -> bytes:
    return struct.pack(">BB", msg_type, rfb.FILE_END)


def pack_file_abort(msg_type: int, reason: str) -> bytes:
    raw = reason.encode("utf-8")[:400]
    return struct.pack(">BBI", msg_type, rfb.FILE_ABORT, len(raw)) + raw


def pack_file_caps(msg_type: int) -> bytes:
    return struct.pack(">BB", msg_type, rfb.FILE_CAPS)


async def read_file_body(read: Callable[[int], Awaitable[bytes]], op: int) -> dict:
    """Read the payload after the message type and op byte."""
    if op == rfb.FILE_CAPS:
        return {"op": op}
    if op == rfb.FILE_BEGIN:
        (nlen,) = struct.unpack(">H", await read(2))
        if nlen > NAME_MAX:
            raise ValueError("file name too long")
        name = (await read(nlen)).decode("utf-8", errors="replace")
        (size,) = struct.unpack(">Q", await read(8))
        return {"op": op, "name": name, "size": size}
    if op == rfb.FILE_DATA:
        (nlen,) = struct.unpack(">I", await read(4))
        if nlen > rfb.FILE_CHUNK:
            raise ValueError("file chunk too large")
        return {"op": op, "data": await read(nlen)}
    if op == rfb.FILE_END:
        return {"op": op}
    if op == rfb.FILE_ABORT:
        (nlen,) = struct.unpack(">I", await read(4))
        if nlen > 400:
            raise ValueError("abort reason too long")
        reason = (await read(nlen)).decode("utf-8", errors="replace")
        return {"op": op, "reason": reason}
    raise ValueError(f"bad file operation {op}")


class IncomingFile:
    """Writes one announced file under a directory. Partial files are deleted."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.active = False
        self._handle = None
        self._path: Path | None = None
        self._size = 0
        self._got = 0

    @property
    def received(self) -> tuple[int, int]:
        return self._got, self._size

    def begin(self, name: str, size: int) -> str | None:
        self.abort()
        if size < 0 or size > rfb.FILE_MAX_BYTES:
            return "file is larger than 128 MB"
        self._path = unique_path(self.directory, name)
        self._handle = self._path.open("wb")
        self._size = size
        self._got = 0
        self.active = True
        return None

    def add(self, data: bytes) -> str | None:
        if not self.active or self._handle is None:
            return "unexpected file data"
        if self._got + len(data) > self._size:
            self.abort()
            return "file was longer than announced"
        self._handle.write(data)
        self._got += len(data)
        return None

    def end(self) -> Path | None:
        if not self.active or self._handle is None or self._path is None:
            return None
        self._handle.close()
        self._handle = None
        self.active = False
        if self._got != self._size:
            self._path.unlink(missing_ok=True)
            self._path = None
            return None
        path = self._path
        self._path = None
        return path

    def abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None
        self.active = False
        self._got = 0
        self._size = 0


async def transmit_files(
    send: Callable[[bytes], Awaitable[None]],
    msg_type: int,
    paths: list[str],
    reject: Callable[[], str | None],
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> None:
    """Send regular files one at a time. A reject callback stops the rest of the batch."""
    loop = asyncio.get_running_loop()
    files = [Path(raw) for raw in paths if Path(raw).is_file()]

    def status(text: str) -> None:
        if on_status is not None:
            on_status(text)

    def progress(name: str, done: int, total: int) -> None:
        if on_progress is not None:
            on_progress(name, done, total)

    sent: list[str] = []
    for index, path in enumerate(files, start=1):
        reason = reject()
        if reason:
            status(reason)
            return
        try:
            size = path.stat().st_size
        except OSError as exc:
            status(f"Could not read {path.name}: {exc}")
            continue
        if size > rfb.FILE_MAX_BYTES:
            status(f"Skipped {path.name}: larger than 128 MB")
            continue
        label = path.name if len(files) == 1 else f"{path.name} ({index} of {len(files)})"
        progress(label, 0, size)
        await send(pack_file_begin(msg_type, path.name, size))
        offset = 0
        failed = False
        while offset < size:
            reason = reject()
            if reason:
                await send(pack_file_end(msg_type))
                status(reason)
                return
            chunk = await loop.run_in_executor(None, read_at, path, offset, rfb.FILE_CHUNK)
            if len(chunk) == 0:
                await send(pack_file_abort(msg_type, f"Could not read {path.name}"))
                failed = True
                break
            await send(pack_file_data(msg_type, chunk))
            offset += len(chunk)
            progress(label, offset, size)
        if not failed:
            await send(pack_file_end(msg_type))
            progress(label, size, size)
            sent.append(path.name)
    if sent:
        status(f"Sent {sent[0]}" if len(sent) == 1 else f"Sent {len(sent)} files")
    reason = reject()
    if reason:
        status(reason)
