"""Create Leaf's Web Push (VAPID) key pair once. The private key is written to a file only
the current user can read and is never printed; the public key (not secret) is printed so it
can go in cloudflare/wrangler.jsonc. scripts/deploy_cloudflare.sh ships the private key as a
Worker secret.

    .venv/bin/python -m scripts.gen_vapid            # prints the public key
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

KEY_PATH = Path(
    os.environ.get("LEAF_VAPID_KEY_FILE", "~/.config/leaf/vapid_private.pem")
).expanduser()


def public_key_b64(private_pem: bytes) -> str:
    key = serialization.load_pem_private_key(private_pem, password=None)
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> int:
    if not KEY_PATH.exists():
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        print(f"created {KEY_PATH} (private key, mode 600)", file=sys.stderr)
    print(public_key_b64(KEY_PATH.read_bytes()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
