"""Launcher window: saved connections, quick connect, preferences."""
from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QKeySequence, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSpinBox, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from client import store
from client.connection import QUALITY_PRESETS
from client.store import Connection, ConnectionStore, Preferences, parse_address
from client.viewer import ViewerWindow
from common.security import KnownHosts

THEMES = [("Follow system", "system"), ("Light", "light"), ("Dark", "dark")]


# -- theme -------------------------------------------------------------------

_DEFAULT_STYLE: str | None = None


def apply_theme(app: QApplication, theme: str) -> None:
    global _DEFAULT_STYLE
    if _DEFAULT_STYLE is None:
        _DEFAULT_STYLE = app.style().name()
    if theme == "system":
        app.setStyle(_DEFAULT_STYLE)
        app.setPalette(app.style().standardPalette())
        return
    app.setStyle("Fusion")
    if theme == "light":
        app.setPalette(app.style().standardPalette())
        return
    p = QPalette()
    dark, base, text = QColor(45, 45, 48), QColor(30, 30, 30), QColor(230, 230, 230)
    for role, color in ((QPalette.ColorRole.Window, dark), (QPalette.ColorRole.WindowText, text),
                        (QPalette.ColorRole.Base, base), (QPalette.ColorRole.AlternateBase, dark),
                        (QPalette.ColorRole.ToolTipBase, dark), (QPalette.ColorRole.ToolTipText, text),
                        (QPalette.ColorRole.Text, text), (QPalette.ColorRole.Button, dark),
                        (QPalette.ColorRole.ButtonText, text), (QPalette.ColorRole.BrightText, QColor("red")),
                        (QPalette.ColorRole.Highlight, QColor(42, 130, 218)),
                        (QPalette.ColorRole.HighlightedText, QColor("white")),
                        (QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140))):
        p.setColor(role, color)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(120, 120, 120))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(120, 120, 120))
    app.setPalette(p)


# -- dialogs -----------------------------------------------------------------

