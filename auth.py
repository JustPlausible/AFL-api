import sqlite3
from dataclasses import dataclass
from fastapi import Depends, Header, HTTPException
from utils.log import log
from api_key_security import api_key_prefix, verify_api_key_hash
from db.connection import get_db_path


def get_db_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _fingerprint_api_key(api_key: str) -> str:
    if not api_key:
        return "<empty>"
    return f"{api_key_prefix(api_key)}…"


@dataclass(frozen=True)
class AuthenticatedCredential:
    """Non-secret identity and capabilities attached to one authenticated key."""

    key_id: int
    label: str
    capabilities: frozenset[str]

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


def authenticate_api_key(x_api_key: str | None = Header(None)) -> AuthenticatedCredential:
    conn = get_db_connection()
    cursor = conn.execute(
        "SELECT id, label, key_hash FROM api_keys WHERE is_active = 1 AND key_hash IS NOT NULL",
    )
    result = None
    for row in cursor.fetchall():
        if x_api_key is not None and verify_api_key_hash(x_api_key, row["key_hash"]):
            result = row
            break
    if not result:
        conn.close()
        log(f"🔐 Invalid API Key attempted: {_fingerprint_api_key(x_api_key)}", "WARN")
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")

    capabilities = frozenset(
        row[0] for row in conn.execute(
            "SELECT capability FROM api_key_capabilities WHERE api_key_id = ?",
            (result["id"],),
        ).fetchall()
    )
    conn.close()
    credential = AuthenticatedCredential(result["id"], result["label"], capabilities)
    log(f"🔐 Authenticated request from: {credential.label}", "DEBUG")
    return credential


def verify_api_key(x_api_key: str = Header(...)) -> str:
    """Backwards-compatible authentication dependency returning the client label."""
    return authenticate_api_key(x_api_key).label


def require_capability(capability: str):
    """Build a reusable FastAPI dependency requiring a named capability."""
    def dependency(
        credential: AuthenticatedCredential = Depends(authenticate_api_key),
    ) -> AuthenticatedCredential:
        if not credential.has_capability(capability):
            raise HTTPException(status_code=403, detail="Required API capability is not granted")
        return credential

    return dependency
