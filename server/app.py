"""PyVNC Server tray app.

On macOS it lives in the menu bar. On Windows it lives in the system tray.
Run from the project root:  python -m server.app   (add --background to start without the window)
"""
from __future__ import annotations

import sys

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSlider, QSpinBox,
    QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

import mss

from server import app_support as support
from server.controller import ControllerEvents, ServerController, ServerSettings

IS_WINDOWS = sys.platform == "win32"
APP_NAME = "PyVNC Server"
INSTANCE_KEY = "pyvnc-server-app"
_THIS_MACHINE = "This PC only (testing)" if IS_WINDOWS else "This Mac only (testing)"
LISTEN_CHOICES = [("All interfaces (LAN + internet)", "all"), ("Tailscale only", "tailscale"),
                  (_THIS_MACHINE, "local")]
SCALE_CHOICES = [("100% (sharpest)", 1.0), ("75%", 0.75), ("50% (fastest)", 0.5)]


# -- icons -------------------------------------------------------------------

def tray_icon(running: bool, clients: int) -> QIcon:
    """Monochrome template icon: a monitor, filled while serving, with a dot for viewers."""
    icon = QIcon()
    for size in (18, 36):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = size / 18
        pen = QPen(QColor("black"), 1.6 * s)
        p.setPen(pen)
        p.drawRoundedRect(int(1.5 * s), int(2.5 * s), int(15 * s), int(10 * s), 2 * s, 2 * s)
        p.drawLine(int(6 * s), int(15.5 * s), int(12 * s), int(15.5 * s))
        p.drawLine(int(9 * s), int(12.5 * s), int(9 * s), int(15.5 * s))
        if running:
            p.fillRect(int(4 * s), int(5 * s), int(10 * s), int(5 * s), QColor("black"))
        if clients:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("black"))
            p.drawEllipse(int(12 * s), 0, int(6 * s), int(6 * s))
        p.end()
        icon.addPixmap(pm)
    if not IS_WINDOWS:
        icon.setIsMask(True)
    return icon


# -- settings persistence ----------------------------------------------------

class Store:
    def __init__(self):
        self.q = QSettings("PyVNC", "Server")

    def load(self) -> tuple[ServerSettings, dict]:
        q = self.q
        quality = int(q.value("quality", 0))
        settings = ServerSettings(
            password=support.load_password(),
            port=int(q.value("port", 5900)),
            display=int(q.value("display", 1)),
            scale=float(q.value("scale", 1.0)),
            fps=float(q.value("fps", 20)),
            quality=quality or None,
            view_only=q.value("view_only", False, bool),
            swap_ctrl_cmd=q.value("swap_ctrl_cmd", False, bool),
            clipboard=q.value("clipboard", True, bool),
            keep_awake=q.value("keep_awake", True, bool),
            allow_insecure=q.value("allow_insecure", False, bool),
        )
        extra = {"listen": q.value("listen", "all", str),
                 "start_on_launch": q.value("start_on_launch", True, bool)}
        return settings, extra

    def save(self, s: ServerSettings, extra: dict) -> None:
        q = self.q
        support.save_password(s.password)
        for key, value in (("port", s.port), ("display", s.display), ("scale", s.scale),
                           ("fps", s.fps), ("quality", s.quality or 0), ("view_only", s.view_only),
                           ("swap_ctrl_cmd", s.swap_ctrl_cmd), ("clipboard", s.clipboard),
                           ("keep_awake", s.keep_awake), ("allow_insecure", s.allow_insecure),
                           ("listen", extra["listen"]), ("start_on_launch", extra["start_on_launch"])):
            q.setValue(key, value)
        q.sync()


def resolve_host(listen: str) -> str:
    if listen == "local":
        return "127.0.0.1"
    if listen == "tailscale":
        ip = support.tailscale_ip()
        if not ip:
            raise RuntimeError("Tailscale is not connected on this computer. Start Tailscale or choose "
                               "'All interfaces'.")
        return ip
    return "0.0.0.0"


# -- signals bridge ----------------------------------------------------------

class Signals(QObject):
    state = pyqtSignal(bool, object)
    client_connected = pyqtSignal(str)
    client_disconnected = pyqtSignal(str)
    auth_failed = pyqtSignal(str)
    log = pyqtSignal(str)

    def events(self) -> ControllerEvents:
        return ControllerEvents(
            on_state=self.state.emit,
            on_client_connected=self.client_connected.emit,
            on_client_disconnected=self.client_disconnected.emit,
            on_auth_failed=self.auth_failed.emit,
            on_log=self.log.emit,
        )


