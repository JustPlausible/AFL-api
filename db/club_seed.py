"""Validated access to the repository's canonical AFL club bootstrap seed."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

CLUB_SEED_PATH = Path(__file__).resolve().parents[1] / "bootstrap" / "clubs.json"
CLUB_SEED_SCHEMA_VERSION = 1
_REQUIRED_FIELDS: dict[str, type] = {
    "name": str,
    "slug": str,
    "canonicalCode": str,
    "teamId": int,
    "clubId": int,
    "providerId": str,
    "abbreviation": str,
    "clubSiteUrl": str,
    "squadUrl": str,
    "editorialAliases": list,
}


class ClubSeedError(ValueError):
    """Raised when the committed club seed does not satisfy its contract."""


def _validate_club(club: Any, index: int) -> dict[str, Any]:
    if not isinstance(club, dict):
        raise ClubSeedError(f"clubs[{index}] must be an object")
    missing = sorted(set(_REQUIRED_FIELDS) - set(club))
    if missing:
        raise ClubSeedError(f"clubs[{index}] is missing required fields: {', '.join(missing)}")
    for field, expected in _REQUIRED_FIELDS.items():
        value = club[field]
        if not isinstance(value, expected) or expected is int and isinstance(value, bool):
            raise ClubSeedError(f"clubs[{index}].{field} must be a {expected.__name__}")
        if expected is str and not value.strip():
            raise ClubSeedError(f"clubs[{index}].{field} must not be empty")
    if not all(isinstance(alias, str) and alias.strip() for alias in club["editorialAliases"]):
        raise ClubSeedError(f"clubs[{index}].editorialAliases must contain non-empty strings")
    return club


def load_club_seed(path: Path | str = CLUB_SEED_PATH) -> list[dict[str, Any]]:
    """Load, validate, and map canonical seed names to the legacy clubs schema."""
    seed_path = Path(path)
    try:
        document = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClubSeedError(f"Cannot load club seed {seed_path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ClubSeedError("club seed root must be an object")
    if document.get("schema_version") != CLUB_SEED_SCHEMA_VERSION:
        raise ClubSeedError(
            f"unsupported club seed schema_version {document.get('schema_version')!r}; "
            f"expected {CLUB_SEED_SCHEMA_VERSION}"
        )
    raw_clubs = document.get("clubs")
    if not isinstance(raw_clubs, list) or not raw_clubs:
        raise ClubSeedError("club seed clubs must be a non-empty array")

    clubs: list[dict[str, Any]] = []
    seen: dict[str, set[Any]] = {field: set() for field in ("canonicalCode", "slug", "teamId", "clubId", "providerId")}
    for index, raw_club in enumerate(raw_clubs):
        club = _validate_club(raw_club, index)
        for field, values in seen.items():
            value = club[field]
            if value in values:
                raise ClubSeedError(f"duplicate clubs[{index}].{field}: {value!r}")
            values.add(value)
        # Retain every canonical field while exposing legacy names at this boundary.
        mapped = dict(club)
        mapped.update(
            code=club["canonicalCode"], website=club["clubSiteUrl"],
            squad_url=club["squadUrl"], aliases=list(club["editorialAliases"]),
        )
        clubs.append(mapped)
    return clubs


def upsert_club_seed(conn: sqlite3.Connection, clubs: list[dict[str, Any]] | None = None) -> int:
    """Idempotently insert or refresh canonical clubs without deleting other rows."""
    records = load_club_seed() if clubs is None else clubs
    conn.executemany(
        """
        INSERT INTO clubs (code, name, slug, website, squad_url, aliases)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name, slug=excluded.slug, website=excluded.website,
            squad_url=excluded.squad_url, aliases=excluded.aliases
        """,
        [(c["code"], c["name"], c["slug"], c["website"], c["squad_url"],
          json.dumps(c["aliases"], ensure_ascii=False)) for c in records],
    )
    return len(records)
