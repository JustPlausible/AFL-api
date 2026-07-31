from datetime import datetime, timezone
import re
import json
from pathlib import Path
from utils.log import log
#from enrich.afl_com import resolve_player
from utils.club_lookup import get_club_by_slug
import sqlite3
from db.connection import get_db_connection
from difflib import get_close_matches
from dataclasses import dataclass
import unicodedata
from utils.stats_cache import ensure_leaderboard_fresh
from utils.dictionary import KNOWN_NICKNAMES
from utils.club_lookup import get_canonical_club

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
        raise RuntimeError("Leaderboard file not found after attempted refresh")

    with LEADERBOARD_PATH.open("r") as f:
        leaderboard = json.load(f)

    if not isinstance(leaderboard, list) or not leaderboard:
        raise RuntimeError("Leaderboard contains no player identity records")

    index = {}
    for player in leaderboard:
        champ_id = player.get("champion_data_id")
        afl_id = player.get("afl_id")
        afl_url = player.get("afl_url")
        if champ_id is not None and afl_id and afl_url:
            index[str(champ_id).strip()] = {
                "afl_id": player.get("afl_id"),
                "afl_url": afl_url,
            }
    if not index:
        raise RuntimeError("Leaderboard contains no usable Champion Data to AFL identity mappings")
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

    try:
        leaderboard_index = load_leaderboard_index()
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        # Fresh squad records carry the AFL ID from their canonical profile URL.
        # Continue with those direct identities, but never hide cache failure or
        # invent an identity for older unresolved records.
        log(f"⚠️ Leaderboard identities unavailable: {exc}; using direct squad identities only", "WARN")
        leaderboard_index = {}

    with raw_path.open("r") as f:
        raw_players = json.load(f)

    enriched_players = []
    for player in raw_players:
        champ_id = player.get("champion_data_id")
        champ_id = str(champ_id).strip() if champ_id is not None else None
        leaderboard_data = leaderboard_index.get(champ_id)

        enriched = {
            **player,
            "afl_id": leaderboard_data["afl_id"] if leaderboard_data else player.get("afl_id"),
            "afl_url": leaderboard_data["afl_url"] if leaderboard_data else player.get("afl_url"),
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

@dataclass(frozen=True, slots=True)
class InjuryPlayerResolution:
    status: str
    source_name: str
    source_club: str
    canonical_player_id: int | None = None
    afl_id: int | None = None
    reason: str | None = None


def _normalise_injury_name(name: str) -> str:
    """Apply deterministic punctuation, whitespace, case, and suffix normalisation."""
    value = unicodedata.normalize("NFKC", clean_name(name))
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(value.split()).casefold()


def resolve_canonical_injury_player(
    name: str, club_code: str, conn: sqlite3.Connection
) -> InjuryPlayerResolution:
    """Resolve an injury name only among canonical members of the supplied club."""
    source_name, source_club = name.strip(), club_code.strip()
    canonical_club = get_canonical_club(source_club)
    if canonical_club is None:
        return InjuryPlayerResolution(
            "unresolved", source_name, source_club,
            reason="source club does not resolve to a canonical club identity",
        )
    rows = conn.execute("""
        SELECT cp.id, cp.display_name, cp.given_name, cp.family_name,
               ppi.provider_player_id, season.afl_id, season.year, season.is_current,
               team.afl_id, team.provider_id, team.name, team.abbreviation
        FROM canonical_players cp
        JOIN competition_season_players csp ON csp.player_id = cp.id
        JOIN afl_seasons season ON season.afl_id = csp.competition_season_id
        JOIN afl_teams team ON team.afl_id = csp.team_id
        LEFT JOIN player_provider_ids ppi
          ON ppi.player_id = cp.id AND ppi.provider = 'afl'
    """).fetchall()

    def team_canonical_code(row) -> str | None:
        resolved = {
            club["code"] for identifier in (row[8], row[9], row[10], row[11])
            if identifier is not None and (club := get_canonical_club(str(identifier))) is not None
        }
        return next(iter(resolved)) if len(resolved) == 1 else None

    club_rows = [row for row in rows if team_canonical_code(row) == canonical_club["code"]]
    if club_rows:
        selected_season = max(
            {(1 if row[7] == 1 else 0, row[6] if row[6] is not None else -1, row[5])
             for row in club_rows}
        )[2]
        rows = [row for row in club_rows if row[5] == selected_season]
    else:
        rows = []

    wanted = _normalise_injury_name(source_name)
    matches = []
    for row in rows:
        stored_names = {row[1], " ".join(part for part in (row[2], row[3]) if part)} - {None, ""}
        if wanted in {_normalise_injury_name(candidate) for candidate in stored_names}:
            matches.append(row)

    if not matches:
        parts = clean_name(source_name).split()
        if len(parts) > 1 and parts[0].casefold() in NICKNAME_MAP:
            canonical_first = NICKNAME_MAP[parts[0].casefold()]
            wanted = _normalise_injury_name(" ".join((canonical_first, *parts[1:])))
            for row in rows:
                stored_names = {row[1], " ".join(part for part in (row[2], row[3]) if part)} - {None, ""}
                if wanted in {_normalise_injury_name(candidate) for candidate in stored_names}:
                    matches.append(row)

    unique = {row[0]: row for row in matches}
    if not unique:
        return InjuryPlayerResolution(
            "unresolved", source_name, source_club,
            reason="no canonical player with this normalised name and current club membership",
        )
    if len(unique) > 1:
        return InjuryPlayerResolution(
            "ambiguous", source_name, source_club,
            reason=f"{len(unique)} canonical players match this name and club",
        )
    row = next(iter(unique.values()))
    if row[4] is None:
        return InjuryPlayerResolution(
            "unresolved", source_name, source_club, canonical_player_id=row[0],
            reason="canonical player has no AFL provider identifier",
        )
    try:
        afl_id = int(row[4])
    except (TypeError, ValueError):
        return InjuryPlayerResolution(
            "unresolved", source_name, source_club, canonical_player_id=row[0],
            reason=f"canonical player's AFL provider identifier is not numeric: {row[4]!r}",
        )
    return InjuryPlayerResolution(
        "resolved", source_name, source_club, canonical_player_id=row[0], afl_id=afl_id
    )

def match_injury_player_to_db(name: str, club_slug: str, conn: sqlite3.Connection | None = None) -> int | None:
    """
    Attempts to match an injury player's name to the database and return their AFL ID.
    Accepts an optional open DB connection for performance.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
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
