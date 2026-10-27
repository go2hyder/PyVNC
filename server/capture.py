"""Screen capture with mss plus dirty-tile detection."""
from __future__ import annotations

import threading

import mss
import numpy as np
from PIL import Image

TILE = 64


class ScreenCapture:
    """Grabs one display as an (H, W, 4) uint8 B,G,R,X array.

    mss is not thread-safe, so each thread gets its own mss instance.
    """

    def __init__(self, display: int = 1, scale: float = 1.0):
        self.display = display
        self.scale = scale
        self._local = threading.local()
        self.monitor = self._sct().monitors[display]

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = mss.MSS()
            self._local.sct = sct
        return sct

    def grab(self) -> np.ndarray:
        sct = self._sct()
        self.monitor = sct.monitors[self.display]
        shot = sct.grab(self.monitor)
        w, h = shot.size
        frame = np.frombuffer(shot.raw, dtype=np.uint8).reshape(h, w, 4)
        if self.scale == 0.5:
            frame = np.ascontiguousarray(frame[::2, ::2])
        elif self.scale != 1.0:
            size = (max(1, int(w * self.scale)), max(1, int(h * self.scale)))
            frame = np.asarray(Image.fromarray(frame, "RGBA").resize(size, Image.BILINEAR))
        return frame


def dirty_rects(prev: np.ndarray | None, cur: np.ndarray, tile: int = TILE) -> list[tuple[int, int, int, int]]:
    """Return changed regions as (x, y, w, h), merging dirty tiles into horizontal runs."""
    h, w = cur.shape[:2]
    if prev is None or prev.shape != cur.shape:
        return [(0, 0, w, h)]

    a = cur.view(np.uint32)[..., 0]
    b = prev.view(np.uint32)[..., 0]
    diff = a != b
    if not diff.any():
        return []

    th, tw = -(-h // tile), -(-w // tile)
    padded = np.zeros((th * tile, tw * tile), dtype=bool)
    padded[:h, :w] = diff
    tiles = padded.reshape(th, tile, tw, tile).any(axis=(1, 3))

    rects = []
    for ty in range(th):
        row = tiles[ty]
        if not row.any():
            continue
        tx = 0
        while tx < tw:
            if not row[tx]:
                tx += 1
                continue
            start = tx
            while tx < tw and row[tx]:
                tx += 1
            x0, y0 = start * tile, ty * tile
            rects.append((x0, y0, min(tx * tile, w) - x0, min(y0 + tile, h) - y0))
    return _merge_vertical(rects)


def _merge_vertical(rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Merge runs that line up exactly with the run directly above them."""
    merged: list[list[int]] = []
    open_by_span: dict[tuple[int, int], list[int]] = {}
    for x, y, w, h in rects:
        above = open_by_span.get((x, w))
        if above is not None and above[1] + above[3] == y:
            above[3] += h
        else:
            r = [x, y, w, h]
            merged.append(r)
            open_by_span[(x, w)] = r
    return [tuple(r) for r in merged]