class ConnectionEditor(QDialog):
    def __init__(self, conn: Connection, password: str, parent=None, title: str = "Edit connection"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.conn = conn

        self.name = QLineEdit(conn.name)
        self.name.setPlaceholderText("e.g. Office Mac")
        self.host = QLineEdit(conn.host)
        self.host.setPlaceholderText("IP address, Tailscale name or hostname")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(conn.port)
        self.username = QLineEdit(conn.username)
        self.password = QLineEdit(password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("asked when connecting if empty")
        self.remember = QCheckBox("Remember password (Windows Credential Manager)")
        self.remember.setChecked(conn.remember_password)
        self.quality = QComboBox()
        self.quality.addItems(list(QUALITY_PRESETS))
        self.quality.setCurrentText(conn.quality)
        self.ctrl_as_cmd = QCheckBox("Use Ctrl as Mac Command key (Win key becomes Control)")
        self.ctrl_as_cmd.setChecked(conn.ctrl_as_cmd)
        self.share_clipboard = QCheckBox("Share clipboard")
        self.share_clipboard.setChecked(conn.share_clipboard)
        self.allow_insecure = QCheckBox("Allow unencrypted connection (trusted LAN only)")
        self.allow_insecure.setChecked(conn.allow_insecure)

        self.forget_cert = QPushButton("Forget trusted certificate")
        self.forget_cert.clicked.connect(self._forget_certificate)
        self.forget_cert.setEnabled(bool(conn.host) and KnownHosts().get(conn.host, conn.port) is not None)

        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Host", self.host)
        form.addRow("Port", self.port)
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        form.addRow("", self.remember)
        form.addRow("Quality", self.quality)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        for w in (self.ctrl_as_cmd, self.share_clipboard, self.allow_insecure):
            layout.addWidget(w)
        layout.addWidget(self.forget_cert, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(buttons)

    def _forget_certificate(self) -> None:
        KnownHosts().remove(self.conn.host, self.conn.port)
        self.forget_cert.setEnabled(False)
        QMessageBox.information(self, "PyVNC", "The certificate will be checked again on the next connect.")

    def _accept(self) -> None:
        host = self.host.text().strip()
        if not host:
            QMessageBox.warning(self, "PyVNC", "Enter the Mac's address.")
            return
        if ":" in host and not host.startswith("["):
            try:
                host, port = parse_address(host, self.port.value())
                self.port.setValue(port)
            except ValueError:
                QMessageBox.warning(self, "PyVNC", "The port in the address is not a number.")
                return
        self.host.setText(host)
        self.accept()

    def result_connection(self) -> tuple[Connection, str]:
        conn = replace(
            self.conn,
            name=self.name.text().strip(),
            host=self.host.text().strip(),
            port=self.port.value(),
            username=self.username.text().strip() or "pyvnc",
            quality=self.quality.currentText(),
            ctrl_as_cmd=self.ctrl_as_cmd.isChecked(),
            share_clipboard=self.share_clipboard.isChecked(),
            allow_insecure=self.allow_insecure.isChecked(),
            remember_password=self.remember.isChecked(),
        )
        return conn, self.password.text()


class PasswordDialog(QDialog):
    def __init__(self, conn: Connection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Password")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember = QCheckBox("Remember password")
        self.remember.setChecked(conn.remember_password)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Password for <b>{conn.display_name}</b> ({conn.host}:{conn.port})"))
        layout.addWidget(self.password)
        layout.addWidget(self.remember)
        layout.addWidget(buttons)


class PreferencesDialog(QDialog):
    def __init__(self, prefs: Preferences, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.prefs = prefs
        self.quality = QComboBox()
        self.quality.addItems(list(QUALITY_PRESETS))
        self.quality.setCurrentText(prefs.default_quality)
        self.theme = QComboBox()
        for label, value in THEMES:
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(prefs.theme)))
        self.ctrl_as_cmd = QCheckBox("New connections use Ctrl as Command")
        self.ctrl_as_cmd.setChecked(prefs.default_ctrl_as_cmd)
        verify = QCheckBox("Verify the server certificate fingerprint on first connect")
        verify.setChecked(True)
        verify.setEnabled(False)
        verify.setToolTip("Always on: this is what protects the connection from interception.")

        form = QFormLayout()
        form.addRow("Default quality", self.quality)
        form.addRow("Theme", self.theme)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.ctrl_as_cmd)
        layout.addWidget(verify)
        layout.addWidget(buttons)

    def save(self) -> None:
        self.prefs.default_quality = self.quality.currentText()
        self.prefs.theme = self.theme.currentData()
        self.prefs.default_ctrl_as_cmd = self.ctrl_as_cmd.isChecked()


# -- launcher ----------------------------------------------------------------

class LauncherWindow(QMainWindow):
    def __init__(self, app: QApplication, icon: QIcon | None = None):
        super().__init__()
        self.app = app
        self.icon = icon
        self.store = ConnectionStore()
        self.prefs = Preferences()
        self.viewers: list[ViewerWindow] = []
        self.setWindowTitle("PyVNC Viewer")
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(640, 420)

        self._build_actions()
        central = QWidget()
        v = QVBoxLayout(central)

        quick = QHBoxLayout()
        self.quick = QLineEdit()
        self.quick.setPlaceholderText("Quick connect: Mac address or address:port")
        self.quick.returnPressed.connect(self.quick_connect)
        go = QPushButton("Connect")
        go.clicked.connect(self.quick_connect)
        quick.addWidget(self.quick, 1)
        quick.addWidget(go)
        v.addLayout(quick)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Address", "Last connected"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self.connect_selected())
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemSelectionChanged.connect(self._update_actions)

        self.empty = QLabel("No saved connections yet.<br><br>Click <b>New</b> to add your Mac, "
                            "or type its address above.")
        self.empty.setTextFormat(Qt.TextFormat.RichText)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color: gray;")
        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.empty)
        v.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.refresh()

    # -- UI ----------------------------------------------------------------

    def _build_actions(self) -> None:
        tb = self.addToolBar("Connections")
        tb.setMovable(False)
        self.new_action = QAction("New", self, shortcut=QKeySequence.StandardKey.New, triggered=self.new_connection)
        self.connect_action = QAction("Connect", self, triggered=self.connect_selected)
        self.edit_action = QAction("Edit", self, triggered=self.edit_selected)
        self.duplicate_action = QAction("Duplicate", self, triggered=self.duplicate_selected)
        self.delete_action = QAction("Delete", self, shortcut=QKeySequence.StandardKey.Delete,
                                     triggered=self.delete_selected)
        prefs_action = QAction("Preferences", self, triggered=self.show_preferences)
        for a in (self.new_action, self.connect_action, self.edit_action, self.duplicate_action,
                  self.delete_action):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(prefs_action)

    def _context_menu(self, pos) -> None:
        if self.selected() is None:
            return
        menu = QMenu(self)
        for a in (self.connect_action, self.edit_action, self.duplicate_action, self.delete_action):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction("Forget trusted certificate", self.forget_certificate)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def refresh(self, select_id: str | None = None) -> None:
        current = select_id or (self.selected().id if self.selected() else None)
        self.tree.clear()
        items = sorted(self.store.items, key=lambda c: (-c.last_connected, c.display_name.lower()))
        for conn in items:
            item = QTreeWidgetItem([conn.display_name, conn.address, conn.last_connected_text()])
            item.setData(0, Qt.ItemDataRole.UserRole, conn.id)
            self.tree.addTopLevelItem(item)
            if conn.id == current:
                self.tree.setCurrentItem(item)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.stack.setCurrentWidget(self.tree if items else self.empty)
        self._update_actions()

    def _update_actions(self) -> None:
        has = self.selected() is not None
        for a in (self.connect_action, self.edit_action, self.duplicate_action, self.delete_action):
            a.setEnabled(has)

    def selected(self) -> Connection | None:
        item = self.tree.currentItem()
        return self.store.get(item.data(0, Qt.ItemDataRole.UserRole)) if item else None

    # -- connection management --------------------------------------------

    def new_connection(self) -> None:
        self._edit(self.prefs.new_connection(), "", "New connection")

    def edit_selected(self) -> None:
        conn = self.selected()
        if conn is not None:
            password = store.load_password(conn.id) if conn.remember_password else ""
            self._edit(conn, password, "Edit connection")

    def duplicate_selected(self) -> None:
        conn = self.selected()
        if conn is not None:
            copy = replace(conn, name=f"{conn.display_name} (copy)", last_connected=0.0,
                           remember_password=False, id=Connection().id)
            self._edit(copy, "", "Duplicate connection")

    def _edit(self, conn: Connection, password: str, title: str) -> None:
        dialog = ConnectionEditor(conn, password, self, title)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        conn, password = dialog.result_connection()
        self._store_password(conn, password)
        self.store.upsert(conn)
        self.refresh(conn.id)

    def _store_password(self, conn: Connection, password: str) -> None:
        if conn.remember_password and password:
            if not store.save_password(conn.id, password):
                conn.remember_password = False
                QMessageBox.warning(self, "PyVNC", "Could not save the password in the credential store.")
        elif not conn.remember_password:
            store.forget_password(conn.id)

    def delete_selected(self) -> None:
        conn = self.selected()
        if conn is None:
            return
        answer = QMessageBox.question(self, "Delete connection", f"Delete {conn.display_name}?")
        if answer == QMessageBox.StandardButton.Yes:
            self.store.delete(conn.id)
            self.refresh()

    def forget_certificate(self) -> None:
        conn = self.selected()
        if conn is not None:
            KnownHosts().remove(conn.host, conn.port)
            QMessageBox.information(self, "PyVNC", f"The certificate of {conn.display_name} will be "
                                    "checked again on the next connect.")

    def show_preferences(self) -> None:
        dialog = PreferencesDialog(self.prefs, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.save()
            apply_theme(self.app, self.prefs.theme)

    # -- connecting --------------------------------------------------------

    def quick_connect(self) -> None:
        text = self.quick.text().strip()
        if not text:
            return
        try:
            host, port = parse_address(text)
        except ValueError:
            QMessageBox.warning(self, "PyVNC", "Use the form address or address:port.")
            return
        conn = self.store.find(host, port)
        if conn is None:
            conn = self.prefs.new_connection(host, port)
            self.store.upsert(conn)
            self.refresh(conn.id)
        self.quick.clear()
        self.open_viewer(conn)

    def connect_selected(self) -> None:
        conn = self.selected()
        if conn is not None:
            self.open_viewer(conn)

    def open_viewer(self, conn: Connection) -> None:
        password = store.load_password(conn.id) if conn.remember_password else ""
        if not password:
            dialog = PasswordDialog(conn, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            password = dialog.password.text()
            if dialog.remember.isChecked() != conn.remember_password:
                conn.remember_password = dialog.remember.isChecked()
                self.store.upsert(conn)
            if conn.remember_password:
                self._store_password(conn, password)

        viewer = ViewerWindow(conn, password, self.icon)
        viewer.connected_ok.connect(self._on_viewer_connected)
        viewer.finished.connect(self._on_viewer_finished)
        self.viewers.append(viewer)
        viewer.show()

    def _on_viewer_connected(self, conn_id: str) -> None:
        self.store.mark_connected(conn_id)
        self.refresh()

    def _on_viewer_finished(self, viewer: ViewerWindow) -> None:
        if viewer in self.viewers:
            self.viewers.remove(viewer)

    def closeEvent(self, event) -> None:
        if self.viewers:
            answer = QMessageBox.question(
                self, "PyVNC Viewer", f"Close {len(self.viewers)} open connection(s) and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        for viewer in list(self.viewers):
            viewer.close()
        super().closeEvent(event)
        self.app.quit()
