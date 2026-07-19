import hashlib
import hmac
import secrets

_HASH_PREFIX = "sha256:"
_PREFIX_LENGTH = 8


def generate_api_key() -> str:
    """Generate a high-entropy API key for one-time display to the caller."""
    return secrets.token_urlsafe(32)


def api_key_prefix(api_key: str) -> str:
    """Return a short non-sensitive prefix for administrative identification."""
    return api_key[:_PREFIX_LENGTH]


def hash_api_key(api_key: str) -> str:
    """Hash a high-entropy API key using SHA-256 for non-reversible storage."""
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return f"{_HASH_PREFIX}{digest}"


def is_hashed_api_key(value: str | None) -> bool:
    return bool(value and value.startswith(_HASH_PREFIX))


def verify_api_key_hash(presented_key: str, stored_hash: str) -> bool:
    """Compare a presented API key with a stored digest in constant time."""
    if not is_hashed_api_key(stored_hash):
        return False
    return hmac.compare_digest(hash_api_key(presented_key), stored_hash)
