"""HMAC request signing shared by the Arena platform and the runner.

Canonical string (UTF-8, newline separated):
    METHOD \n PATH(+?query) \n TIMESTAMP \n NONCE \n SHA256_HEX(BODY)
Headers: X-Arena-Key-Id, X-Arena-Timestamp (unix seconds), X-Arena-Nonce,
         X-Arena-Signature (hex HMAC-SHA256).
The verifier rejects: unknown key ids, |now - timestamp| > max_skew, nonces
seen within the replay window, and signature mismatches (constant time).
"""

import hashlib
import hmac
import secrets
import time

HEADER_KEY = "X-Arena-Key-Id"
HEADER_TS = "X-Arena-Timestamp"
HEADER_NONCE = "X-Arena-Nonce"
HEADER_SIG = "X-Arena-Signature"


class SignatureError(Exception):
    pass


def canonical(method, path, timestamp, nonce, body):
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return "\n".join([method.upper(), path, str(timestamp), nonce, body_hash]).encode()


def sign(secret, method, path, body=b"", key_id="default", timestamp=None, nonce=None):
    timestamp = int(timestamp if timestamp is not None else time.time())
    nonce = nonce or secrets.token_hex(16)
    if isinstance(secret, str):
        secret = secret.encode()
    sig = hmac.new(
        secret, canonical(method, path, timestamp, nonce, body), hashlib.sha256
    ).hexdigest()
    return {
        HEADER_KEY: key_id,
        HEADER_TS: str(timestamp),
        HEADER_NONCE: nonce,
        HEADER_SIG: sig,
    }


def verify(keys, method, path, body, headers, nonce_store, max_skew=60, now=None):
    """keys: {key_id: secret}. nonce_store.seen(nonce, ttl) -> bool (True if
    already seen; must atomically remember it otherwise)."""
    key_id = headers.get(HEADER_KEY)
    ts = headers.get(HEADER_TS)
    nonce = headers.get(HEADER_NONCE)
    sig = headers.get(HEADER_SIG)
    if not (key_id and ts and nonce and sig):
        raise SignatureError("missing signature headers")
    secret = keys.get(key_id)
    if secret is None:
        raise SignatureError("unknown key id")
    try:
        ts_int = int(ts)
    except ValueError:
        raise SignatureError("bad timestamp")
    now = int(now if now is not None else time.time())
    if abs(now - ts_int) > max_skew:
        raise SignatureError("timestamp outside allowed skew")
    if len(nonce) < 16 or len(nonce) > 64:
        raise SignatureError("bad nonce")
    if isinstance(secret, str):
        secret = secret.encode()
    expected = hmac.new(
        secret, canonical(method, path, ts_int, nonce, body), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("bad signature")
    # Remember the nonce only after the signature is valid (no cache pollution).
    if nonce_store.seen(f"{key_id}:{nonce}", ttl=2 * max_skew + 5):
        raise SignatureError("replayed request")
    return key_id
