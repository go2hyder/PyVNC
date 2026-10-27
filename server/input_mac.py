"""Inject remote keyboard and mouse events into macOS with Quartz CGEvents."""
from __future__ import annotations

import ctypes
import time

import Quartz

from common import rfb

# US ANSI virtual keycodes (Carbon kVK_*)
_CHAR_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    " ": 49, "`": 50,
}
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
    '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}
_SPECIAL_KEYCODES = {
    rfb.XK_Return: 36, rfb.XK_Tab: 48, rfb.XK_BackSpace: 51, rfb.XK_Escape: 53,
    rfb.XK_Delete: 117, rfb.XK_Home: 115, rfb.XK_End: 119, rfb.XK_Page_Up: 116,
    rfb.XK_Page_Down: 121, rfb.XK_Left: 123, rfb.XK_Right: 124, rfb.XK_Down: 125,
    rfb.XK_Up: 126, rfb.XK_Insert: 114, rfb.XK_Help: 114, rfb.XK_Caps_Lock: 57,
    rfb.XK_KP_Enter: 76, rfb.XK_KP_Multiply: 67, rfb.XK_KP_Add: 69,
    rfb.XK_KP_Subtract: 78, rfb.XK_KP_Decimal: 65, rfb.XK_KP_Divide: 75,
    rfb.XK_KP_Equal: 81, rfb.XK_Num_Lock: 71,
}
for _i, _code in enumerate([82, 83, 84, 85, 86, 87, 88, 89, 91, 92]):
    _SPECIAL_KEYCODES[rfb.XK_KP_0 + _i] = _code
for _i, _code in enumerate([122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111,
                            105, 107, 113, 106, 64, 79, 80, 90]):
    _SPECIAL_KEYCODES[rfb.XK_F1 + _i] = _code

SHIFT, CONTROL, OPTION, COMMAND = "shift", "control", "option", "command"

_MODIFIER_KEYS = {
    rfb.XK_Shift_L: (SHIFT, 56), rfb.XK_Shift_R: (SHIFT, 60),
    rfb.XK_Control_L: (CONTROL, 59), rfb.XK_Control_R: (CONTROL, 62),
    rfb.XK_Alt_L: (OPTION, 58), rfb.XK_Alt_R: (OPTION, 61),
    rfb.XK_ISO_Level3_Shift: (OPTION, 61),
    rfb.XK_Super_L: (COMMAND, 55), rfb.XK_Super_R: (COMMAND, 54),
    rfb.XK_Meta_L: (COMMAND, 55), rfb.XK_Meta_R: (COMMAND, 54),
}
_SWAPPED = {
    rfb.XK_Control_L: rfb.XK_Super_L, rfb.XK_Control_R: rfb.XK_Super_R,
    rfb.XK_Super_L: rfb.XK_Control_L, rfb.XK_Super_R: rfb.XK_Control_R,
}
_FLAG_MASKS = {
    SHIFT: Quartz.kCGEventFlagMaskShift,
    CONTROL: Quartz.kCGEventFlagMaskControl,
    OPTION: Quartz.kCGEventFlagMaskAlternate,
    COMMAND: Quartz.kCGEventFlagMaskCommand,
}

_BUTTONS = [
    (rfb.BTN_LEFT, Quartz.kCGMouseButtonLeft, Quartz.kCGEventLeftMouseDown,
     Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged),
    (rfb.BTN_RIGHT, Quartz.kCGMouseButtonRight, Quartz.kCGEventRightMouseDown,
     Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged),
    (rfb.BTN_MIDDLE, Quartz.kCGMouseButtonCenter, Quartz.kCGEventOtherMouseDown,
     Quartz.kCGEventOtherMouseUp, Quartz.kCGEventOtherMouseDragged),
]

DOUBLE_CLICK_SECONDS = 0.5
DOUBLE_CLICK_DISTANCE = 4.0


def accessibility_trusted() -> bool:
    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except OSError:
        return True


def screen_recording_allowed(request: bool = False) -> bool:
    """Check Screen Recording permission; with request=True macOS may show its prompt."""
    preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    if preflight is None:
        return True
    if preflight():
        return True
    ask = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
    if request and ask is not None:
        ask()
    return False


