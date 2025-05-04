import json
from pathlib import Path

def load_clubs():
    path = Path("data/clubs.json")
    with path.open("r") as f:
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
