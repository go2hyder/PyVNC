"""Framebuffer widget: renders the remote screen and forwards mouse/keyboard input."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QEvent, QRect, QRectF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QWidget

from common import rfb

Key = Qt.Key

_SPECIAL_KEYS = {
    Key.Key_Escape: rfb.XK_Escape, Key.Key_Tab: rfb.XK_Tab, Key.Key_Backtab: rfb.XK_Tab,
    Key.Key_Backspace: rfb.XK_BackSpace, Key.Key_Return: rfb.XK_Return,
    Key.Key_Enter: rfb.XK_KP_Enter, Key.Key_Insert: rfb.XK_Insert, Key.Key_Delete: rfb.XK_Delete,
    Key.Key_Pause: rfb.XK_Pause, Key.Key_Print: rfb.XK_Print, Key.Key_Home: rfb.XK_Home,
    Key.Key_End: rfb.XK_End, Key.Key_Left: rfb.XK_Left, Key.Key_Up: rfb.XK_Up,
    Key.Key_Right: rfb.XK_Right, Key.Key_Down: rfb.XK_Down, Key.Key_PageUp: rfb.XK_Page_Up,
    Key.Key_PageDown: rfb.XK_Page_Down, Key.Key_Shift: rfb.XK_Shift_L,
    Key.Key_Control: rfb.XK_Control_L, Key.Key_Meta: rfb.XK_Super_L, Key.Key_Alt: rfb.XK_Alt_L,
    Key.Key_AltGr: rfb.XK_ISO_Level3_Shift, Key.Key_CapsLock: rfb.XK_Caps_Lock,
    Key.Key_NumLock: rfb.XK_Num_Lock, Key.Key_ScrollLock: rfb.XK_Scroll_Lock,
    Key.Key_Menu: rfb.XK_Menu, Key.Key_Help: rfb.XK_Help,
}
_SPECIAL_KEYS = {k.value: v for k, v in _SPECIAL_KEYS.items()}
for _i in range(20):
    _SPECIAL_KEYS[Key.Key_F1.value + _i] = rfb.XK_F1 + _i
_TAB_KEYS = (Key.Key_Tab.value, Key.Key_Backtab.value)


class VncView(QWidget):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.scale: float | None = None  # None = fit to window
        self.ctrl_as_cmd = True
        self._fb: np.ndarray | None = None
        self._image: QImage | None = None
        self._buttons = 0
        self._pressed: dict[int, int] = {}
        self._wheel = [0, 0]
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # -- framebuffer -------------------------------------------------------

    def set_framebuffer(self, fb: np.ndarray) -> None:
        self._fb = fb
        h, w = fb.shape[:2]
        # QImage reads straight from the numpy buffer (B,G,R,X bytes = Format_RGB32).
        self._image = QImage(fb.data, w, h, w * 4, QImage.Format.Format_RGB32)
        self._apply_size()
        self.update()

    def framebuffer_size(self) -> tuple[int, int]:
        if self._fb is None:
            return (0, 0)
        return (self._fb.shape[1], self._fb.shape[0])

    def snapshot(self) -> QImage | None:
        return self._image.copy() if self._image is not None else None

    def set_scale(self, scale: float | None) -> None:
        """None fits the remote screen to the window; a number draws it at that zoom."""
        self.scale = scale
        self._apply_size()
        self.update()

    def _apply_size(self) -> None:
        if self.scale is None:
            self.setMinimumSize(1, 1)
            self.setMaximumSize(16777215, 16777215)
        elif self._fb is not None:
            fw, fh = self.framebuffer_size()
            self.setFixedSize(max(1, int(fw * self.scale)), max(1, int(fh * self.scale)))

    def _target_rect(self) -> QRectF:
        fw, fh = self.framebuffer_size()
        if not fw or not fh:
            return QRectF(0, 0, 0, 0)
        if self.scale is not None:
            return QRectF(0, 0, fw * self.scale, fh * self.scale)
        scale = min(self.width() / fw, self.height() / fh)
        w, h = fw * scale, fh * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def region_updated(self, x: int, y: int, w: int, h: int) -> None:
        target = self._target_rect()
        fw, fh = self.framebuffer_size()
        if not fw:
            return
        sx, sy = target.width() / fw, target.height() / fh
        self.update(QRect(int(target.x() + x * sx) - 1, int(target.y() + y * sy) - 1,
                          int(w * sx) + 3, int(h * sy) + 3))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._image is not None:
            target = self._target_rect()
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                            int(target.width()) != self._image.width())
            p.drawImage(target, self._image)
        p.end()

    # -- mouse -------------------------------------------------------------

    def _to_fb(self, pos) -> tuple[int, int]:
        target = self._target_rect()
        fw, fh = self.framebuffer_size()
        if target.width() <= 0:
            return (0, 0)
        x = (pos.x() - target.x()) * fw / target.width()
        y = (pos.y() - target.y()) * fh / target.height()
        return (int(max(0, min(x, fw - 1))), int(max(0, min(y, fh - 1))))

    @staticmethod
    def _mask(buttons) -> int:
        mask = 0
        if buttons & Qt.MouseButton.LeftButton:
            mask |= rfb.BTN_LEFT
        if buttons & Qt.MouseButton.MiddleButton:
            mask |= rfb.BTN_MIDDLE
        if buttons & Qt.MouseButton.RightButton:
            mask |= rfb.BTN_RIGHT
        return mask

    def _send_mouse(self, event) -> None:
        self._buttons = self._mask(event.buttons())
        self.client.send_pointer(self._buttons, *self._to_fb(event.position()))

    def mousePressEvent(self, event) -> None:
        self.setFocus()
        self._send_mouse(event)

    def mouseReleaseEvent(self, event) -> None:
        self._send_mouse(event)

    def mouseMoveEvent(self, event) -> None:
        self._send_mouse(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self._send_mouse(event)

    def wheelEvent(self, event) -> None:
        x, y = self._to_fb(event.position())
        delta = event.angleDelta()
        self._wheel[0] += delta.x()
        self._wheel[1] += delta.y()
        for axis, positive, negative in ((1, rfb.BTN_WHEEL_UP, rfb.BTN_WHEEL_DOWN),
                                         (0, rfb.BTN_WHEEL_LEFT, rfb.BTN_WHEEL_RIGHT)):
            while abs(self._wheel[axis]) >= 120:
                bit = positive if self._wheel[axis] > 0 else negative
                self._wheel[axis] -= 120 if self._wheel[axis] > 0 else -120
                self.client.send_pointer(self._buttons | bit, x, y)
                self.client.send_pointer(self._buttons, x, y)
        event.accept()

    # -- keyboard ----------------------------------------------------------

    def event(self, event) -> bool:
        # Deliver Tab/Backtab to keyPressEvent instead of moving widget focus.
        if event.type() == QEvent.Type.KeyPress and int(event.key()) in _TAB_KEYS:
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def _keysym(self, event) -> int | None:
        key = int(event.key())
        if key in _SPECIAL_KEYS:
            keysym = _SPECIAL_KEYS[key]
            if self.ctrl_as_cmd and keysym == rfb.XK_Control_L:
                return rfb.XK_Super_L
            if self.ctrl_as_cmd and keysym == rfb.XK_Super_L:
                return rfb.XK_Control_L
            return keysym
        text = event.text()
        if len(text) == 1 and text.isprintable():
            return rfb.char_to_keysym(text)
        if 0x20 <= key <= 0x7E:
            ch = chr(key)
            if ch.isalpha() and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                ch = ch.lower()
            return ord(ch)
        return None

    def keyPressEvent(self, event) -> None:
        event.accept()
        ident = event.nativeScanCode() or int(event.key())
        if event.isAutoRepeat() and ident in self._pressed:
            self.client.send_key(self._pressed[ident], True)
            return
        keysym = self._keysym(event)
        if keysym is None:
            return
        self._pressed[ident] = keysym
        self.client.send_key(keysym, True)

    def keyReleaseEvent(self, event) -> None:
        event.accept()
        if event.isAutoRepeat():
            return
        ident = event.nativeScanCode() or int(event.key())
        keysym = self._pressed.pop(ident, None)
        if keysym is not None:
            self.client.send_key(keysym, False)

    def send_combo(self, keysyms: list[int]) -> None:
        """Press keys in order, then release them in reverse (e.g. Cmd+Tab)."""
        for keysym in keysyms:
            self.client.send_key(keysym, True)
        for keysym in reversed(keysyms):
            self.client.send_key(keysym, False)

    def release_all_keys(self) -> None:
        for keysym in self._pressed.values():
            self.client.send_key(keysym, False)
        self._pressed.clear()

    def focusOutEvent(self, event) -> None:
        self.release_all_keys()
        super().focusOutEvent(event)
