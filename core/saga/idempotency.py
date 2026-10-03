"""Deterministic idempotency keys. Never mint a new key for a retry."""
import hashlib
import os
import time


def request_id(purchase_id: str, step: str) -> str:
    return hashlib.sha256(f"{purchase_id}:{step}".encode()).hexdigest()


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """26-char ULID: 48-bit ms timestamp + 80 bits randomness (Crockford base32)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))
