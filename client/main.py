"""PyVNC Viewer (built for Windows; also runs on macOS and Linux).

Run from the project root:  python -m client.main
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from client.launcher import LauncherWindow, apply_theme
from client.store import Preferences


def _icon() -> QIcon | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for name in ("PyVNC.ico", "PyVNC.png"):
        path = base / "assets" / name
        if path.exists():
            return QIcon(str(path))
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.platform == "win32":
        import ctypes
        # Own taskbar identity, so Windows shows our icon instead of python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PyVNC.Viewer")
    app = QApplication(sys.argv)
    app.setApplicationName("PyVNC Viewer")
    app.setOrganizationName("PyVNC")
    icon = _icon()
    if icon is not None:
        app.setWindowIcon(icon)
    apply_theme(app, Preferences().theme)
    window = LauncherWindow(app, icon)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
