"""Windows clipboard access through the Win32 API.

Called from the server thread, so this does not use Qt.
"""
from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

CF_UNICODETEXT = 13
CF_HDROP = 15
_GMEM_MOVEABLE = 0x0002

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_shell32 = ctypes.windll.shell32

_user32.OpenClipboard.argtypes = [ctypes.c_void_p]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
_shell32.DragQueryFileW.restype = wintypes.UINT


def pack_drop_files(paths: list[str]) -> bytes:
    """CF_HDROP payload: a DROPFILES header plus UTF-16 paths, including folders."""
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    body = bytearray()
    for path in paths:
        body += os.path.normpath(path).encode("utf-16-le") + b"\x00\x00"
    body += b"\x00\x00"
    return header + bytes(body)


def unpack_drop_files(payload: bytes) -> list[str]:
    """Read paths back out of a payload from pack_drop_files."""
    if len(payload) < 20:
        return []
    offset, _x, _y, _nc, wide = struct.unpack_from("<IiiII", payload, 0)
    if not wide or offset < 20 or offset >= len(payload):
        return []
    text = payload[offset:].decode("utf-16-le", errors="replace")
    return [part for part in text.split("\x00") if part]


class Clipboard:
    def __init__(self):
        self._seq = _user32.GetClipboardSequenceNumber()

    def poll(self) -> str | list[str] | None:
        """Return new clipboard text, copied paths, or None if unchanged.

        Files win over text when both are present. A copied folder is included.
        """
        seq = _user32.GetClipboardSequenceNumber()
        if seq == self._seq:
            return None
        try:
            files = self._read_files()
            text = None if files else self._read_text()
        except OSError:
            return None
        self._seq = seq
        if files:
            return files
        return text or None

    def set(self, text: str) -> None:
        raw = text.encode("utf-16-le") + b"\x00\x00"
        self._set_data(CF_UNICODETEXT, raw)
        self._seq = _user32.GetClipboardSequenceNumber()

    def set_files(self, paths: list[str]) -> None:
        """Put file and folder paths on the clipboard so Explorer's Paste places them."""
        self._set_data(CF_HDROP, pack_drop_files(paths))
        self._seq = _user32.GetClipboardSequenceNumber()

    def _read_text(self) -> str | None:
        if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        with _open_clipboard():
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            pointer = _kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return ctypes.wstring_at(pointer)
            finally:
                _kernel32.GlobalUnlock(handle)

    def _read_files(self) -> list[str]:
        if not _user32.IsClipboardFormatAvailable(CF_HDROP):
            return []
        with _open_clipboard():
            handle = _user32.GetClipboardData(CF_HDROP)
            if not handle:
                return []
            count = _shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
            paths = []
            for index in range(count):
                length = _shell32.DragQueryFileW(handle, index, None, 0)
                buf = ctypes.create_unicode_buffer(length + 1)
                _shell32.DragQueryFileW(handle, index, buf, length + 1)
                path = buf.value
                if path and (os.path.isfile(path) or os.path.isdir(path)):
                    paths.append(path)
            return paths

    def _set_data(self, fmt: int, payload: bytes) -> None:
        handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
        if not handle:
            raise OSError("could not allocate clipboard data")
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("could not lock clipboard data")
        ctypes.memmove(pointer, payload, len(payload))
        _kernel32.GlobalUnlock(handle)
        with _open_clipboard():
            _user32.EmptyClipboard()
            if not _user32.SetClipboardData(fmt, handle):
                raise OSError("could not set clipboard data")


class _open_clipboard:
    def __enter__(self):
        if not _user32.OpenClipboard(None):
            raise OSError("clipboard is busy")
        return self

    def __exit__(self, *_args):
        _user32.CloseClipboard()
