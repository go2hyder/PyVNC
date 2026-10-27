"""macOS clipboard access through NSPasteboard."""
from __future__ import annotations

from AppKit import NSPasteboard, NSPasteboardTypeString


class Clipboard:
    def __init__(self):
        self._pb = NSPasteboard.generalPasteboard()
        self._count = self._pb.changeCount()

    def poll(self) -> str | None:
        """Return the clipboard text if it changed since the last poll or set."""
        count = self._pb.changeCount()
        if count == self._count:
            return None
        self._count = count
        return self._pb.stringForType_(NSPasteboardTypeString)

    def set(self, text: str) -> None:
        self._pb.clearContents()
        self._pb.setString_forType_(text, NSPasteboardTypeString)
        self._count = self._pb.changeCount()
