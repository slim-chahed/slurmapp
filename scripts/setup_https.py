#!/usr/bin/env python3
"""Generate a self-signed cert for local HTTPS development."""
import os
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime
except ImportError:
    raise SystemExit("cryptography is required. Install it with: pip install cryptography")

BASE_DIR = Path(__file__).resolve().parent.parent
CERT_PATH = BASE_DIR / "certs" / "cert.pem"
KEY_PATH = BASE_DIR / "certs" / "key.pem"


def generate():
    if CERT_PATH.exists() and KEY_PATH.exists():
        print(f"Cert already exists at {CERT_PATH}")
        return

    key = rsa.generate_private_key(key_size=2048, public_exponent=65537)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HPC Esprit"),
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    KEY_PATH.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Generated cert: {CERT_PATH}")
    print(f"Generated key:  {KEY_PATH}")


if __name__ == "__main__":
    generate()
