#!/usr/bin/env python3
"""Run the FastAPI app with HTTPS using a self-signed certificate."""
import os
import sys

import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, "..")
sys.path.insert(0, APP_DIR)


def main():
    cert_file = os.path.join(BASE_DIR, "..", "certs", "cert.pem")
    key_file = os.path.join(BASE_DIR, "..", "certs", "key.pem")

    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        print("Missing certs. Generate them first:")
        print("  python scripts/setup_https.py")
        sys.exit(1)

    from main import app

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=False,
        ssl_keyfile=key_file,
        ssl_certfile=cert_file,
    )


if __name__ == "__main__":
    main()
