"""Self-signed HTTPS certificate for the lab website.

Some browsers/devices insist on https://. The website therefore also listens with TLS
(default port 8443) using a certificate generated on first start and kept in the data
folder. Browsers show a one-time "not private / not secure" warning because the
certificate is self-made; choose Advanced -> Proceed (Chrome/Edge) or Show Details ->
visit this website (Safari). Traffic, including the password, is then encrypted.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path


def local_ips() -> list[str]:
    """This computer's LAN IPv4 address(es) (no traffic is sent)."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def ensure_cert(data: Path) -> tuple[Path, Path]:
    """Create (once) and return (cert.pem, key.pem) in <data>/tls/."""
    d = Path(data) / "tls"
    cert_p, key_p = d / "cert.pem", d / "key.pem"
    if cert_p.exists() and key_p.exists():
        return cert_p, key_p
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "liverhu"
    names: list = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if host and host != "localhost":
        names.append(x509.DNSName(host))
    for ip in local_ips():
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HUnter lab website")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    try:
        key_p.chmod(0o600)
    except OSError:
        pass
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p
