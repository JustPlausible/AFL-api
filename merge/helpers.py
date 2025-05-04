import re
import json
from pathlib import Path
from utils.log import log
from enrich.afl_com import resolve_player
from utils.club_lookup import get_club_by_slug
import sqlite3
from difflib import get_close_matches
from utils.dictionary import KNOWN_NICKNAMES

def extract_club_player_id(url: str) -> int:
    match = re.search(r"/players/(\d+)", url)
    return int(match.group(1)) if match else None

def resolve_players_for_club(club_slug: str, skip_existing=False):
    """
    Resolves all players from a club's raw JSON file and writes enriched output.
    """
    raw_path = Path(f"data/players-{club_slug}-raw.json")
    output_path = Path(f"data/players-{club_slug}.json")

    club = get_club_by_slug(club_slug)
    display_name = f"{club['name']} [{club['code']}]" if club else club_slug.upper()

    if skip_existing and output_path.exists():
        log(f"⏩ Skipping {display_name} (enriched file exists)", "DEBUG")
        return

    if not raw_path.exists():
        log(f"[!] Missing raw file for {display_name}", "ERROR")
        return

    with raw_path.open("r") as f:
        raw_players = json.load(f)

    enriched_players = []
    for player in raw_players:
        enriched = resolve_player(player)
        if enriched:
            enriched_players.append(enriched)

    with output_path.open("w") as f:
        json.dump(enriched_players, f, indent=2)

    log(f"✅ Enriched {len(enriched_players)} players for {display_name} → {output_path}", "INFO")

NICKNAME_SUGGESTION_FILE = Path("logs/nickname_suggestions.txt")
NICKNAME_MAP = {}
for canonical, nicknames in KNOWN_NICKNAMES.items():
    for nickname in nicknames:
        NICKNAME_MAP[nickname.lower()] = canonical.lower()

def log_nickname_suggestion(name: str, club: str):
    NICKNAME_SUGGESTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NICKNAME_SUGGESTION_FILE.open("a") as f:
        f.write(f"{club},{name}\n")

def match_injury_player_to_db(name: str, club_slug: str, conn: sqlite3.Connection | None = None, db_path="data/afl_players.db") -> int | None:
    """
    Attempts to match an injury player's name to the database and return their AFL ID.
    Accepts an optional open DB connection for performance.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        close_conn = True

    cur = conn.cursor()

    name = name.strip()
    parts = name.split()
    first_name = parts[0] if len(parts) > 1 else ""
    last_name = parts[-1] if len(parts) > 1 else name

    # Exact full_name match
    cur.execute("""
        SELECT * FROM players
        WHERE LOWER(full_name) = LOWER(?) AND LOWER(club) = LOWER(?)
    """, (name, club_slug))
    row = cur.fetchone()
    if row:
        if close_conn: conn.close()
        return row["afl_id"]

    # Exact first + last match
    cur.execute("""
        SELECT * FROM players
        WHERE LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?) AND LOWER(club) = LOWER(?)
    """, (first_name, last_name, club_slug))
    row = cur.fetchone()
    if row:
        if close_conn: conn.close()
        return row["afl_id"]

    # 🔁 Nickname fallback (e.g. Lachie → Lachlan)
    if first_name.lower() in NICKNAME_MAP:
        alt_first = NICKNAME_MAP[first_name.lower()]
        cur.execute("""
            SELECT * FROM players
            WHERE LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?) AND LOWER(club) = LOWER(?)
        """, (alt_first, last_name.lower(), club_slug.lower()))
        row = cur.fetchone()
        if row:
            if close_conn: conn.close()
            return row["afl_id"]

    # Loose match fallback
    cur.execute("SELECT full_name FROM players WHERE LOWER(club) = LOWER(?)", (club_slug,))
    names = [r["full_name"] for r in cur.fetchall()]
    matches = get_close_matches(name, names, n=1, cutoff=0.85)
    if matches:
        cur.execute("SELECT afl_id FROM players WHERE full_name = ? AND LOWER(club) = LOWER(?)", (matches[0], club_slug))
        row = cur.fetchone()
        if row:
            if close_conn: conn.close()
            return row["afl_id"]

    # After all match attempts fail:
    log(f"❌ No match for player '{name}' ({club_slug})", "WARN")
    log_nickname_suggestion(name, club_slug)

    if close_conn:
        conn.close()
    return None