class InputInjector:
    def __init__(self, swap_ctrl_cmd: bool = False, scroll_lines: int = 3):
        self.swap_ctrl_cmd = swap_ctrl_cmd
        self.scroll_lines = scroll_lines
        self._modifiers: dict[str, int] = {m: 0 for m in _FLAG_MASKS}
        self._pressed: dict[int, int | None] = {}
        self._mask = 0
        self._pos: tuple[float, float] | None = None
        self._click_state: dict[int, tuple[float, tuple[float, float], int]] = {}

    def _flags(self) -> int:
        flags = 0
        for mod, count in self._modifiers.items():
            if count > 0:
                flags |= _FLAG_MASKS[mod]
        return flags

    @staticmethod
    def _post(event) -> None:
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    # -- keyboard ----------------------------------------------------------

    def key(self, keysym: int, down: bool) -> None:
        if self.swap_ctrl_cmd:
            keysym = _SWAPPED.get(keysym, keysym)

        if keysym in _MODIFIER_KEYS:
            mod, keycode = _MODIFIER_KEYS[keysym]
            if down:
                if self._pressed.get(keysym) is not None:
                    return
                self._pressed[keysym] = keycode
                self._modifiers[mod] += 1
            else:
                if self._pressed.pop(keysym, None) is None:
                    return
                self._modifiers[mod] = max(0, self._modifiers[mod] - 1)
            ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
            Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
            Quartz.CGEventSetFlags(ev, self._flags())
            self._post(ev)
            return

        if not down and keysym not in self._pressed:
            return

        keycode = _SPECIAL_KEYCODES.get(keysym)
        char = None
        if keycode is None:
            char = rfb.keysym_to_char(keysym)
            if char is None:
                return
            base = _SHIFTED.get(char, char.lower())
            keycode = _CHAR_KEYCODES.get(base)

        if down:
            self._pressed[keysym] = keycode
        else:
            self._pressed.pop(keysym, None)

        ev = Quartz.CGEventCreateKeyboardEvent(None, keycode if keycode is not None else 0, down)
        Quartz.CGEventSetFlags(ev, self._flags())
        shortcut = self._modifiers[COMMAND] or self._modifiers[CONTROL] or self._modifiers[OPTION]
        if char is not None and (keycode is None or not shortcut):
            # Typing the exact character keeps text correct on any Mac keyboard layout.
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(char.encode("utf-16-le")) // 2, char)
        self._post(ev)

    def release_all(self) -> None:
        for keysym in list(self._pressed):
            self.key(keysym, False)
        if self._pos is not None and self._mask:
            self.pointer(0, self._pos)

    # -- mouse -------------------------------------------------------------

    def pointer(self, mask: int, pos: tuple[float, float]) -> None:
        moved = pos != self._pos
        self._pos = pos
        changed = mask ^ self._mask
        flags = self._flags()

        if moved and not (changed & (rfb.BTN_LEFT | rfb.BTN_RIGHT | rfb.BTN_MIDDLE)):
            event_type, button = Quartz.kCGEventMouseMoved, Quartz.kCGMouseButtonLeft
            for bit, btn, _down, _up, dragged in _BUTTONS:
                if mask & bit:
                    event_type, button = dragged, btn
                    break
            ev = Quartz.CGEventCreateMouseEvent(None, event_type, pos, button)
            Quartz.CGEventSetFlags(ev, flags)
            self._post(ev)

        for bit, btn, down_type, up_type, _dragged in _BUTTONS:
            if not changed & bit:
                continue
            is_down = bool(mask & bit)
            clicks = self._click_count(btn, pos) if is_down else self._click_state.get(btn, (0, pos, 1))[2]
            ev = Quartz.CGEventCreateMouseEvent(None, down_type if is_down else up_type, pos, btn)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
            Quartz.CGEventSetFlags(ev, flags)
            self._post(ev)

        pressed = mask & ~self._mask
        dy = (1 if pressed & rfb.BTN_WHEEL_UP else 0) - (1 if pressed & rfb.BTN_WHEEL_DOWN else 0)
        dx = (1 if pressed & rfb.BTN_WHEEL_LEFT else 0) - (1 if pressed & rfb.BTN_WHEEL_RIGHT else 0)
        if dx or dy:
            ev = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 2, dy * self.scroll_lines, dx * self.scroll_lines)
            Quartz.CGEventSetFlags(ev, flags)
            self._post(ev)

        self._mask = mask

    def _click_count(self, button: int, pos: tuple[float, float]) -> int:
        now = time.monotonic()
        last_time, last_pos, count = self._click_state.get(button, (0.0, pos, 0))
        near = abs(pos[0] - last_pos[0]) <= DOUBLE_CLICK_DISTANCE and abs(pos[1] - last_pos[1]) <= DOUBLE_CLICK_DISTANCE
        count = count + 1 if (now - last_time) <= DOUBLE_CLICK_SECONDS and near else 1
        self._click_state[button] = (now, pos, count)
        return count
