"""One remote-screen window per connection."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QMimeData, QObject, QStandardPaths, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QScrollArea,
    QToolButton,
)

from client.connection import QUALITY_PRESETS, Callbacks, ConnectOptions, RFBClient
from client.store import Connection
from client.view import VncView
from common import rfb

SEND_KEYS = [
    ("Cmd+Tab (switch apps)", [rfb.XK_Super_L, rfb.XK_Tab]),
    ("Cmd+Space (Spotlight)", [rfb.XK_Super_L, ord(" ")]),
    ("Cmd+W (close window)", [rfb.XK_Super_L, ord("w")]),
    ("Cmd+Q (quit app)", [rfb.XK_Super_L, ord("q")]),
    ("Cmd+Opt+Esc (Force Quit)", [rfb.XK_Super_L, rfb.XK_Alt_L, rfb.XK_Escape]),
    ("Ctrl+Up (Mission Control)", [rfb.XK_Control_L, rfb.XK_Up]),
    ("Ctrl+Cmd+Q (lock screen)", [rfb.XK_Control_L, rfb.XK_Super_L, ord("q")]),
    ("Cmd+Shift+3 (Mac screenshot)", [rfb.XK_Super_L, rfb.XK_Shift_L, ord("3")]),
]
SCALES = [("Fit to window", None), ("100%", 1.0), ("75%", 0.75), ("50%", 0.5)]


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class Bridge(QObject):
    """Moves callbacks from the network thread onto the Qt GUI thread."""
    connected = pyqtSignal(str, object)
    resized = pyqtSignal(object)
    updated = pyqtSignal(int, int, int, int)
    clipboard = pyqtSignal(str)
    bell = pyqtSignal()
    closed = pyqtSignal(object)
    security = pyqtSignal(str)
    cert_request = pyqtSignal(object)
    file_status = pyqtSignal(str)
    file_progress = pyqtSignal(str, str, int, int)
    files_received = pyqtSignal(object)

    def callbacks(self) -> Callbacks:
        return Callbacks(
            on_connected=self.connected.emit,
            on_resize=self.resized.emit,
            on_update=self.updated.emit,
            on_clipboard=self.clipboard.emit,
            on_bell=self.bell.emit,
            on_closed=self.closed.emit,
            on_security=self.security.emit,
            on_file_status=self.file_status.emit,
            on_file_progress=self.file_progress.emit,
            on_files=self.files_received.emit,
            confirm_certificate=self._confirm_certificate,
        )

    def _confirm_certificate(self, host_port: str, fingerprint: str, known: str | None) -> bool:
        request = {"host_port": host_port, "fingerprint": fingerprint, "known": known,
                   "done": threading.Event(), "accepted": False}
        self.cert_request.emit(request)
        request["done"].wait()
        return request["accepted"]


class ViewerWindow(QMainWindow):
    connected_ok = pyqtSignal(str)   # connection id
    finished = pyqtSignal(object)    # self

    def __init__(self, conn: Connection, password: str, icon: QIcon | None = None):
        super().__init__()
        self.conn = conn
        self.password = password
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if icon is not None:
            self.setWindowIcon(icon)

        self._security = ""
        self._was_connected = False
        self._user_closed = False
        self._finished = False
        self._last_remote_clipboard: str | None = None
        self._remote_files: frozenset[str] = frozenset()
        self._ignore_files_until = 0.0
        self._transfer_note = ""
        self.client: RFBClient | None = None
        self.bridge: Bridge | None = None

        self.view = VncView(None)
        self.view.ctrl_as_cmd = conn.ctrl_as_cmd
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.view)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setStyleSheet("QScrollArea { background: black; border: none; }")
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setCentralWidget(self.scroll)

        self._build_toolbar()
        self.status = QLabel()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(160)
        self.progress.setFixedHeight(16)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.progress)
        QGuiApplication.clipboard().dataChanged.connect(self._on_local_clipboard)

        self.resize(1024, 700)
        self._connect()

    # -- connection lifecycle ----------------------------------------------

    def _connect(self) -> None:
        self.setWindowTitle(f"{self.conn.display_name} - connecting...")
        self._security = ""
        self._was_connected = False
        self._transfer_note = ""
        self.progress.hide()
        self.progress.setValue(0)
        self.status.setText(f"Connecting to {self.conn.host}:{self.conn.port}...")
        options = ConnectOptions(
            host=self.conn.host, port=self.conn.port, password=self.password,
            username=self.conn.username or "pyvnc",
            quality_level=QUALITY_PRESETS.get(self.conn.quality, 5),
            allow_insecure=self.conn.allow_insecure,
            share_clipboard=self.conn.share_clipboard,
        )
        self.bridge = Bridge()
        self.client = RFBClient(options, self.bridge.callbacks())
        self.view.client = self.client
        b = self.bridge
        b.connected.connect(self._on_connected)
        b.resized.connect(self._on_resized)
        b.updated.connect(self.view.region_updated)
        b.clipboard.connect(self._on_remote_clipboard)
        b.bell.connect(QApplication.beep)
        b.closed.connect(self._on_closed)
        b.security.connect(self._on_security)
        b.cert_request.connect(self._on_cert_request)
        b.file_status.connect(self._on_file_status)
        b.file_progress.connect(self._on_file_progress)
        b.files_received.connect(self._on_remote_files)
        self.client.start()

    def _on_connected(self, name: str, fb) -> None:
        self._was_connected = True
        self.setWindowTitle(f"{self.conn.display_name} - {name}")
        first_frame = self.view.framebuffer_size() == (0, 0)
        self.view.set_framebuffer(fb)
        self._update_status()
        self.connected_ok.emit(self.conn.id)
        if first_frame and not self.isFullScreen():
            screen = self.screen().availableGeometry()
            h, w = fb.shape[:2]
            chrome_h = self.height() - self.scroll.height()
            self.resize(min(w + 2, screen.width() - 40), min(h + chrome_h + 2, screen.height() - 40))
        self.view.setFocus()

    def _on_resized(self, fb) -> None:
        self.view.set_framebuffer(fb)
        self._update_status()

    def _on_security(self, description: str) -> None:
        self._security = description
        self._update_status()

    def _update_status(self) -> None:
        w, h = self.view.framebuffer_size()
        parts = [f"{self.conn.host}:{self.conn.port}"]
        if w:
            parts.append(f"{w} x {h}")
        if self._security:
            parts.append(self._security)
        if self._transfer_note:
            parts.append(self._transfer_note)
        self.status.setText("   |   ".join(parts))

    def _on_closed(self, error) -> None:
        if self._user_closed:
            return
        if error is None:
            self.close()
            return
        if self._was_connected and "Authentication failed" not in error and "not trusted" not in error:
            answer = QMessageBox.question(
                self, "Connection lost", f"{error}\n\nReconnect to {self.conn.display_name}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer == QMessageBox.StandardButton.Yes:
                self._connect()
                return
        elif "not trusted" not in error:
            QMessageBox.critical(self, "PyVNC", error)
        self.close()

    def _on_cert_request(self, req: dict) -> None:
        if req["known"]:
            text = (f"WARNING: the certificate of {req['host_port']} has CHANGED.\n\n"
                    f"Previously trusted:\n{req['known']}\n\nNow presented:\n{req['fingerprint']}\n\n"
                    "This can mean someone is intercepting the connection, or the server's "
                    "certificate was regenerated. Only continue if you know why it changed.\n\n"
                    "Trust the new certificate?")
            icon, default = QMessageBox.Icon.Warning, QMessageBox.StandardButton.No
        else:
            text = (f"First connection to {req['host_port']}.\n\nServer certificate fingerprint (SHA-256):\n"
                    f"{req['fingerprint']}\n\nCompare it with the fingerprint shown in PyVNC Server "
                    "on the Mac (Status tab). Trust this server?")
            icon, default = QMessageBox.Icon.Question, QMessageBox.StandardButton.Yes
        box = QMessageBox(icon, "Verify server", text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(default)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        req["accepted"] = box.exec() == QMessageBox.StandardButton.Yes
        req["done"].set()

    # -- toolbar -----------------------------------------------------------

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("View")
        tb.setMovable(False)
        tb.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        scale_menu = QMenu(self)
        group = QActionGroup(self)
        for label, value in SCALES:
            action = QAction(label, self, checkable=True, checked=value is None)
            action.triggered.connect(lambda _=False, v=value: self._set_scale(v))
            group.addAction(action)
            scale_menu.addAction(action)
        tb.addWidget(self._menu_button("Scale", scale_menu))

        self.full_action = QAction("Full screen", self, checkable=True)
        self.full_action.toggled.connect(self._set_fullscreen)
        tb.addAction(self.full_action)

        keys_menu = QMenu(self)
        for label, combo in SEND_KEYS:
            keys_menu.addAction(label, lambda c=combo: self._send_combo(c))
        tb.addWidget(self._menu_button("Send keys", keys_menu))

        self.cmd_action = QAction("Ctrl = Command", self, checkable=True, checked=self.conn.ctrl_as_cmd)
        self.cmd_action.toggled.connect(lambda on: setattr(self.view, "ctrl_as_cmd", on))
        tb.addAction(self.cmd_action)

        shot = QAction("Screenshot...", self)
        shot.triggered.connect(self._save_screenshot)
        tb.addAction(shot)

        disconnect = QAction("Disconnect", self)
        disconnect.triggered.connect(self.close)
        tb.addAction(disconnect)

    def _menu_button(self, text: str, menu: QMenu) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _set_scale(self, scale: float | None) -> None:
        policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOff if scale is None else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        self.scroll.setHorizontalScrollBarPolicy(policy)
        self.scroll.setVerticalScrollBarPolicy(policy)
        self.scroll.setWidgetResizable(scale is None)
        self.view.set_scale(scale)
        self.view.setFocus()

    def _set_fullscreen(self, on: bool) -> None:
        if on:
            self.showFullScreen()
        else:
            self.showNormal()
        self.view.setFocus()

    def _send_combo(self, combo: list[int]) -> None:
        self.view.send_combo(combo)
        self.view.setFocus()

    def _save_screenshot(self) -> None:
        image = self.view.snapshot()
        if image is None:
            return
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        name = f"{self.conn.display_name} {time.strftime('%Y-%m-%d %H%M%S')}.png"
        path, _ = QFileDialog.getSaveFileName(self, "Save screenshot", str(Path(folder) / name),
                                              "PNG image (*.png)")
        if path:
            if not image.save(path):
                QMessageBox.warning(self, "PyVNC", f"Could not save {path}")

    # -- clipboard ---------------------------------------------------------

    def _on_remote_clipboard(self, text: str) -> None:
        if not self.conn.share_clipboard:
            return
        self._last_remote_clipboard = text
        QGuiApplication.clipboard().setText(text)

    def _on_file_status(self, text: str) -> None:
        self._transfer_note = text
        self.progress.hide()
        self._update_status()

    def _on_file_progress(self, phase: str, name: str, done: int, total: int) -> None:
        percent = 100 if total <= 0 else min(100, int(done * 100 / total))
        self.progress.setValue(percent)
        self.progress.show()
        verb = "Sending" if phase == "send" else "Receiving"
        self._transfer_note = f"{verb} {name}   {_format_size(done)} / {_format_size(total)}"
        self._update_status()

    def _on_remote_files(self, paths: list[str]) -> None:
        if not self.conn.share_clipboard or not paths:
            return
        self._remote_files = frozenset(os.path.normcase(os.path.abspath(path)) for path in paths)
        self._ignore_files_until = time.monotonic() + 1.0
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        QGuiApplication.clipboard().setMimeData(mime)

    def _local_files(self) -> list[str]:
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None or not mime.hasUrls():
            return []
        files = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                files.append(path)
        return files

    def _on_local_clipboard(self) -> None:
        if not self.conn.share_clipboard or self.client is None:
            return
        files = self._local_files()
        if files:
            key = frozenset(os.path.normcase(os.path.abspath(path)) for path in files)
            if key == self._remote_files and time.monotonic() < self._ignore_files_until:
                return
            self._remote_files = frozenset()
            self.client.send_files(files)
            return
        mime = QGuiApplication.clipboard().mimeData()
        if mime is not None and any(url.isLocalFile() for url in mime.urls()):
            self._on_file_status("Folders are not copied")
            return
        text = QGuiApplication.clipboard().text()
        if text and text != self._last_remote_clipboard:
            self._last_remote_clipboard = text
            self.client.send_clipboard(text)

    def closeEvent(self, event) -> None:
        if not self._finished:
            self._finished = True
            self._user_closed = True
            self.view.release_all_keys()
            try:
                QGuiApplication.clipboard().dataChanged.disconnect(self._on_local_clipboard)
            except TypeError:
                pass
            if self.client is not None:
                self.client.close()
            self.finished.emit(self)
        super().closeEvent(event)
