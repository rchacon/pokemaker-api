"""PIN hashing and unlock tokens.

PINs are hashed with scrypt, a deliberately slow, memory-hard function, so a
leaked table doesn't reveal them instantly. (With only 10,000 possible 4-digit
PINs, the real protection is the API's attempt limit; hashing just avoids
storing them in plain text.)

Unlock tokens are HMAC-signed strings, not database sessions: verifying one is
a hash computation, and nothing has to be stored or cleaned up. They carry the
profile's `pinVersion`, so changing a PIN invalidates every older token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

# n=2**14, r=8: ~16 MB and ~50-80 ms per hash. hashlib.scrypt releases the GIL,
# so calling it via asyncio.to_thread keeps the event loop free AND lets several
# hashes run in parallel threads — a rare case where threads speed up CPU work.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_pin(pin: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(
        pin.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(_unb64(digest))
    )
    return hmac.compare_digest(candidate, _unb64(digest))  # constant-time comparison


@dataclass(frozen=True)
class TokenClaims:
    family_id: str
    profile_id: str
    pin_version: int
    expires_at: int  # unix seconds


def make_token(secret: bytes, claims: TokenClaims) -> str:
    payload = f"{claims.family_id}:{claims.profile_id}:{claims.pin_version}:{claims.expires_at}"
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    return f"v1.{_b64(payload.encode())}.{_b64(signature)}"


def read_token(secret: bytes, token: str) -> TokenClaims | None:
    """Return the claims of a correctly signed token, or None. Expiry is checked by the caller."""
    try:
        version, payload_b64, signature_b64 = token.split(".")
        if version != "v1":
            return None
        payload = _unb64(payload_b64)
        expected = hmac.new(secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature_b64)):
            return None
        family_id, profile_id, pin_version, expires_at = payload.decode().split(":")
        return TokenClaims(family_id, profile_id, int(pin_version), int(expires_at))
    except (ValueError, UnicodeDecodeError):
        return None
