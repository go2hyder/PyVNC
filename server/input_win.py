"""Inject remote keyboard and mouse events on Windows with SendInput."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from common import rfb

# Virtual keys. https://learn.microsoft.com/windows/win32/inputdev/virtual-key-codes
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_SNAPSHOT = 0x2C
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_APPS = 0x5D
VK_NUMPAD0 = 0x60
VK_MULTIPLY = 0x6A
VK_ADD = 0x6B
VK_SUBTRACT = 0x6D
VK_DECIMAL = 0x6E
VK_DIVIDE = 0x6F
VK_F1 = 0x70
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_OEM_1 = 0xBA
VK_OEM_PLUS = 0xBB
VK_OEM_COMMA = 0xBC
VK_OEM_MINUS = 0xBD
VK_OEM_PERIOD = 0xBE
VK_OEM_2 = 0xBF
VK_OEM_3 = 0xC0
VK_OEM_4 = 0xDB
VK_OEM_5 = 0xDC
VK_OEM_6 = 0xDD
VK_OEM_7 = 0xDE

_MODIFIER_KEYS = {
    rfb.XK_Shift_L: VK_LSHIFT, rfb.XK_Shift_R: VK_RSHIFT,
    rfb.XK_Control_L: VK_LCONTROL, rfb.XK_Control_R: VK_RCONTROL,
    rfb.XK_Alt_L: VK_LMENU, rfb.XK_Alt_R: VK_RMENU,
    rfb.XK_ISO_Level3_Shift: VK_RMENU,
    rfb.XK_Super_L: VK_LWIN, rfb.XK_Super_R: VK_RWIN,
    rfb.XK_Meta_L: VK_LWIN, rfb.XK_Meta_R: VK_RWIN,
}
_SHORTCUT_MODIFIERS = {VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN}
_SWAPPED = {
    rfb.XK_Control_L: rfb.XK_Super_L, rfb.XK_Control_R: rfb.XK_Super_R,
    rfb.XK_Super_L: rfb.XK_Control_L, rfb.XK_Super_R: rfb.XK_Control_R,
}
_SPECIAL_KEYS = {
    rfb.XK_Return: VK_RETURN, rfb.XK_Tab: VK_TAB, rfb.XK_BackSpace: VK_BACK,
    rfb.XK_Escape: VK_ESCAPE, rfb.XK_Delete: VK_DELETE, rfb.XK_Home: VK_HOME,
    rfb.XK_End: VK_END, rfb.XK_Page_Up: VK_PRIOR, rfb.XK_Page_Down: VK_NEXT,
    rfb.XK_Left: VK_LEFT, rfb.XK_Right: VK_RIGHT, rfb.XK_Down: VK_DOWN, rfb.XK_Up: VK_UP,
    rfb.XK_Insert: VK_INSERT, rfb.XK_Help: VK_INSERT, rfb.XK_Caps_Lock: VK_CAPITAL,
    rfb.XK_KP_Enter: VK_RETURN, rfb.XK_KP_Multiply: VK_MULTIPLY, rfb.XK_KP_Add: VK_ADD,
    rfb.XK_KP_Subtract: VK_SUBTRACT, rfb.XK_KP_Decimal: VK_DECIMAL, rfb.XK_KP_Divide: VK_DIVIDE,
    rfb.XK_Num_Lock: VK_NUMLOCK, rfb.XK_Print: VK_SNAPSHOT, rfb.XK_Pause: VK_PAUSE,
    rfb.XK_Scroll_Lock: VK_SCROLL, rfb.XK_Menu: VK_APPS,
}
for _i in range(10):
    _SPECIAL_KEYS[rfb.XK_KP_0 + _i] = VK_NUMPAD0 + _i
for _i in range(24):
    _SPECIAL_KEYS[rfb.XK_F1 + _i] = VK_F1 + _i

# US virtual keys, used only while Ctrl, Alt, or the Windows key is held.
_SYMBOL_VK = {
    " ": VK_SPACE, "-": VK_OEM_MINUS, "=": VK_OEM_PLUS, "[": VK_OEM_4, "]": VK_OEM_6,
    "\\": VK_OEM_5, ";": VK_OEM_1, "'": VK_OEM_7, ",": VK_OEM_COMMA, ".": VK_OEM_PERIOD,
    "/": VK_OEM_2, "`": VK_OEM_3,
}
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
    '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}

_KEYUP = 0x0002
_UNICODE = 0x0004
_MOUSE_MOVE = 0x0001
_LEFTDOWN, _LEFTUP = 0x0002, 0x0004
_RIGHTDOWN, _RIGHTUP = 0x0008, 0x0010
_MIDDLEDOWN, _MIDDLEUP = 0x0020, 0x0040
_WHEEL, _HWHEEL = 0x0800, 0x01000
_ABSOLUTE = 0x8000
_VIRTUALDESK = 0x4000
_WHEEL_DELTA = 120
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77
_SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 78, 79

_BUTTONS = (
    (rfb.BTN_LEFT, _LEFTDOWN, _LEFTUP),
    (rfb.BTN_RIGHT, _RIGHTDOWN, _RIGHTUP),
    (rfb.BTN_MIDDLE, _MIDDLEDOWN, _MIDDLEUP),
)

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", _INPUTUNION))


def translate_key(keysym: int, shortcut: bool, swap_ctrl_cmd: bool = False) -> tuple[str, int] | None:
    """Return ('vk', virtual_key) or ('unicode', codepoint) for one keysym.

    Printable characters are unicode so the remote keyboard layout still types
    the right letter. Ctrl, Alt, and the Windows key use a virtual key so
    shortcuts such as Ctrl+C stay shortcuts.
    """
    if swap_ctrl_cmd:
        keysym = _SWAPPED.get(keysym, keysym)
    if keysym in _MODIFIER_KEYS:
        return ("vk", _MODIFIER_KEYS[keysym])
    special = _SPECIAL_KEYS.get(keysym)
    if special is not None:
        return ("vk", special)
    char = rfb.keysym_to_char(keysym)
    if char is None:
        return None
    if shortcut:
        base = _SHIFTED.get(char, char.lower())
        if "a" <= base <= "z":
            return ("vk", ord(base.upper()))
        if "0" <= base <= "9":
            return ("vk", ord(base))
        vk = _SYMBOL_VK.get(base)
        return ("vk", vk) if vk is not None else None
    return ("unicode", ord(char))


def to_absolute(x: float, y: float, origin: tuple[int, int], size: tuple[int, int]) -> tuple[int, int]:
    """Map a global pixel to the 0..65535 virtual-desktop range SendInput expects."""
    ox, oy = origin
    width, height = size
    if width <= 1 or height <= 1:
        return 0, 0
    ax = int(round((x - ox) * 65535 / (width - 1)))
    ay = int(round((y - oy) * 65535 / (height - 1)))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _send(items: list[_INPUT]) -> None:
    if not items:
        return
    array = (_INPUT * len(items))(*items)
    ctypes.windll.user32.SendInput(len(items), array, ctypes.sizeof(_INPUT))


def _key_input(vk: int, scan: int, flags: int) -> _INPUT:
    event = _INPUT(type=1)
    event.union.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


def _mouse_input(x: int, y: int, data: int, flags: int) -> _INPUT:
    event = _INPUT(type=0)
    event.union.mi = _MOUSEINPUT(x, y, data, flags, 0, 0)
    return event


class InputInjector:
    def __init__(self, swap_ctrl_cmd: bool = False, scroll_lines: int = 3):
        self.swap_ctrl_cmd = swap_ctrl_cmd
        self.scroll_lines = scroll_lines
        self._pressed: dict[int, tuple[str, int]] = {}
        self._modifiers: dict[int, int] = {}
        self._mask = 0
        self._pos: tuple[float, float] | None = None

    def key(self, keysym: int, down: bool) -> None:
        if not down:
            action = self._pressed.pop(keysym, None)
            if action is None:
                return
            if action[1] in _MODIFIER_KEYS.values() and action[0] == "vk":
                self._modifiers[action[1]] = max(0, self._modifiers.get(action[1], 0) - 1)
            self._emit(action, False)
            return
        if keysym in self._pressed:
            return
        action = translate_key(keysym, self._shortcut_down(), self.swap_ctrl_cmd)
        if action is None:
            return
        self._pressed[keysym] = action
        if action[0] == "vk" and action[1] in _MODIFIER_KEYS.values():
            self._modifiers[action[1]] = self._modifiers.get(action[1], 0) + 1
        self._emit(action, True)

    def release_all(self) -> None:
        for keysym in list(self._pressed):
            self.key(keysym, False)
        if self._pos is not None and self._mask:
            self.pointer(0, self._pos)

    def pointer(self, mask: int, pos: tuple[float, float]) -> None:
        moved = pos != self._pos
        self._pos = pos
        changed = mask ^ self._mask
        ax, ay = self._screen_point(pos)
        items: list[_INPUT] = []
        move_flags = _MOUSE_MOVE | _ABSOLUTE | _VIRTUALDESK
        if moved:
            items.append(_mouse_input(ax, ay, 0, move_flags))
        for bit, down_flag, up_flag in _BUTTONS:
            if changed & bit:
                items.append(_mouse_input(ax, ay, 0, move_flags | (down_flag if mask & bit else up_flag)))
        pressed = mask & ~self._mask
        dy = (1 if pressed & rfb.BTN_WHEEL_UP else 0) - (1 if pressed & rfb.BTN_WHEEL_DOWN else 0)
        dx = (1 if pressed & rfb.BTN_WHEEL_LEFT else 0) - (1 if pressed & rfb.BTN_WHEEL_RIGHT else 0)
        if dy:
            items.append(_mouse_input(ax, ay, dy * self.scroll_lines * _WHEEL_DELTA, _WHEEL))
        if dx:
            items.append(_mouse_input(ax, ay, dx * self.scroll_lines * _WHEEL_DELTA, _HWHEEL))
        self._mask = mask
        _send(items)

    def _shortcut_down(self) -> bool:
        return any(self._modifiers.get(vk, 0) > 0 for vk in _SHORTCUT_MODIFIERS)

    def _emit(self, action: tuple[str, int], down: bool) -> None:
        kind, value = action
        if kind == "vk":
            _send([_key_input(value, 0, 0 if down else _KEYUP)])
            return
        encoded = chr(value).encode("utf-16-le")
        units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
        flags = _UNICODE if down else _UNICODE | _KEYUP
        _send([_key_input(0, unit, flags) for unit in units])

    @staticmethod
    def _screen_point(pos: tuple[float, float]) -> tuple[int, int]:
        user32 = ctypes.windll.user32
        origin = (user32.GetSystemMetrics(_SM_XVIRTUALSCREEN), user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
        size = (user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
        return to_absolute(pos[0], pos[1], origin, size)