# -- settings window ---------------------------------------------------------

class SettingsWindow(QWidget):
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    apply_requested = pyqtSignal()
    kick_requested = pyqtSignal(str)
    regenerate_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_banner())
        tabs = QTabWidget()
        tabs.addTab(self._build_general(), "General")
        tabs.addTab(self._build_display(), "Display")
        tabs.addTab(self._build_status(), "Status")
        self.tabs = tabs
        layout.addWidget(tabs)

        bottom = QHBoxLayout()
        self.state_label = QLabel()
        self.apply_button = QPushButton("Save")
        self.apply_button.clicked.connect(self.apply_requested)
        self.start_button = QPushButton("Start Server")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._toggle)
        bottom.addWidget(self.state_label, 1)
        bottom.addWidget(self.apply_button)
        bottom.addWidget(self.start_button)
        layout.addLayout(bottom)

        self._running = False
        self._fingerprint = ""
        self.refresh_permissions()
        self._perm_timer = QTimer(self, interval=3000, timeout=self.refresh_permissions)
        self._perm_timer.start()

    # -- building ----------------------------------------------------------

    def _build_banner(self) -> QFrame:
        self.banner = QFrame()
        self.banner.setStyleSheet("QFrame { background: #fff4ce; border: 1px solid #e0c060; border-radius: 6px; }"
                                  "QLabel { border: none; color: #4a3b00; }")
        row = QVBoxLayout(self.banner)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        row.addWidget(self.banner_text)
        buttons = QHBoxLayout()
        self.screen_button = QPushButton("Open Screen Recording settings")
        self.screen_button.clicked.connect(self._fix_screen_recording)
        self.access_button = QPushButton("Open Accessibility settings")
        self.access_button.clicked.connect(lambda: support.open_privacy_settings("Accessibility"))
        buttons.addWidget(self.screen_button)
        buttons.addWidget(self.access_button)
        buttons.addStretch(1)
        row.addLayout(buttons)
        return self.banner

    def _build_general(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.listen = QComboBox()
        for label, value in LISTEN_CHOICES:
            self.listen.addItem(label, value)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("at least 8 characters")
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: (self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password), show.setText("Hide" if on else "Show")))
        generate = QPushButton("Generate")
        generate.clicked.connect(self._generate_password)
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.password, 1)
        pw_row.addWidget(show)
        pw_row.addWidget(generate)

        self.start_on_launch = QCheckBox("Start the server when the app launches")
        self.open_at_login = QCheckBox("Open PyVNC Server at login")
        self.allow_insecure = QCheckBox("Also allow unencrypted connections (old viewers, LAN only)")

        form.addRow("Port", self.port)
        form.addRow("Listen on", self.listen)
        form.addRow("Password", pw_row)
        stored = "Windows Credential Manager" if IS_WINDOWS else "your macOS Keychain"
        form.addRow(QLabel(f"The password is stored in {stored}."))
        form.addRow(self.start_on_launch)
        form.addRow(self.open_at_login)
        form.addRow(self.allow_insecure)
        return w

    def _build_display(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.display = QComboBox()
        with mss.MSS() as sct:
            for i, mon in enumerate(sct.monitors[1:], start=1):
                self.display.addItem(f"Display {i}: {mon['width']} x {mon['height']}", i)
        self.scale = QComboBox()
        for label, value in SCALE_CHOICES:
            self.scale.addItem(label, value)

        fps_row = QHBoxLayout()
        self.fps = QSlider(Qt.Orientation.Horizontal)
        self.fps.setRange(5, 60)
        self.fps_label = QLabel()
        self.fps.valueChanged.connect(lambda v: self.fps_label.setText(f"{v} fps"))
        fps_row.addWidget(self.fps, 1)
        fps_row.addWidget(self.fps_label)

        self.quality = QSpinBox()
        self.quality.setRange(0, 95)
        self.quality.setSpecialValueText("Let the viewer decide")
        self.view_only = QCheckBox("View only (ignore the viewer's mouse and keyboard)")
        self.swap_ctrl_cmd = QCheckBox("Treat the viewer's Ctrl as Command (for third-party viewers)")
        self.clipboard = QCheckBox("Share clipboard")
        awake = "Keep this PC awake while the server runs" if IS_WINDOWS else "Keep the Mac awake while the server runs"
        self.keep_awake = QCheckBox(awake)

        form.addRow("Display", self.display)
        form.addRow("Resolution", self.scale)
        form.addRow("Max frame rate", fps_row)
        form.addRow("JPEG quality", self.quality)
        boxes = [self.view_only, self.clipboard, self.keep_awake]
        if not IS_WINDOWS:
            boxes.insert(1, self.swap_ctrl_cmd)
        for box in boxes:
            form.addRow(box)
        return w

    def _build_status(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        heading = "Connect to this PC at" if IS_WINDOWS else "Connect to this Mac at"
        v.addWidget(QLabel(f"<b>{heading}</b>"))
        self.addresses = QLabel()
        self.addresses.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.addresses)

        v.addWidget(QLabel("<b>Certificate fingerprint</b> (the viewer shows this on first connect)"))
        self.fingerprint = QLabel()
        self.fingerprint.setWordWrap(True)
        self.fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.fingerprint.setStyleSheet("font-family: Menlo, monospace; font-size: 11px;")
        v.addWidget(self.fingerprint)
        fp_row = QHBoxLayout()
        copy = QPushButton("Copy fingerprint")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self._fingerprint))
        regen = QPushButton("Regenerate certificate...")
        regen.clicked.connect(self.regenerate_requested)
        fp_row.addWidget(copy)
        fp_row.addWidget(regen)
        fp_row.addStretch(1)
        v.addLayout(fp_row)

        v.addWidget(QLabel("<b>Connected viewers</b>"))
        self.clients = QListWidget()
        self.clients.setMaximumHeight(90)
        v.addWidget(self.clients)
        kick = QPushButton("Disconnect selected")
        kick.clicked.connect(self._kick_selected)
        v.addWidget(kick, alignment=Qt.AlignmentFlag.AlignLeft)

        v.addWidget(QLabel("<b>Log</b>"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setStyleSheet("font-family: Menlo, monospace; font-size: 11px;")
        v.addWidget(self.log, 1)
        return w

    # -- form <-> settings -------------------------------------------------

    def set_values(self, s: ServerSettings, extra: dict) -> None:
        self.port.setValue(s.port)
        self.listen.setCurrentIndex(max(0, self.listen.findData(extra["listen"])))
        self.password.setText(s.password)
        self.start_on_launch.setChecked(extra["start_on_launch"])
        self.open_at_login.setChecked(support.is_open_at_login())
        self.allow_insecure.setChecked(s.allow_insecure)
        self.display.setCurrentIndex(max(0, self.display.findData(s.display)))
        self.scale.setCurrentIndex(max(0, self.scale.findData(s.scale)))
        self.fps.setValue(int(s.fps))
        self.fps_label.setText(f"{int(s.fps)} fps")
        self.quality.setValue(s.quality or 0)
        self.view_only.setChecked(s.view_only)
        self.swap_ctrl_cmd.setChecked(s.swap_ctrl_cmd)
        self.clipboard.setChecked(s.clipboard)
        self.keep_awake.setChecked(s.keep_awake)

    def values(self) -> tuple[ServerSettings, dict]:
        s = ServerSettings(
            password=self.password.text(),
            port=self.port.value(),
            display=self.display.currentData() or 1,
            scale=self.scale.currentData(),
            fps=self.fps.value(),
            quality=self.quality.value() or None,
            view_only=self.view_only.isChecked(),
            swap_ctrl_cmd=self.swap_ctrl_cmd.isChecked(),
            clipboard=self.clipboard.isChecked(),
            keep_awake=self.keep_awake.isChecked(),
            allow_insecure=self.allow_insecure.isChecked(),
        )
        extra = {"listen": self.listen.currentData(), "start_on_launch": self.start_on_launch.isChecked(),
                 "open_at_login": self.open_at_login.isChecked()}
        return s, extra

    # -- status ------------------------------------------------------------

    def set_fingerprint(self, fingerprint: str) -> None:
        self._fingerprint = fingerprint
        half = len(fingerprint) // 2 + 1
        self.fingerprint.setText(fingerprint[:half] + "\n" + fingerprint[half:])

    def set_running(self, running: bool, port: int | None = None, clients: int = 0) -> None:
        self._running = running
        self.start_button.setText("Stop Server" if running else "Start Server")
        self.apply_button.setText("Save && Restart" if running else "Save")
        if running:
            viewers = f", {clients} viewer{'s' if clients != 1 else ''} connected" if clients else ""
            self.state_label.setText(f"Running on port {port}{viewers}")
        else:
            self.state_label.setText("Stopped")

    def set_addresses(self, port: int) -> None:
        lines = []
        for iface, ip in support.local_addresses():
            kind = "Tailscale" if support.is_tailscale(ip) else f"LAN ({iface})"
            lines.append(f"{ip} : {port}    <span style='color:gray'>{kind}</span>")
        if not any(support.is_tailscale(ip) for _i, ip in support.local_addresses()):
            where = "both PCs" if IS_WINDOWS else "this Mac and the PC"
            lines.append("<span style='color:gray'>For internet access, install Tailscale on "
                         f"{where}.</span>")
        self.addresses.setText("<br>".join(lines) or "No network connection")

    def set_clients(self, peers: list[str]) -> None:
        self.clients.clear()
        self.clients.addItems(peers)

    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def refresh_permissions(self) -> None:
        if IS_WINDOWS:
            self.banner.setVisible(False)
            return
        from server.input_mac import accessibility_trusted, screen_recording_allowed
        screen_ok = screen_recording_allowed()
        access_ok = accessibility_trusted()
        problems = []
        if not screen_ok:
            problems.append("<b>Screen Recording</b> is off, so viewers will only see the wallpaper.")
        if not access_ok:
            problems.append("<b>Accessibility</b> is off, so viewers cannot use the mouse or keyboard.")
        self.screen_button.setVisible(not screen_ok)
        self.access_button.setVisible(not access_ok)
        self.banner.setVisible(bool(problems))
        if problems:
            self.banner_text.setText(" ".join(problems) + " Enable PyVNC Server in System Settings > "
                                     "Privacy & Security, then quit and reopen the app.")

    # -- actions -----------------------------------------------------------

    def _toggle(self) -> None:
        (self.stop_requested if self._running else self.start_requested).emit()

    def _generate_password(self) -> None:
        self.password.setText(support.generate_password())
        self.password.setEchoMode(QLineEdit.EchoMode.Normal)

    def _fix_screen_recording(self) -> None:
        from server.input_mac import screen_recording_allowed
        screen_recording_allowed(request=True)
        support.open_privacy_settings("ScreenCapture")

    def _kick_selected(self) -> None:
        item = self.clients.currentItem()
        if item is not None:
            self.kick_requested.emit(item.text())


# -- tray app ----------------------------------------------------------------

class ServerApp(QObject):
    def __init__(self, qt: QApplication, background: bool):
        super().__init__()
        self.qt = qt
        self.store = Store()
        self.signals = Signals()
        self.controller = ServerController(self.signals.events())
        self.window = SettingsWindow()
        settings, extra = self.store.load()
        self.window.set_values(settings, extra)
        self.window.set_fingerprint(self.controller.fingerprint())
        self.window.set_addresses(settings.port)

        self.tray = QSystemTrayIcon(tray_icon(False, 0))
        self.tray.setToolTip(APP_NAME)
        self._build_menu()
        self.tray.show()

        s = self.signals
        s.state.connect(self._on_state)
        s.client_connected.connect(self._on_client_connected)
        s.client_disconnected.connect(lambda peer: self._refresh())
        s.auth_failed.connect(lambda peer: self.tray.showMessage(
            APP_NAME, f"Wrong password from {peer.rsplit(':', 1)[0]}", QSystemTrayIcon.MessageIcon.Warning))
        s.log.connect(self.window.append_log)

        w = self.window
        w.start_requested.connect(self.start)
        w.stop_requested.connect(self.stop)
        w.apply_requested.connect(self.apply)
        w.kick_requested.connect(self.controller.kick)
        w.regenerate_requested.connect(self.regenerate_certificate)

        self._refresh()
        if extra["start_on_launch"] and settings.password:
            QTimer.singleShot(0, lambda: self.start(show_errors=not background))
        if not background or not settings.password:
            QTimer.singleShot(0, self.show_window)

    def _build_menu(self) -> None:
        menu = QMenu()
        self.status_action = QAction("Stopped", menu, enabled=False)
        self.toggle_action = QAction("Start Server", menu, triggered=self._toggle)
        settings_action = QAction("Settings...", menu, triggered=self.show_window)
        copy_action = QAction("Copy Fingerprint", menu, triggered=lambda: QGuiApplication.clipboard().setText(
            self.controller.fingerprint()))
        quit_action = QAction("Quit PyVNC Server", menu, triggered=self.quit)
        for a in (self.status_action, None, self.toggle_action, settings_action, copy_action, None, quit_action):
            menu.addSeparator() if a is None else menu.addAction(a)
        self.menu = menu
        self.tray.setContextMenu(menu)

    # -- actions -----------------------------------------------------------

    def show_window(self) -> None:
        self.window.refresh_permissions()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        support.bring_to_front()

    def _save_form(self) -> ServerSettings | None:
        settings, extra = self.window.values()
        if len(settings.password) < 8:
            self._error("Set a password of at least 8 characters on the General tab.")
            self.window.tabs.setCurrentIndex(0)
            self.show_window()
            return None
        try:
            self.store.save(settings, extra)
            support.set_open_at_login(extra["open_at_login"])
            settings.host = resolve_host(extra["listen"])
        except Exception as exc:
            self._error(str(exc))
            return None
        self.window.set_addresses(settings.port)
        return settings

    def start(self, show_errors: bool = True) -> None:
        settings = self._save_form()
        if settings is None:
            return
        try:
            self.controller.start(settings)
        except RuntimeError as exc:
            if show_errors:
                self._error(f"Could not start the server:\n\n{exc}")
            else:
                self.tray.showMessage(APP_NAME, f"Could not start: {exc}", QSystemTrayIcon.MessageIcon.Critical)
        self._refresh()

    def stop(self) -> None:
        self.controller.stop()
        self._refresh()

    def apply(self) -> None:
        if self.controller.running:
            settings = self._save_form()
            if settings is None:
                return
            try:
                self.controller.restart(settings)
            except RuntimeError as exc:
                self._error(f"Could not restart the server:\n\n{exc}")
        else:
            self._save_form()
        self._refresh()

    def _toggle(self) -> None:
        self.stop() if self.controller.running else self.start()

    def regenerate_certificate(self) -> None:
        answer = QMessageBox.question(
            self.window, APP_NAME,
            "Create a new TLS certificate? Viewers that connected before will warn that the "
            "certificate changed, and you'll need to confirm the new fingerprint.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        was_running = self.controller.running
        self.controller.stop()
        self.window.set_fingerprint(self.controller.regenerate_certificate())
        if was_running:
            self.start()
        self._refresh()

    def quit(self) -> None:
        self.controller.stop()
        self.tray.hide()
        self.qt.quit()

    def _error(self, message: str) -> None:
        self.show_window()
        QMessageBox.critical(self.window, APP_NAME, message)

    # -- controller events -------------------------------------------------

    def _on_state(self, running: bool, error) -> None:
        if not running and error:
            self.tray.showMessage(APP_NAME, f"Server stopped: {error}", QSystemTrayIcon.MessageIcon.Critical)
        self._refresh()

    def _on_client_connected(self, peer: str) -> None:
        self.tray.showMessage(APP_NAME, f"Viewer connected from {peer.rsplit(':', 1)[0]}")
        self._refresh()

    def _refresh(self) -> None:
        running = self.controller.running
        clients = self.controller.clients() if running else []
        port = self.controller.settings.port if (running and self.controller.settings) else None
        self.tray.setIcon(tray_icon(running, len(clients)))
        if running:
            viewers = f", {len(clients)} viewer{'s' if len(clients) != 1 else ''}" if clients else ""
            status = f"Running on port {port}{viewers}"
        else:
            status = "Stopped"
        self.tray.setToolTip(f"{APP_NAME}: {status}")
        self.status_action.setText(status)
        self.toggle_action.setText("Stop Server" if running else "Start Server")
        self.window.set_running(running, port, len(clients))
        self.window.set_clients(clients)


def _already_running() -> bool:
    """If another instance is running, ask it to show its window."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main() -> None:
    background = "--background" in sys.argv
    qt = QApplication(sys.argv)
    qt.setApplicationName(APP_NAME)
    qt.setQuitOnLastWindowClosed(False)
    if _already_running():
        return
    support.hide_dock_icon()

    instance = QLocalServer()
    QLocalServer.removeServer(INSTANCE_KEY)
    instance.listen(INSTANCE_KEY)
    app = ServerApp(qt, background)

    def on_instance():
        conn = instance.nextPendingConnection()
        if conn is not None:
            conn.close()
        app.show_window()

    instance.newConnection.connect(on_instance)
    sys.exit(qt.exec())


if __name__ == "__main__":
    main()
