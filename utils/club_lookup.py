# utils/club_lookup.py
import sqlite3
from pathlib import Path
import json
from utils.log import log
import config
import re

CLUBS_JSON = Path("data/clubs.json")
DB_PATH = Path(config.DB_PATH)

def load_clubs():
    """Load all club data from the database, fallback to JSON."""
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT code, name, slug, website, squad_url, aliases FROM clubs ORDER BY code")
            rows = cur.fetchall()
            conn.close()

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
            log(f"⚠️ Failed to load clubs from DB: {e}, falling back to JSON", "WARN")

    # Fallback to static JSON
    with CLUBS_JSON.open("r") as f:
        return json.load(f)

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

