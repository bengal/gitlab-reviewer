"""Secret encryption and shared-password hashing.

- Secrets (GitLab token, model API keys) are encrypted at rest with Fernet,
  keyed by the SECRET_ENC_KEY environment variable.
- The shared app password is stored as a PBKDF2-HMAC-SHA256 hash with the
  format: pbkdf2$<iterations>$<salt_hex>$<hash_hex>
"""

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

from app.config import get_settings

PBKDF2_ITERATIONS = 200_000
_SALT_BYTES = 16


def _fernet(key: str | None = None) -> Fernet:
    if key is None:
        key = get_settings().secret_enc_key
    if not key:
        raise RuntimeError("SECRET_ENC_KEY is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(value: str, key: str | None = None) -> str:
    """Encrypt a secret for storage. Empty strings pass through unencrypted."""
    if value == "":
        return ""
    return _fernet(key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str, key: str | None = None) -> str:
    """Decrypt a stored secret. Empty strings pass through unchanged."""
    if value == "":
        return ""
    return _fernet(key).decrypt(value.encode("ascii")).decode("utf-8")


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash a shared password as pbkdf2$<iter>$<salt_hex>$<hash_hex>."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against a pbkdf2$<iter>$<salt_hex>$<hash_hex> string."""
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2":
        return False
    _, iterations_s, salt_hex, hash_hex = parts
    try:
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)
