"""Saved connections (JSON, no secrets), passwords (OS credential store) and preferences."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from PyQt6.QtCore import QSettings

from common.security import config_dir

KEYRING_SERVICE = "PyVNC Viewer"


@dataclass
class Connection:
    name: str = ""
    host: str = ""
    port: int = 5900
    username: str = "pyvnc"
    quality: str = "Medium"
    ctrl_as_cmd: bool = True
    share_clipboard: bool = True
    allow_insecure: bool = False
    remember_password: bool = False
    last_connected: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def address(self) -> str:
        return self.host if self.port == 5900 else f"{self.host}:{self.port}"

    @property
    def display_name(self) -> str:
        return self.name or self.host

    def last_connected_text(self) -> str:
        if not self.last_connected:
            return "Never"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_connected))

    @classmethod
    def from_dict(cls, data: dict) -> "Connection":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def parse_address(text: str, default_port: int = 5900) -> tuple[str, int]:
    """Accept 'host', 'host:port', '[v6]:port' or VNC-style 'host::port'."""
    text = text.strip()
    if "::" in text and not text.startswith("["):
        host, _, port = text.partition("::")
        return host, int(port)
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host, int(port)
    return text, default_port


class ConnectionStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "connections.json")
        self.items: list[Connection] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.items = [Connection.from_dict(d) for d in data.get("connections", [])]
        except (OSError, ValueError):
            self.items = []

    def save(self) -> None:
        data = {"connections": [asdict(c) for c in self.items]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def get(self, conn_id: str) -> Connection | None:
        return next((c for c in self.items if c.id == conn_id), None)

    def find(self, host: str, port: int) -> Connection | None:
        return next((c for c in self.items if c.host == host and c.port == port), None)

    def upsert(self, conn: Connection) -> None:
        for i, c in enumerate(self.items):
            if c.id == conn.id:
                self.items[i] = conn
                break
        else:
            self.items.append(conn)
        self.save()

    def delete(self, conn_id: str) -> None:
        self.items = [c for c in self.items if c.id != conn_id]
        forget_password(conn_id)
        self.save()

    def mark_connected(self, conn_id: str) -> None:
        conn = self.get(conn_id)
        if conn is not None:
            conn.last_connected = time.time()
            self.save()


# -- passwords (Windows Credential Manager / macOS Keychain) -----------------

def load_password(conn_id: str) -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, conn_id) or ""
    except Exception:
        return ""


def save_password(conn_id: str, password: str) -> bool:
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, conn_id, password)
        return True
    except Exception:
        return False


def forget_password(conn_id: str) -> None:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, conn_id)
    except Exception:
        pass


# -- preferences -------------------------------------------------------------

class Preferences:
    def __init__(self):
        self.q = QSettings("PyVNC", "Viewer")

    @property
    def default_quality(self) -> str:
        return self.q.value("default_quality", "Medium", str)

    @default_quality.setter
    def default_quality(self, value: str) -> None:
        self.q.setValue("default_quality", value)

    @property
    def theme(self) -> str:
        return self.q.value("theme", "system", str)

    @theme.setter
    def theme(self, value: str) -> None:
        self.q.setValue("theme", value)

    @property
    def default_ctrl_as_cmd(self) -> bool:
        return self.q.value("default_ctrl_as_cmd", True, bool)

    @default_ctrl_as_cmd.setter
    def default_ctrl_as_cmd(self, value: bool) -> None:
        self.q.setValue("default_ctrl_as_cmd", value)

    def new_connection(self, host: str = "", port: int = 5900) -> Connection:
        return Connection(host=host, port=port, quality=self.default_quality,
                          ctrl_as_cmd=self.default_ctrl_as_cmd)
