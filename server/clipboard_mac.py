"""macOS clipboard access through NSPasteboard."""
from __future__ import annotations

import os

from AppKit import NSPasteboard, NSPasteboardTypeString


class Clipboard:
    def __init__(self):
        self._pb = NSPasteboard.generalPasteboard()
        self._count = self._pb.changeCount()

    def poll(self) -> str | list[str] | None:
        """Return new clipboard text, a list of copied file paths, or None if unchanged.

        Files win over text when both are present. A copied folder is included.
        """
        count = self._pb.changeCount()
        if count == self._count:
            return None
        self._count = count
        files = self._file_paths()
        if files:
            return files
        return self._pb.stringForType_(NSPasteboardTypeString)

    def set(self, text: str) -> None:
        self._pb.clearContents()
        self._pb.setString_forType_(text, NSPasteboardTypeString)
        self._count = self._pb.changeCount()

    def set_files(self, paths: list[str]) -> None:
        """Put file URLs on the pasteboard so Finder's Paste places those files."""
        from Foundation import NSURL
        urls = [NSURL.fileURLWithPath_(str(path)) for path in paths]
        self._pb.clearContents()
        self._pb.writeObjects_(urls)
        self._count = self._pb.changeCount()

    def _file_paths(self) -> list[str]:
        try:
            return self._collect_file_paths()
        except Exception:
            return []

    def _collect_file_paths(self) -> list[str]:
        paths: list[str] = []
        items = self._pb.pasteboardItems() or []
        for item in items:
            value = item.stringForType_("public.file-url")
            if not value:
                continue
            from Foundation import NSURL
            url = NSURL.URLWithString_(value)
            if url is None or not url.isFileURL():
                continue
            path = str(url.path())
            if os.path.isfile(path) or os.path.isdir(path):
                paths.append(path)
        if paths:
            return paths
        names = self._pb.propertyListForType_("NSFilenamesPboardType") or []
        for name in names:
            path = str(name)
            if os.path.isfile(path) or os.path.isdir(path):
                paths.append(path)
        return paths
