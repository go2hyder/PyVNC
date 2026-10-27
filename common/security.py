"""TLS, certificate and authentication helpers shared by server and client."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import socket
import ssl
from pathlib import Path

from Crypto.Cipher import DES


def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        path = base / "PyVNC"
    else:
        path = Path.home() / ".pyvnc"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# VNC DES challenge/response (security type 2)
# ---------------------------------------------------------------------------

def _vnc_des_key(password: str) -> bytes:
    """VNC uses the first 8 password bytes with each byte's bits mirrored."""
    key = password.encode("latin-1", errors="replace")[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in key)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    return DES.new(_vnc_des_key(password), DES.MODE_ECB).encrypt(challenge)


# ---------------------------------------------------------------------------
# Server certificate
# ---------------------------------------------------------------------------

def ensure_server_certificate(directory: Path | None = None) -> tuple[Path, Path]:
    """Create (once) a self-signed RSA certificate for the server."""
    directory = directory or config_dir()
    cert_path = directory / "server-cert.pem"
    key_path = directory / "server-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"PyVNC {socket.gethostname()}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def cert_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def pem_fingerprint(cert_path: Path) -> str:
    return cert_fingerprint(ssl.PEM_cert_to_DER_cert(cert_path.read_text()))


def server_tls_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def client_tls_context() -> ssl.SSLContext:
    """The server cert is self-signed, so we pin its fingerprint instead of using CAs."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------------------
# Client-side trust-on-first-use store
# ---------------------------------------------------------------------------

class KnownHosts:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "known_hosts.json")
        try:
            self._data: dict[str, str] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._data = {}

    def get(self, host: str, port: int) -> str | None:
        return self._data.get(f"{host}:{port}")

    def set(self, host: str, port: int, fingerprint: str) -> None:
        self._data[f"{host}:{port}"] = fingerprint
        self.path.write_text(json.dumps(self._data, indent=2))

    def remove(self, host: str, port: int) -> None:
        if self._data.pop(f"{host}:{port}", None) is not None:
            self.path.write_text(json.dumps(self._data, indent=2))
