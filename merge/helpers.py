from datetime import datetime, timezone
import re
import json
from pathlib import Path
from utils.log import log
#from enrich.afl_com import resolve_player
from utils.club_lookup import get_club_by_slug
import sqlite3
from difflib import get_close_matches
from utils.stats_cache import ensure_leaderboard_fresh
from utils.dictionary import KNOWN_NICKNAMES

def extract_club_player_id(url: str) -> int:
    match = re.search(r"/players/(\d+)", url)
    return int(match.group(1)) if match else None

def extract_champion_id(image_url: str) -> str | None:
    match = re.search(r"/(\d+)\.png", image_url)
    return match.group(1) if match else None

LEADERBOARD_PATH = Path("data/afl_stats_leaderboard.json")

def load_leaderboard_index():
    ensure_leaderboard_fresh(max_age_hours=24)

    if not LEADERBOARD_PATH.exists():
        log("❌ Leaderboard file not found after attempted refresh!", "ERROR")
        return {}

    with LEADERBOARD_PATH.open("r") as f:
        leaderboard = json.load(f)

    index = {}
    for player in leaderboard:
        champ_id = player.get("champion_data_id")
        if champ_id:
            index[champ_id] = {
                "afl_id": player.get("afl_id"),
                "afl_url": player.get("afl_url"),
            }
    return index

def resolve_players_for_club(club_slug: str, skip_existing=False):
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

    leaderboard_index = load_leaderboard_index()

    with raw_path.open("r") as f:
        raw_players = json.load(f)

    enriched_players = []
    for player in raw_players:
        champ_id = player.get("champion_data_id")
        leaderboard_data = leaderboard_index.get(champ_id)

        enriched = {
            **player,
            "afl_id": leaderboard_data["afl_id"] if leaderboard_data else player.get("afl_id"),
            "afl_url": leaderboard_data["afl_url"] if leaderboard_data else None,
            "source": "afl-leaderboard" if leaderboard_data else "fallback",
            "resolved_at": datetime.now(timezone.utc).isoformat()
        }

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

SUFFIXES = {"jnr", "jr", "snr", "sr"}

def clean_name(name: str) -> str:
    """Strip suffixes like 'jnr' from names."""
    parts = name.strip().split()
    if parts[-1].lower().strip(".") in SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts)

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

    original_name = name.strip()
    name = clean_name(original_name)  # 🧼 Clean suffix like "jnr"
    if name != original_name:
        log(f"🧽 Normalised injury name: '{original_name}' → '{name}'", "DEBUG")

    parts = name.split()
    first_name = parts[0] if len(parts) > 1 else ""
    last_name = parts[-1] if len(parts) > 1 else name

    # 1️⃣ Exact full_name match
    cur.execute("""
        SELECT * FROM players
        WHERE LOWER(full_name) = LOWER(?) AND LOWER(club) = LOWER(?)
    """, (name, club_slug))
    row = cur.fetchone()
    if row:
        if close_conn: conn.close()
        return row["afl_id"]

    # 2️⃣ Exact first + last match
    cur.execute("""
        SELECT * FROM players
        WHERE LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?) AND LOWER(club) = LOWER(?)
    """, (first_name, last_name, club_slug))
    row = cur.fetchone()
    if row:
        if close_conn: conn.close()
        return row["afl_id"]

    # 3️⃣ Nickname fallback
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

    # 4️⃣ Loose fuzzy match
    cur.execute("SELECT full_name FROM players WHERE LOWER(club) = LOWER(?)", (club_slug,))
    names = [r["full_name"] for r in cur.fetchall()]
    matches = get_close_matches(name, names, n=1, cutoff=0.85)
    if matches:
        cur.execute("SELECT afl_id FROM players WHERE full_name = ? AND LOWER(club) = LOWER(?)", (matches[0], club_slug))
        row = cur.fetchone()
        if row:
            if close_conn: conn.close()
            return row["afl_id"]

    # ❌ No match
    log(f"❌ No match for player '{name}' ({club_slug})", "WARN")
    log_nickname_suggestion(name, club_slug)

    if close_conn:
        conn.close()
    return None

SEASON_ID = "2025014"  # Can be made dynamic later

def extract_champion_data_id_from_html(html: str) -> tuple[str | None, str | None]:
    """Extracts champion_data_id and image_url from any HTML block."""
    match = re.search(r"/(\d+)\.png", html)
    if match:
        champ_id = match.group(1)
        image_url = f"https://s.afl.com.au/staticfile/AFL%20Tenant/AFL/Players/ChampIDImages/AFL/{SEASON_ID}/{champ_id}.png"
        return champ_id, image_url
    return None, None