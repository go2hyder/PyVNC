"""Helpers for the menu bar app: network addresses, Keychain password, open-at-login."""
from __future__ import annotations

import ctypes
import ipaddress
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

KEYCHAIN_SERVICE = "PyVNC Server"
KEYCHAIN_ACCOUNT = "vnc-password"
LOGIN_AGENT_LABEL = "com.pyvnc.server.app"
LOGIN_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_AGENT_LABEL}.plist"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_CLI = ["tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# -- addresses ---------------------------------------------------------------

def local_addresses() -> list[tuple[str, str]]:
    """Return (interface, IPv4) pairs for active non-loopback interfaces."""
    if sys.platform == "win32":
        return _windows_addresses()
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    result, iface = [], ""
    for line in out.splitlines():
        header = re.match(r"^(\S+):", line)
        if header:
            iface = header.group(1)
            continue
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if m and not m.group(1).startswith("127."):
            result.append((iface, m.group(1)))
    return result


def is_tailscale(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_NET
    except ValueError:
        return False


def tailscale_ip() -> str | None:
    for _iface, ip in local_addresses():
        if is_tailscale(ip):
            return ip
    for cli in TAILSCALE_CLI:
        if shutil.which(cli) or Path(cli).exists():
            try:
                out = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            ip = out.strip().splitlines()[0] if out.strip() else ""
            if is_tailscale(ip):
                return ip
    return None


# -- password ----------------------------------------------------------------

def load_password() -> str:
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) or ""
    except Exception:
        return ""


def save_password(password: str) -> None:
    import keyring
    if password:
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, password)
    else:
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        except Exception:
            pass


def generate_password() -> str:
    return secrets.token_urlsafe(15)


# -- open at login -----------------------------------------------------------

def _app_bundle() -> Path | None:
    """The enclosing .app when running as a packaged app."""
    if not getattr(sys, "frozen", False):
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def _login_program() -> tuple[list[str], str | None]:
    bundle = _app_bundle()
    if bundle is not None:
        return ["/usr/bin/open", "-a", str(bundle)], None
    return [sys.executable, "-m", "server.app", "--background"], str(PROJECT_ROOT)


_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_RUN_NAME = "PyVNC Server"


def _windows_login_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    root = str(PROJECT_ROOT)
    return f'cmd /c cd /d "{root}" && "{sys.executable}" -m server.app --background'


def is_open_at_login() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
                winreg.QueryValueEx(key, _WIN_RUN_NAME)
            return True
        except OSError:
            return False
    return LOGIN_AGENT_PATH.exists()


def set_open_at_login(enabled: bool) -> None:
    if sys.platform == "win32":
        _set_windows_login(enabled)
        return
    if not enabled:
        LOGIN_AGENT_PATH.unlink(missing_ok=True)
        return
    args, cwd = _login_program()
    plist = {"Label": LOGIN_AGENT_LABEL, "ProgramArguments": args, "RunAtLoad": True,
             "ProcessType": "Interactive"}
    if cwd:
        plist["WorkingDirectory"] = cwd
    LOGIN_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOGIN_AGENT_PATH, "wb") as f:
        plistlib.dump(plist, f)


# -- macOS app behaviour -----------------------------------------------------

def _set_windows_login(enabled: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _WIN_RUN_NAME, 0, winreg.REG_SZ, _windows_login_command())
        else:
            try:
                winreg.DeleteValue(key, _WIN_RUN_NAME)
            except OSError:
                pass


def _windows_addresses() -> list[tuple[str, str]]:
    """IPv4 addresses from GetAdaptersAddresses. Skips loopback."""
    class _SockAddr(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 14)]

    class _SocketAddress(ctypes.Structure):
        _fields_ = [("addr", ctypes.POINTER(_SockAddr)), ("length", ctypes.c_int)]

    class _Unicast(ctypes.Structure):
        pass

    _Unicast._fields_ = [
        ("length", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("next", ctypes.POINTER(_Unicast)),
        ("address", _SocketAddress),
    ]

    class _Adapter(ctypes.Structure):
        pass

    _Adapter._fields_ = [
        ("length", ctypes.c_ulong),
        ("index", ctypes.c_ulong),
        ("next", ctypes.POINTER(_Adapter)),
        ("name", ctypes.c_char_p),
        ("unicast", ctypes.POINTER(_Unicast)),
        ("anycast", ctypes.c_void_p),
        ("multicast", ctypes.c_void_p),
        ("dns", ctypes.c_void_p),
        ("suffix", ctypes.c_wchar_p),
        ("description", ctypes.c_wchar_p),
        ("friendly", ctypes.c_wchar_p),
    ]

    iphlpapi = ctypes.windll.iphlpapi
    iphlpapi.GetAdaptersAddresses.argtypes = [
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    iphlpapi.GetAdaptersAddresses.restype = ctypes.c_ulong
    size = ctypes.c_ulong(16 * 1024)
    for _ in range(2):
        buf = ctypes.create_string_buffer(size.value)
        # AF_INET, skip anycast, multicast, and DNS server addresses.
        err = iphlpapi.GetAdaptersAddresses(
            2, 0x0002 | 0x0004 | 0x0008, None, ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(size))
        if err == 0:
            break
        if err != 111:  # ERROR_BUFFER_OVERFLOW
            return []
    else:
        return []

    found: list[tuple[str, str]] = []
    adapter = ctypes.cast(buf, ctypes.POINTER(_Adapter))
    while adapter:
        item = adapter.contents
        label = item.friendly or item.description or "Ethernet"
        node = item.unicast
        while node:
            addr = node.contents.address
            if addr.addr and addr.addr.contents.family == 2:
                raw = bytes(addr.addr.contents.data[2:6])
                ip = ".".join(str(b) for b in raw)
                if ip and not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0":
                    found.append((label, ip))
            node = node.contents.next
        adapter = item.next
    return found


def hide_dock_icon() -> None:
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


def bring_to_front() -> None:
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def open_privacy_settings(pane: str) -> None:
    subprocess.run(["open", f"x-apple.systempreferences:com.apple.preference.security?Privacy_{pane}"],
                   check=False)
