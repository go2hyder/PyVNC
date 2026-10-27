"""File clipboard framing: safe names and a loopback transfer. Run: python -m tests.file_transfer_test"""
from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from client.connection import Callbacks, ConnectOptions, RFBClient
from common import rfb
from common.security import ensure_server_certificate, server_tls_context
from common.transfer import IncomingFile, safe_name
from server.session import AuthGuard, ServerConfig, Session

PASSWORD = "smoke-test-password"


def check_names() -> None:
    assert safe_name(r"..\..\evil.txt") == "evil.txt"
    assert safe_name("../etc/passwd") == "passwd"
    assert safe_name("..") == "file"
    assert ":" not in safe_name("a:b")
    folder = Path(tempfile.mkdtemp())
    incoming = IncomingFile(folder)
    assert incoming.begin("../../secret.txt", 4) is None
    assert incoming.add(b"data") is None
    path = incoming.end()
    assert path is not None and path.parent == folder and path.name == "secret.txt"
    assert path.read_bytes() == b"data"
    assert incoming.begin("note.txt", 1) is None
    incoming.add(b"a")
    incoming.end()
    assert incoming.begin("note.txt", 1) is None
    incoming.add(b"b")
    second = incoming.end()
    assert second is not None and second.name == "note (2).txt"
    assert incoming.begin("big", rfb.FILE_MAX_BYTES + 1)
    print("  names ok")


class FakeCapture:
    monitor = {"left": 0, "top": 0, "width": 32, "height": 24}

    def grab(self) -> np.ndarray:
        frame = np.zeros((24, 32, 4), dtype=np.uint8)
        frame[..., 3] = 255
        return frame


class FakeInjector:
    def key(self, *args):
        pass

    def pointer(self, *args):
        pass

    def release_all(self):
        pass


class FakeClipboard:
    def __init__(self, outgoing: list[str] | None = None):
        self.outgoing = outgoing
        self.files: list[list[str]] = []
        self._sent = False

    def poll(self):
        if self.outgoing and not self._sent:
            self._sent = True
            return list(self.outgoing)
        return None

    def set(self, text: str) -> None:
        pass

    def set_files(self, paths: list[str]) -> None:
        self.files.append(list(paths))


def serve(stop: threading.Event, port_holder: list[int], clipboard, receive: Path,
          view_only: bool = False) -> None:
    async def main() -> None:
        cert_dir = Path(tempfile.mkdtemp())
        cert, key = ensure_server_certificate(cert_dir)
        config = ServerConfig(
            password=PASSWORD,
            ssl_context=server_tls_context(cert, key),
            allow_insecure=False,
            fps=5,
            forced_quality=None,
            view_only=view_only,
            name="loopback",
            file_dir=receive,
        )
        executor = ThreadPoolExecutor(max_workers=1)

        async def on_client(reader, writer):
            await Session(reader, writer, config, FakeCapture(), executor, FakeInjector(),
                          clipboard, AuthGuard()).run()

        server = await asyncio.start_server(on_client, "127.0.0.1", 0)
        port_holder.append(server.sockets[0].getsockname()[1])
        while not stop.is_set():
            await asyncio.sleep(0.05)
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def connect(port: int, file_dir: Path, result: dict):
    got = threading.Event()

    def on_update(*_args):
        got.set()

    def on_files(paths):
        result["files"] = list(paths)
        result["got_files"].set()

    def on_status(text):
        result["status"] = text

    cb = Callbacks(
        on_update=on_update,
        on_files=on_files,
        on_file_status=on_status,
        confirm_certificate=lambda *_a: True,
        on_closed=lambda error: result.update(error=error),
    )
    client = RFBClient(ConnectOptions(
        "127.0.0.1", port, PASSWORD, quality_level=5, file_dir=file_dir,
    ), cb)
    client.start()
    return client, got


def check_transfer() -> None:
    source = Path(tempfile.mkdtemp())
    mac_dir = Path(tempfile.mkdtemp())
    win_dir = Path(tempfile.mkdtemp())
    from_mac = source / "from-mac.txt"
    from_mac.write_bytes(b"hello from mac")
    one = source / "one.txt"
    two = source / "two.txt"
    one.write_bytes(b"alpha")
    two.write_text("beta \u2603", encoding="utf-8")

    clipboard = FakeClipboard([str(from_mac)])
    stop = threading.Event()
    port_holder: list[int] = []
    thread = threading.Thread(target=serve, args=(stop, port_holder, clipboard, mac_dir), daemon=True)
    thread.start()
    for _ in range(50):
        if port_holder:
            break
        time.sleep(0.05)
    assert port_holder, "server did not start"
    result: dict = {"got_files": threading.Event()}
    client, got = connect(port_holder[0], win_dir, result)
    try:
        assert got.wait(10), f"no picture: {result}"
        assert result["got_files"].wait(10), f"mac file did not arrive: {result}"
        received = Path(result["files"][-1])
        assert received.parent == win_dir
        assert received.read_bytes() == b"hello from mac"

        result["got_files"].clear()
        client.send_files([str(one), str(two), str(source)])
        deadline = time.time() + 10
        while time.time() < deadline and (not clipboard.files or len(clipboard.files[-1]) < 2):
            time.sleep(0.05)
        assert clipboard.files and len(clipboard.files[-1]) == 2, clipboard.files
        saved = [Path(path) for path in clipboard.files[-1]]
        assert {path.name for path in saved} == {"one.txt", "two.txt"}
        assert all(path.parent == mac_dir for path in saved)
        by_name = {path.name: path.read_bytes() for path in saved}
        assert by_name["one.txt"] == b"alpha"
        assert by_name["two.txt"] == "beta \u2603".encode()
        assert not any(path.is_dir() and path.name == source.name for path in mac_dir.iterdir())
        print("  transfer ok", result.get("status"))
    finally:
        client.close()
        stop.set()
        thread.join(5)


def check_view_only() -> None:
    source = Path(tempfile.mkdtemp())
    payload = source / "nope.txt"
    payload.write_bytes(b"secret")
    mac_dir = Path(tempfile.mkdtemp())
    clipboard = FakeClipboard()
    stop = threading.Event()
    port_holder: list[int] = []
    thread = threading.Thread(
        target=serve, args=(stop, port_holder, clipboard, mac_dir, True), daemon=True)
    thread.start()
    for _ in range(50):
        if port_holder:
            break
        time.sleep(0.05)
    result: dict = {"got_files": threading.Event()}
    client, got = connect(port_holder[0], Path(tempfile.mkdtemp()), result)
    try:
        assert got.wait(10), result
        client.send_files([str(payload)])
        deadline = time.time() + 5
        while time.time() < deadline and "disabled" not in (result.get("status") or ""):
            time.sleep(0.05)
        assert "disabled" in (result.get("status") or ""), result
        assert clipboard.files == []
        assert list(mac_dir.iterdir()) == []
        print("  view-only blocked")
    finally:
        client.close()
        stop.set()
        thread.join(5)


if __name__ == "__main__":
    check_names()
    check_transfer()
    check_view_only()
    print("file transfer ok")
