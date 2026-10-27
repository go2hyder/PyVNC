"""Helpers for the menu bar app: network addresses, Keychain password, open-at-login."""
from __future__ import annotations

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


def is_open_at_login() -> bool:
    return LOGIN_AGENT_PATH.exists()


def set_open_at_login(enabled: bool) -> None:
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
