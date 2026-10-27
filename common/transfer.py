"""PyVNC file clipboard: framing, safe names, and the receive folder."""
from __future__ import annotations

import asyncio
import os
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


def safe_relative(name: str) -> str:
    """Slash-separated path under the receive folder. `..` and empty pieces are dropped."""
    is_dir = name.endswith("/") or name.endswith("\\")
    parts = [safe_name(piece) for piece in name.replace("\\", "/").split("/") if piece not in ("", ".", "..")]
    if not parts:
        parts = ["file"]
    relative = "/".join(parts)
    return relative + "/" if is_dir else relative


def iter_copy_items(paths: list[str]) -> list[tuple[Path | None, str]]:
    """Expand files and folders into (source file or None, relative name).

    Directory entries use a trailing slash and have no source file. Symlinks to
    directories are not followed.
    """
    items: list[tuple[Path | None, str]] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            items.append((path, safe_relative(path.name)))
            continue
        if not path.is_dir():
            continue
        root = safe_name(path.name)
        items.append((None, root + "/"))
        for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
            current = Path(dirpath)
            rel_dir = current.relative_to(path).as_posix()
            prefix = root if rel_dir == "." else f"{root}/{rel_dir}"
            if current != path:
                items.append((None, safe_relative(prefix) + "/"))
            for filename in filenames:
                file_path = current / filename
                if file_path.is_file():
                    items.append((file_path, safe_relative(f"{prefix}/{filename}")))
    return items


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
    raw = safe_relative(name).encode("utf-8")[:NAME_MAX]
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
    """Writes one announced file under a directory. Partial files are deleted.

    A name ending in `/` with size 0 creates a directory and does not open a file.
    The first component of a folder is renamed once per batch if it already exists,
    so `Photos/a.jpg` and `Photos/b.jpg` stay inside the same `Photos (2)`.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.active = False
        self.clipboard_target: Path | None = None
        self._handle = None
        self._path: Path | None = None
        self._size = 0
        self._got = 0
        self._is_dir = False
        self._top_rename: dict[str, str] = {}

    @property
    def received(self) -> tuple[int, int]:
        return self._got, self._size

    def reset_batch(self) -> None:
        """Forget folder renames from the previous copy."""
        self._top_rename.clear()

    def begin(self, name: str, size: int) -> str | None:
        self.abort()
        is_dir = name.endswith("/") or name.endswith("\\")
        if is_dir and size != 0:
            return "directory entry has a size"
        if size < 0 or size > rfb.FILE_MAX_BYTES:
            return "file is larger than 128 MB"
        try:
            path = self._destination(name, is_dir)
        except ValueError:
            return "file name is not allowed"
        self._path = path
        self._is_dir = is_dir
        self._size = size
        self._got = 0
        self.clipboard_target = self._clipboard_target(path)
        if is_dir:
            path.mkdir(parents=True, exist_ok=True)
            self._handle = None
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("wb")
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
        if not self.active or self._path is None:
            return None
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            if self._got != self._size:
                self._path.unlink(missing_ok=True)
                self._path = None
                self.active = False
                self._is_dir = False
                return None
        elif not self._is_dir:
            self.active = False
            return None
        self.active = False
        path = self._path
        self._path = None
        self._is_dir = False
        return path

    def abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._path is not None and self._path.is_file():
            self._path.unlink(missing_ok=True)
        elif self._path is not None and self._is_dir:
            try:
                self._path.rmdir()
            except OSError:
                pass
        self._path = None
        self.active = False
        self._is_dir = False
        self._got = 0
        self._size = 0

    def _destination(self, name: str, is_dir: bool) -> Path:
        parts = [piece for piece in safe_relative(name).rstrip("/").split("/") if piece]
        if not parts:
            parts = ["file"]
        if len(parts) == 1 and not is_dir:
            path = unique_path(self.directory, parts[0])
        else:
            top = parts[0]
            if top not in self._top_rename:
                self._top_rename[top] = _unique_name(self.directory, top, directory_name=True)
            path = self.directory.joinpath(self._top_rename[top], *parts[1:])
        resolved_dir = self.directory.resolve()
        resolved = path.resolve()
        if resolved != resolved_dir and resolved_dir not in resolved.parents:
            raise ValueError("path escapes the receive folder")
        return path

    def _clipboard_target(self, path: Path) -> Path:
        relative = path.resolve().relative_to(self.directory.resolve())
        if len(relative.parts) <= 1:
            return path
        return self.directory / relative.parts[0]


def _unique_name(directory: Path, name: str, directory_name: bool) -> str:
    if not (directory / name).exists():
        return name
    stem = name if directory_name else Path(name).stem
    suffix = "" if directory_name else Path(name).suffix
    n = 2
    while (directory / f"{stem} ({n}){suffix}").exists():
        n += 1
    return f"{stem} ({n}){suffix}"


async def transmit_files(
    send: Callable[[bytes], Awaitable[None]],
    msg_type: int,
    paths: list[str],
    reject: Callable[[], str | None],
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> None:
    """Send files and folders one entry at a time. A reject callback stops the rest."""
    loop = asyncio.get_running_loop()
    items = iter_copy_items(paths)
    file_total = sum(1 for source, _name in items if source is not None)

    def status(text: str) -> None:
        if on_status is not None:
            on_status(text)

    def progress(name: str, done: int, total: int) -> None:
        if on_progress is not None:
            on_progress(name, done, total)

    sent: list[str] = []
    sent_folders: list[str] = []
    file_index = 0
    for source, name in items:
        reason = reject()
        if reason:
            status(reason)
            return
        if source is None:
            await send(pack_file_begin(msg_type, name, 0))
            await send(pack_file_end(msg_type))
            top = name.rstrip("/").split("/")[0]
            if top not in sent_folders:
                sent_folders.append(top)
            continue
        file_index += 1
        try:
            size = source.stat().st_size
        except OSError as exc:
            status(f"Could not read {name}: {exc}")
            continue
        if size > rfb.FILE_MAX_BYTES:
            status(f"Skipped {name}: larger than 128 MB")
            continue
        label = name if file_total == 1 else f"{name} ({file_index} of {file_total})"
        progress(label, 0, size)
        await send(pack_file_begin(msg_type, name, size))
        offset = 0
        failed = False
        while offset < size:
            reason = reject()
            if reason:
                await send(pack_file_end(msg_type))
                status(reason)
                return
            chunk = await loop.run_in_executor(None, read_at, source, offset, rfb.FILE_CHUNK)
            if len(chunk) == 0:
                await send(pack_file_abort(msg_type, f"Could not read {name}"))
                failed = True
                break
            await send(pack_file_data(msg_type, chunk))
            offset += len(chunk)
            progress(label, offset, size)
        if not failed:
            await send(pack_file_end(msg_type))
            progress(label, size, size)
            sent.append(name)
    if sent:
        status(f"Sent {sent[0]}" if len(sent) == 1 else f"Sent {len(sent)} files")
    elif sent_folders:
        status(f"Sent {sent_folders[0]}" if len(sent_folders) == 1 else f"Sent {len(sent_folders)} folders")
    reason = reject()
    if reason:
        status(reason)
