"""Encryption of provider credentials and session connection data at rest.

Key: ARENA_SECRET_KEY (recommended, 32+ chars) or the CTFd SECRET_KEY,
stretched with HKDF-SHA256 into a Fernet key. Rotating the key requires
re-entering provider credentials (documented in SECURITY_GUIDE.md).
"""

import base64
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app


def _fernet():
    secret = (
        current_app.config.get("ARENA_SECRET_KEY") or current_app.config["SECRET_KEY"]
    )
    if isinstance(secret, str):
        secret = secret.encode()
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"arena-labs-v1",
        info=b"arena credentials",
    ).derive(secret)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value):
    if value is None:
        return None
    return _fernet().encrypt(json.dumps(value).encode()).decode()


def decrypt(token):
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return None
