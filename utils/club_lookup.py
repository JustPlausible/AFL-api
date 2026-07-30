# utils/club_lookup.py
import sqlite3
import json
from utils.log import log
from db.connection import get_db_path
from db.club_seed import load_club_seed
import re

def load_clubs():
    """Load all club data from the database, falling back to the canonical seed."""
    db_path = get_db_path()
    if db_path.exists():
        try:
            # This lookup deliberately uses a direct connection because a missing or
            # unreadable database falls back to JSON. Resolve the central path at call
            # time for test overrides, and preserve the existing tuple-row behaviour.
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT code, name, slug, website, squad_url, aliases FROM clubs ORDER BY code")
                rows = cur.fetchall()

            clubs = []
            for row in rows:
                aliases_raw = row[5]
                aliases = []
                if aliases_raw:
                    try:
                        aliases = json.loads(aliases_raw)
                    except json.JSONDecodeError:
                        log(f"⚠️ Failed to parse aliases for club {row[0]}", "WARN")

                clubs.append({
                    "code": row[0],
                    "name": row[1],
                    "slug": row[2],
                    "website": row[3],
                    "squad_url": row[4],
                    "aliases": aliases
                })
            return clubs
        except Exception as e:
            log(f"⚠️ Failed to load clubs from DB: {e}, falling back to canonical seed", "WARN")

    return load_club_seed()

def get_club(identifier: str) -> dict | None:
    """
    Lookup a club by slug or short code (case-insensitive).
    Returns the full club dictionary or None if not found.
    """
    clubs = load_clubs()
    identifier = identifier.strip().lower()

    for club in clubs:
        if club["slug"].lower() == identifier or club["code"].lower() == identifier:
            return club
    return None

def get_club_by_slug(slug: str) -> dict | None:
    """Lookup full club metadata using slug (e.g. 'portadelaide')"""
    clubs = load_clubs()
    return next((club for club in clubs if club["slug"] == slug.lower()), None)

def resolve_club_code(name: str) -> str:
    """
    Attempts to resolve a team name (e.g. 'Fremantle') to a standard club code (e.g. 'FRE').
    Falls back to original name if no match is found, and logs a warning.
    """
    name = re.sub(r"[^a-z]", "", name.strip().lower())
    clubs = load_clubs()

    for club in clubs:
        # Match on official name
        if club["name"].lower().startswith(name) or name in club["name"].lower():
            return club["code"]

        aliases = club.get("aliases") or []
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except json.JSONDecodeError:
                aliases = []

        # Exact alias match
        for alias in aliases:
            alias_clean = re.sub(r"[^a-z]", "", alias.lower())
            if name == alias_clean:
                return club["code"]

        # New: partial/startswith fallback for tricky cases
        for alias in aliases:
            alias_clean = re.sub(r"[^a-z]", "", alias.lower())
            if name.startswith(alias_clean):
                log(f"🧩 Partial alias match: '{name}' starts with '{alias_clean}' → {club['code']}", "DEBUG")
                return club["code"]

    log(f"⚠️ Unmatched team name: '{name}'", "WARN")
    return name
