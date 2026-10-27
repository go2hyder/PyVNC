"""End-to-end loopback test: start the server on macOS and connect the headless client.

    python -m tests.smoke_test
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from client.connection import Callbacks, ConnectOptions, RFBClient

PORT = 5999
PASSWORD = "smoke-test-password"


def run_client(quality_level, password=PASSWORD, expect_error=False, port=PORT) -> None:
    got_update = threading.Event()
    closed = threading.Event()
    result = {}

    def on_update(x, y, w, h):
        result.setdefault("updates", 0)
        result["updates"] += 1
        got_update.set()

    def on_closed(error):
        result["error"] = error
        closed.set()

    cb = Callbacks(
        on_connected=lambda name, fb: result.update(name=name, size=fb.shape),
        on_update=on_update,
        on_closed=on_closed,
        on_security=lambda s: result.update(security=s),
        confirm_certificate=lambda hp, fp, old: result.update(fingerprint=fp) or True,
    )
    client = RFBClient(ConnectOptions("127.0.0.1", port, password, quality_level=quality_level), cb)
    client.start()

    if expect_error:
        assert closed.wait(15), "client did not close"
        assert result.get("error") and "Authentication failed" in result["error"], result
        print("  wrong password rejected:", result["error"])
        return

    assert got_update.wait(15), f"no framebuffer update: {result}"
    client.send_pointer(0, 10, 10)
    time.sleep(1.0)
    fb = client.fb
    client.close()
    closed.wait(5)
    print(f"  {result.get('name')} {result.get('size')} security={result.get('security')!r} "
          f"updates={result.get('updates')} mean_pixel={fb[..., :3].mean():.1f}")


def run_controller() -> None:
    """Drive ServerController in-process: start, connect, kick, stop, restart."""
    from server.controller import ControllerEvents, ServerController, ServerSettings

    port = PORT - 1
    seen = {"connected": [], "disconnected": [], "auth_failed": [], "states": []}
    connected = threading.Event()
    disconnected = threading.Event()

    def on_connected(peer):
        seen["connected"].append(peer)
        connected.set()

    def on_disconnected(peer):
        seen["disconnected"].append(peer)
        disconnected.set()

    events = ControllerEvents(
        on_state=lambda running, error: seen["states"].append(running),
        on_client_connected=on_connected,
        on_client_disconnected=on_disconnected,
        on_auth_failed=lambda peer: seen["auth_failed"].append(peer),
    )
    controller = ServerController(events)
    settings = ServerSettings(password=PASSWORD, host="127.0.0.1", port=port,
                              view_only=True, keep_awake=False)
    controller.start(settings)
    try:
        try:
            ServerController().start(settings)
            raise AssertionError("second server on the same port should fail")
        except RuntimeError as exc:
            print("  port conflict reported:", exc)

        closed = threading.Event()
        cb = Callbacks(confirm_certificate=lambda *a: True, on_closed=lambda e: closed.set())
        client = RFBClient(ConnectOptions("127.0.0.1", port, PASSWORD), cb)
        client.start()
        assert connected.wait(10), "controller never saw the client connect"
        assert controller.clients() == seen["connected"], controller.clients()
        controller.kick(seen["connected"][0])
        assert disconnected.wait(10), "kick did not disconnect the client"
        assert closed.wait(10), "client did not notice the kick"
        assert controller.clients() == []

        run_client(quality_level=6, password="wrong-password-123", expect_error=True, port=port)
        assert seen["auth_failed"], "auth failure not reported"

        controller.restart(settings)
        run_client(quality_level=6, port=port)
    finally:
        controller.stop()
    assert not controller.running
    assert seen["states"] == [True, False, True, False], seen["states"]
    print(f"  events ok: {len(seen['connected'])} connects, states={seen['states']}")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        env = dict(os.environ, PYVNC_PASSWORD=PASSWORD, HOME=home)
        server = subprocess.Popen(
            [sys.executable, "-m", "server.main", "--port", str(PORT), "--host", "127.0.0.1",
             "--view-only", "--no-caffeinate"], cwd=root, env=env)
        try:
            time.sleep(3)
            print("Tight JPEG:")
            run_client(quality_level=6)
            print("Zlib (lossless):")
            run_client(quality_level=None)
            print("Wrong password:")
            run_client(quality_level=6, password="not-the-password", expect_error=True)
            print("ServerController:")
            run_controller()
            print("OK")
        finally:
            server.terminate()
            server.wait(5)


if __name__ == "__main__":
    main()
