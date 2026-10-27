"""PyVNC server command-line version (the tray app is server/app.py).

Run from the project root:  python -m server.main --help
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import subprocess
import sys
from pathlib import Path

from server.controller import ServerController, ServerSettings

log = logging.getLogger("pyvnc")

PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_{}"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PyVNC server (RFB 3.8 over TLS)")
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    p.add_argument("--port", type=int, default=5900)
    p.add_argument("--password", help="password (default: $PYVNC_PASSWORD, otherwise prompt)")
    p.add_argument("--password-file", type=Path, help="read the password from this file (for launchd)")
    p.add_argument("--display", type=int, default=1, help="display number, 1 = main display")
    p.add_argument("--scale", type=float, default=1.0, help="downscale factor, e.g. 0.5 for slow links")
    p.add_argument("--fps", type=float, default=20.0, help="maximum frames per second")
    p.add_argument("--quality", type=int, help="force JPEG quality 1-95 (default: what the viewer asks for)")
    p.add_argument("--view-only", action="store_true", help="ignore keyboard and mouse input")
    p.add_argument("--swap-ctrl-cmd", action="store_true",
                   help="treat the viewer's Ctrl as Command and Win/Super as Control")
    p.add_argument("--no-clipboard", action="store_true", help="disable clipboard sync")
    p.add_argument("--no-caffeinate", action="store_true", help="allow the computer to sleep while serving")
    p.add_argument("--insecure-allow-plain", action="store_true",
                   help="also offer unencrypted VNC auth (only for LAN testing with old viewers)")
    p.add_argument("--cert-dir", type=Path, help="where server-cert.pem / server-key.pem live")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def get_password(args: argparse.Namespace) -> str:
    password = args.password or os.environ.get("PYVNC_PASSWORD")
    if not password and args.password_file:
        password = args.password_file.expanduser().read_text().strip()
    if not password:
        if not sys.stdin.isatty():
            sys.exit("No password: pass --password or set PYVNC_PASSWORD")
        password = getpass.getpass("Set a password for this session: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match")
    if len(password) < 8:
        sys.exit("Use a password of at least 8 characters")
    return password


def check_permissions(view_only: bool) -> None:
    from server.input_mac import accessibility_trusted, screen_recording_allowed

    if not screen_recording_allowed(request=True):
        log.error("Screen Recording permission is missing. Enable it for your terminal/Python in "
                  "System Settings > Privacy & Security > Screen Recording, then restart the server.")
        subprocess.run(["open", PRIVACY_URL.format("ScreenCapture")], check=False)
    if not view_only and not accessibility_trusted():
        log.error("Accessibility permission is missing, so keyboard/mouse control will not work. "
                  "Enable it in System Settings > Privacy & Security > Accessibility, then restart.")
        subprocess.run(["open", PRIVACY_URL.format("Accessibility")], check=False)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if sys.platform not in ("darwin", "win32"):
        sys.exit("The PyVNC server runs on macOS and Windows")
    settings = ServerSettings(
        password=get_password(args),
        host=args.host,
        port=args.port,
        display=args.display,
        scale=args.scale,
        fps=args.fps,
        quality=args.quality,
        view_only=args.view_only,
        swap_ctrl_cmd=args.swap_ctrl_cmd,
        clipboard=not args.no_clipboard,
        keep_awake=not args.no_caffeinate,
        allow_insecure=args.insecure_allow_plain,
        cert_dir=args.cert_dir,
    )
    if sys.platform == "darwin":
        check_permissions(args.view_only)

    controller = ServerController()
    try:
        controller.start(settings)
    except RuntimeError as exc:
        sys.exit(str(exc))
    log.info("Check that the viewer shows the fingerprint above the first time you connect.")
    try:
        controller.wait()
    except KeyboardInterrupt:
        controller.stop()
        log.info("Stopped")


if __name__ == "__main__":
    main()
