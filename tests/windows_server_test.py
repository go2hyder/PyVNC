"""Windows key mapping and clipboard file lists. Run: python -m tests.windows_server_test

Does not post keystrokes or change the real clipboard.
"""
from __future__ import annotations

import ctypes

from common import rfb
from server.clipboard_win import pack_drop_files, unpack_drop_files
from server.input_win import (
    VK_LCONTROL, VK_LWIN, VK_RETURN, _INPUT, to_absolute, translate_key,
)


def check_keys() -> None:
    assert translate_key(rfb.XK_Return, False) == ("vk", VK_RETURN)
    assert translate_key(rfb.XK_Control_L, False) == ("vk", VK_LCONTROL)
    assert translate_key(ord("c"), False) == ("unicode", ord("c"))
    assert translate_key(ord("c"), True) == ("vk", ord("C"))
    assert translate_key(ord("C"), True) == ("vk", ord("C"))
    assert translate_key(rfb.XK_Control_L, False, swap_ctrl_cmd=True) == ("vk", VK_LWIN)
    assert translate_key(rfb.XK_Super_L, False, swap_ctrl_cmd=True) == ("vk", VK_LCONTROL)
    assert translate_key(0x01000000 | 0x1F600, False) == ("unicode", 0x1F600)
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(_INPUT) == 40
    print("  keys ok")


def check_pointer() -> None:
    assert to_absolute(0, 0, (0, 0), (1920, 1080)) == (0, 0)
    assert to_absolute(1919, 1079, (0, 0), (1920, 1080)) == (65535, 65535)
    assert to_absolute(-1920, 100, (-1920, 0), (1920, 1080)) == (0, int(round(100 * 65535 / 1079)))
    print("  pointer ok")


def check_clipboard_list() -> None:
    folder = r"C:\Users\me\Downloads\PyVNC\Photos"
    nested = folder + r"\trip\shot.txt"
    payload = pack_drop_files([folder, nested])
    assert unpack_drop_files(payload) == [folder, nested]
    assert unpack_drop_files(b"") == []
    assert unpack_drop_files(payload[:10]) == []
    print("  clipboard list ok")


if __name__ == "__main__":
    check_keys()
    check_pointer()
    check_clipboard_list()
    print("windows server ok")
