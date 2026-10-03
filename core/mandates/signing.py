"""ES256 signatures from an owner's registered device (WebAuthn-style passkey).

The device signs canonical_json(statement). WebCrypto produces raw r||s (64 bytes);
other clients produce DER. Both are accepted."""
import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from core.ledger.hashchain import canonical_json


def b64d(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def load_public_key(pem_or_spki_b64: str):
    data = pem_or_spki_b64.strip()
    if data.startswith("-----BEGIN"):
        key = serialization.load_pem_public_key(data.encode())
    else:
        key = serialization.load_der_public_key(b64d(data))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("device key must be ECDSA P-256")
    return key


def verify(public_key: str, statement: dict, signature_b64: str) -> bool:
    try:
        key = load_public_key(public_key)
        sig = b64d(signature_b64)
        if len(sig) == 64:
            sig = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        key.verify(sig, canonical_json(statement).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def generate_key():
    return ec.generate_private_key(ec.SECP256R1())


def sign(private_key, statement: dict) -> str:
    """Client-side helper (tests, seed script, CLI phone)."""
    sig = private_key.sign(canonical_json(statement).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(sig).decode()


def public_pem(private_key) -> str:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def private_pem(private_key) -> str:
    return private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode()


def load_private_pem(pem: str):
    return serialization.load_pem_private_key(pem.encode(), password=None)
