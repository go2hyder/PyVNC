"""Runs the VNC server on a background asyncio thread so a UI (or the CLI) can control it."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from common.security import config_dir, ensure_server_certificate, pem_fingerprint, server_tls_context
from server.capture import ScreenCapture
from server.session import AuthGuard, ServerConfig, Session, SessionEvents

log = logging.getLogger("pyvnc")


@dataclass
class ServerSettings:
    password: str = ""
    host: str = "0.0.0.0"
    port: int = 5900
    display: int = 1
    scale: float = 1.0
    fps: float = 20.0
    quality: int | None = None
    view_only: bool = False
    swap_ctrl_cmd: bool = False
    clipboard: bool = True
    keep_awake: bool = True
    allow_insecure: bool = False
    cert_dir: Path | None = None


@dataclass
class ControllerEvents:
    """Callbacks; invoked from the server thread."""
    on_state: Callable[[bool, str | None], None] = lambda running, error: None
    on_client_connected: Callable[[str], None] = lambda peer: None
    on_client_disconnected: Callable[[str], None] = lambda peer: None
    on_auth_failed: Callable[[str], None] = lambda peer: None
    on_log: Callable[[str], None] | None = None


class _LogForwarder(logging.Handler):
    def __init__(self, callback: Callable[[str], None]):
        super().__init__()
        self.callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(self.format(record))
        except Exception:
            pass


class ServerController(SessionEvents):
    def __init__(self, events: ControllerEvents | None = None):
        self.events = events or ControllerEvents()
        self.settings: ServerSettings | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._caffeinate: subprocess.Popen | None = None
        self._log_handler: logging.Handler | None = None
        if self.events.on_log is not None:
            self._log_handler = _LogForwarder(self.events.on_log)
            log.addHandler(self._log_handler)
            log.setLevel(logging.INFO)

    # -- public API (call from any thread) ---------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def clients(self) -> list[str]:
        with self._lock:
            return [peer for peer, s in self._sessions.items() if s.connected]

    def fingerprint(self, cert_dir: Path | None = None) -> str:
        cert_path, _ = ensure_server_certificate(cert_dir)
        return pem_fingerprint(cert_path)

    def regenerate_certificate(self, cert_dir: Path | None = None) -> str:
        directory = cert_dir or config_dir()
        for name in ("server-cert.pem", "server-key.pem"):
            (directory / name).unlink(missing_ok=True)
        return self.fingerprint(cert_dir)

    def start(self, settings: ServerSettings, timeout: float = 10.0) -> None:
        """Start listening. Raises RuntimeError if the server cannot start."""
        if self.running:
            raise RuntimeError("Server is already running")
        if len(settings.password) < 8:
            raise RuntimeError("Use a password of at least 8 characters")
        self.settings = replace(settings)
        started = threading.Event()
        result: dict[str, str | None] = {"error": None}
        self._thread = threading.Thread(target=self._thread_main, args=(started, result),
                                        name="vnc-server", daemon=True)
        self._thread.start()
        if not started.wait(timeout):
            raise RuntimeError("Server did not start in time")
        if result["error"]:
            self._thread.join(2)
            self._thread = None
            raise RuntimeError(result["error"])

    def stop(self, timeout: float = 10.0) -> None:
        if not self.running:
            return
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            loop.call_soon_threadsafe(stop.set)
        self._thread.join(timeout)
        self._thread = None

    def restart(self, settings: ServerSettings) -> None:
        self.stop()
        self.start(settings)

    def kick(self, peer: str) -> None:
        with self._lock:
            session = self._sessions.get(peer)
        if session is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(session.disconnect)

    def wait(self) -> None:
        """Block until the server thread exits (used by the CLI)."""
        while self.running:
            self._thread.join(0.5)

    # -- session events (server thread) ------------------------------------

    def session_started(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.peer] = session

    def session_connected(self, session: Session) -> None:
        self.events.on_client_connected(session.peer)

    def session_closed(self, session: Session) -> None:
        with self._lock:
            self._sessions.pop(session.peer, None)
        if session.connected:
            self.events.on_client_disconnected(session.peer)

    def auth_failed(self, session: Session) -> None:
        self.events.on_auth_failed(session.peer)

    # -- server thread -----------------------------------------------------

    def _thread_main(self, started: threading.Event, result: dict) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._serve(started, result))
        except Exception as exc:
            log.exception("server crashed")
            result["error"] = result["error"] or str(exc)
            started.set()
        finally:
            self._loop.close()
            self._loop = None
            self._stop_caffeinate()
            self.events.on_state(False, result["error"])

    async def _serve(self, started: threading.Event, result: dict) -> None:
        s = self.settings
        self._stop = asyncio.Event()
        try:
            cert_path, key_path = ensure_server_certificate(s.cert_dir)
            config = ServerConfig(
                password=s.password,
                ssl_context=server_tls_context(cert_path, key_path),
                allow_insecure=s.allow_insecure,
                fps=max(1.0, s.fps),
                forced_quality=s.quality,
                view_only=s.view_only,
                name=f"PyVNC on {socket.gethostname()}",
            )
            capture = ScreenCapture(s.display, s.scale)
            injector = _make_injector(s)
            clipboard = _make_clipboard() if s.clipboard else None
            capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
            guard = AuthGuard()

            session_tasks: set[asyncio.Task] = set()

            async def on_client(reader, writer):
                task = asyncio.current_task()
                session_tasks.add(task)
                try:
                    await Session(reader, writer, config, capture, capture_executor,
                                  injector, clipboard, guard, events=self).run()
                finally:
                    session_tasks.discard(task)

            server = await asyncio.start_server(on_client, s.host, s.port)
        except OSError as exc:
            in_use = exc.errno in (48, 10048) or getattr(exc, "winerror", None) == 10048
            if in_use and sys.platform == "win32":
                result["error"] = f"Port {s.port} is already in use. Choose another port."
            elif in_use:
                result["error"] = (f"Port {s.port} is already in use. If macOS Screen Sharing is on, "
                                   "it owns 5900: turn it off or choose another port.")
            else:
                result["error"] = f"Cannot listen on {s.host}:{s.port}: {exc.strerror or exc}"
            started.set()
            return
        except Exception as exc:
            result["error"] = str(exc)
            started.set()
            return

        if s.keep_awake:
            self._start_caffeinate()
        log.info("Listening on %s:%d (display %d, %dx%d points)", s.host, s.port, s.display,
                 capture.monitor["width"], capture.monitor["height"])
        log.info("TLS certificate fingerprint (SHA-256): %s", pem_fingerprint(cert_path))
        if s.allow_insecure:
            log.warning("Unencrypted VNC auth is enabled. Do NOT expose this port to the internet.")
        started.set()
        self.events.on_state(True, None)

        await self._stop.wait()
        log.info("Stopping server")
        server.close()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.disconnect()
        pending = set(session_tasks)
        if pending:
            _done, pending = await asyncio.wait(pending, timeout=5)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=5)
        except asyncio.TimeoutError:
            pass
        capture_executor.shutdown(wait=False)
        log.info("Server stopped")

    def _start_caffeinate(self) -> None:
        if sys.platform == "win32":
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)
            self._awake = True
            return
        if shutil.which("caffeinate"):
            self._caffeinate = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])

    def _stop_caffeinate(self) -> None:
        if sys.platform == "win32":
            if getattr(self, "_awake", False):
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
                self._awake = False
            return
        if self._caffeinate is not None:
            self._caffeinate.terminate()
            self._caffeinate = None


def _make_injector(settings: ServerSettings):
    if sys.platform == "win32":
        from server.input_win import InputInjector
        return InputInjector(swap_ctrl_cmd=settings.swap_ctrl_cmd)
    from server.input_mac import InputInjector
    return InputInjector(swap_ctrl_cmd=settings.swap_ctrl_cmd)


def _make_clipboard():
    if sys.platform == "win32":
        from server.clipboard_win import Clipboard
        return Clipboard()
    from server.clipboard_mac import Clipboard
    return Clipboard()
