#db/import_to_db.py
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from utils.log import log
from utils.club_lookup import load_clubs
from db.connection import get_db_connection
import config

#DB_FILE = Path("data/afl_players.db")
DATA_DIR = Path("data")

def resolve_club_code_from_name(club_name: str) -> str | None:
    """
    Given a full club name (e.g. 'Gold Coast Suns'), return the short club code (e.g. 'GCS').
    """
    clubs = load_clubs()
    name_clean = club_name.strip().lower()

    for club in clubs:
        if club['name'].strip().lower() == name_clean:
            return club['code']
    
    return None

def save_clubs_to_db(conn: sqlite3.Connection, clubs: list[dict]):
    """Insert or update clubs in the database using a shared connection."""
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clubs (
            code TEXT PRIMARY KEY,
            name TEXT,
            slug TEXT,
            website TEXT,
            squad_url TEXT,
            aliases TEXT
        )
    """)

    for club in clubs:
        aliases_json = json.dumps(club.get("aliases", []))
        cur.execute("""
            INSERT INTO clubs (code, name, slug, website, squad_url, aliases)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                slug = excluded.slug,
                website = excluded.website,
                squad_url = excluded.squad_url,
                aliases = excluded.aliases
        """, (
            club["code"], club["name"], club["slug"],
            club["website"], club["squad_url"], aliases_json
        ))

    log(f"✅ Imported {len(clubs)} clubs into DB", "SUCCESS")

def export_clubs_from_db():
    """
    Exports the current 'clubs' table to data/clubs-bak.json for safe backup/editing.
    """
    import json
    from pathlib import Path

    DB_PATH = Path(config.DB_PATH)
    OUTPUT_PATH = Path("data/clubs-bak.json")

    if not DB_PATH.exists():
        log("❌ Cannot export clubs — DB does not exist.", "ERROR")
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT code, name, slug, website, squad_url, aliases FROM clubs ORDER BY code")
    rows = cur.fetchall()
    conn.close()

    clubs = []
    for row in rows:
        club = {
            "code": row[0],
            "name": row[1],
            "slug": row[2],
            "website": row[3],
            "squad_url": row[4],
        }
        aliases_raw = row[5]
        if aliases_raw:
            try:
                club["aliases"] = json.loads(aliases_raw)
            except json.JSONDecodeError:
                log(f"⚠️ Could not parse aliases for club {row[0]}", "WARN")
                club["aliases"] = []

        clubs.append(club)

    with OUTPUT_PATH.open("w") as f:
        json.dump(clubs, f, indent=2)

    log(f"✅ Exported {len(clubs)} clubs to {OUTPUT_PATH}", "SUCCESS")

def diff_clubs():
    source_path = Path("data/clubs.json")
    backup_path = Path("data/clubs-bak.json")

    if not source_path.exists() or not backup_path.exists():
        log("❌ Cannot diff clubs — one or both files are missing.", "ERROR")
        return

    with source_path.open() as f:
        source = {c["code"]: c for c in json.load(f)}

    with backup_path.open() as f:
        backup = {c["code"]: c for c in json.load(f)}

    all_codes = sorted(set(source.keys()) | set(backup.keys()))
    added, removed, changed = [], [], []

    for code in all_codes:
        if code not in backup:
            added.append(source[code])
        elif code not in source:
            removed.append(backup[code])
        else:
            diffs = {}
            for field in ["name", "slug", "website", "squad_url", "aliases"]:
                s_val = source[code].get(field)
                b_val = backup[code].get(field)

                # Normalise aliases for fair comparison
                if field == "aliases":
                    s_val = s_val or []
                    b_val = b_val or []
                    if sorted(s_val) != sorted(b_val):
                        diffs[field] = {"old": b_val, "new": s_val}
                elif s_val != b_val:
                    diffs[field] = {"old": b_val, "new": s_val}

            if diffs:
                changed.append({
                    "code": code,
                    "diffs": diffs
                })

    return added, removed, changed

def import_players():
    if not Path(config.DB_PATH).exists():
        log("❌ Database not found. Run init_db.py first.", "ERROR")
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            afl_id INTEGER UNIQUE NOT NULL,
            full_name TEXT,
            first_name TEXT,
            last_name TEXT,
            nickname TEXT,
            formatted_nickname TEXT,
            formatted_last_name TEXT,
            club TEXT,
            guernsey INTEGER,
            position TEXT,
            club_profile_url TEXT,
            image_url TEXT,
            club_player_id INTEGER,
            afl_url TEXT,
            champion_data_id TEXT,
            source TEXT,
            scraped_at TEXT,
            resolved_at TEXT,
            last_updated TEXT
        )
    """)

    enriched_files = sorted(f for f in DATA_DIR.glob("players-*.json") if "-raw" not in f.stem)
    total_imported = 0

    for file in enriched_files:
        with file.open() as f:
            players = json.load(f)

        for player in players:
            if not player.get("afl_id"):
                log(f"⚠️ Skipping player without afl_id: {player.get('full_name', 'Unknown')} in file {file.name}", "WARN")
                continue
            cursor.execute("""
                INSERT INTO players (
                    afl_id, full_name, first_name, last_name,
                    nickname, formatted_nickname, formatted_last_name,
                    club, guernsey, position, club_profile_url, image_url,
                    club_player_id, afl_url, champion_data_id,
                    source, scraped_at, resolved_at, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(afl_id) DO UPDATE SET
                    full_name = excluded.full_name,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    nickname = excluded.nickname,
                    formatted_nickname = excluded.formatted_nickname,
                    formatted_last_name = excluded.formatted_last_name,
                    club = excluded.club,
                    guernsey = excluded.guernsey,
                    position = excluded.position,
                    club_profile_url = excluded.club_profile_url,
                    image_url = excluded.image_url,
                    club_player_id = excluded.club_player_id,
                    afl_url = excluded.afl_url,
                    champion_data_id = excluded.champion_data_id,
                    source = excluded.source,
                    scraped_at = excluded.scraped_at,
                    resolved_at = excluded.resolved_at,
                    last_updated = excluded.last_updated
            """, (
                player.get("afl_id"),
                player.get("full_name"),
                player.get("first_name"),
                player.get("last_name"),
                player.get("nickname"),
                player.get("formatted_nickname"),
                player.get("formatted_last_name"),
                resolve_club_code_from_name(player.get("club")),
                player.get("guernsey"),
                player.get("position"),
                player.get("club_profile_url"),
                player.get("image_url"),
                player.get("club_id") or player.get("club_player_id"),
                player.get("afl_url"),
                player.get("champion_data_id"),
                player.get("source"),
                player.get("scraped_at"),
                player.get("resolved_at"),
                now
            ))

        total_imported += len(players)
        log(f"📥 Imported {len(players)} players from {file.name}", "INFO")

    conn.commit()
    conn.close()
    log(f"✅ Total players processed: {total_imported}", "SUCCESS")

def save_injuries_to_db(data: dict, conn: sqlite3.Connection):
    """
    Saves the scraped injury data to the 'injuries' table in the database.
    Expects data as { "ADE": { "updated": "Date", "players": [ ... ] }, ... }
    """
    cur = conn.cursor()

    # Create the table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS injuries (
            afl_id INTEGER NOT NULL,
            club TEXT NOT NULL,
            player_name TEXT NOT NULL,
            injury TEXT,
            return_info TEXT,
            updated TEXT,
            first_updated TEXT,
            source TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
            current INTEGER DEFAULT 1,
            UNIQUE(afl_id, updated)
        )
    """)

    # Track all currently listed injuries
    currently_listed_ids = set()

    for team in data["teams"]:
        club = team["club"]
        updated = team.get("updated", "")
        for player in team["players"]:
            if not player["afl_id"]:
                raise ValueError(f"Missing AFL ID for player {player['name']} from {club}")

            afl_id = player["afl_id"]
            currently_listed_ids.add(afl_id)

            cur.execute("""
                INSERT INTO injuries (
                    afl_id, club, player_name, injury, return_info, updated, first_updated, source, scraped_at, current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(afl_id, updated) DO UPDATE SET
                    club = excluded.club,
                    player_name = excluded.player_name,
                    injury = excluded.injury,
                    return_info = excluded.return_info,
                    source = excluded.source,
                    scraped_at = excluded.scraped_at,
                    current = 1
            """, (
                player["afl_id"],
                club,
                player["name"],
                player["injury"],
                player["return"],
                updated,
                updated,  # first_updated = same as updated if new
                data["source"],
                data["scraped_at"],
            ))

    # Mark previous entries as no longer current
    if currently_listed_ids:
        placeholders = ",".join("?" for _ in currently_listed_ids)
        cur.execute(f"""
            UPDATE injuries
            SET current = 0
            WHERE current = 1 AND afl_id NOT IN ({placeholders})
        """, tuple(currently_listed_ids))

    conn.commit()
    log(f"💾 Injury data saved for {len(data['teams'])} teams", "INFO")

def save_lineups_to_db(players: list[dict], conn: sqlite3.Connection, round_number: int):
    """
    Saves scraped lineup data to the 'lineups' table in the database.
    Expects a flat list of player dicts, each with match_id, afl_id, etc.
    """
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    # Create table if it doesn't exist
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lineups (
            round_number INTEGER NOT NULL,
            match_id TEXT NOT NULL,
            afl_id INTEGER NOT NULL,
            first_name TEXT,
            surname TEXT,
            team TEXT,
            position_group TEXT,
            champion_id TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (match_id, afl_id)
        )
    """)

    inserted = 0
    for player in players:
        try:
            cur.execute("""
                INSERT INTO lineups (
                    round_number, match_id, afl_id, first_name, surname,
                    team, position_group, champion_id, scraped_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(match_id, afl_id) DO UPDATE SET
                    round_number = excluded.round_number,
                    first_name = excluded.first_name,
                    surname = excluded.surname,
                    team = excluded.team,
                    position_group = excluded.position_group,
                    champion_id = excluded.champion_id,
                    scraped_at = excluded.scraped_at
            """, (
                round_number,
                player["match_id"],
                player["afl_id"],
                player.get("first_name"),
                player.get("surname"),
                player.get("team"),
                player.get("position_group"),
                player.get("champion_id"),
                now
            ))
            inserted += 1
        except Exception as e:
            log(f"❌ Failed to insert player {player['first_name']} {player['surname']}: {e}", "ERROR")

    conn.commit()
    log(f"💾 Saved {inserted} player lineups to DB", "SUCCESS")

def save_rounds_to_db(rounds: list[dict], metadata: dict, conn: sqlite3.Connection):
    """
    Stores the list of available rounds in the `rounds` table.
    Each round should include: round_id, round_label
    Metadata should include: season_id, competition_id
    """
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            round_id INTEGER PRIMARY KEY,
            round_label TEXT,
            season_id INTEGER,
            competition_id INTEGER,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    inserted = 0

    for round_info in rounds:
        cur.execute("""
            INSERT INTO rounds (
                round_id, round_label, season_id, competition_id, scraped_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(round_id) DO UPDATE SET
                round_label = excluded.round_label,
                season_id = excluded.season_id,
                competition_id = excluded.competition_id,
                scraped_at = excluded.scraped_at
        """, (
            round_info["round_id"],
            round_info["round_label"],
            metadata.get("season_id"),
            metadata.get("competition_id"),
            now
        ))
        inserted += 1

    conn.commit()
    log(f"💾 Saved {inserted} rounds to DB", "SUCCESS")

def save_matches_to_db(matches: list[dict], conn: sqlite3.Connection):
    """
    Saves scraped match data to the 'matches' table in the database.
    Each match dict should include: match_id, match_provider_id, round_id, home_team, away_team,
    venue, status, match_date_label, start_time_text (optional), score_home, score_away.
    """
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id INTEGER PRIMARY KEY,
            match_provider_id TEXT,
            round_id INTEGER NOT NULL,
            home_team TEXT,
            away_team TEXT,
            venue TEXT,
            status TEXT,
            start_time_utc TEXT,
            score_home INTEGER,
            score_away INTEGER,
            match_time_label TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    inserted = 0

    for match in matches:
        cur.execute("""
            INSERT INTO matches (
                match_id, match_provider_id, round_id, home_team, away_team, venue, status,
                start_time_utc, score_home, score_away, match_time_label, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                match_provider_id = excluded.match_provider_id,
                round_id = excluded.round_id,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                venue = excluded.venue,
                status = excluded.status,
                start_time_utc = excluded.start_time_utc,
                score_home = excluded.score_home,
                score_away = excluded.score_away,
                match_time_label = excluded.match_time_label,
                scraped_at = excluded.scraped_at
        """, (
            match["match_id"],
            match.get("match_provider_id"),
            match["round_id"],
            match["home_team"],
            match["away_team"],
            match["venue"],
            match["status"],
            match.get("start_time_utc"),
            match.get("score_home"),
            match.get("score_away"),
            match.get("match_time_label"),
            now
        ))
        inserted += 1

    conn.commit()
    log(f"💾 Saved {inserted} matches to DB", "SUCCESS")


def save_player_stats_to_db(stats: list[dict], conn: sqlite3.Connection):
    """Insert or update player stats records in the database."""
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            round_id INTEGER,
            afl_id INTEGER,
            champion_id TEXT,
            player_name TEXT NOT NULL,
            jumper_number INTEGER,
            team_code TEXT NOT NULL,
            af_score INTEGER,
            goals INTEGER,
            behinds INTEGER,
            disposals INTEGER,
            kicks INTEGER,
            handballs INTEGER,
            marks INTEGER,
            tackles INTEGER,
            hitouts INTEGER,
            clearances INTEGER,
            metres_gained INTEGER,
            goal_assists INTEGER,
            time_on_ground_pct REAL,
            status TEXT CHECK(status IN ('LIVE', 'COMPLETED')) NOT NULL,
            scraped_at TEXT NOT NULL,
            UNIQUE(match_id, afl_id)
        )
    """)

    for stat in stats:
        cur.execute("""
            INSERT INTO player_stats (
                match_id, round_id, afl_id, champion_id, player_name, jumper_number, team_code,
                af_score, goals, behinds, disposals, kicks, handballs, marks, tackles,
                hitouts, clearances, metres_gained, goal_assists, time_on_ground_pct,
                status, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, afl_id) DO UPDATE SET
                champion_id = excluded.champion_id,
                player_name = excluded.player_name,
                jumper_number = excluded.jumper_number,
                team_code = excluded.team_code,
                af_score = excluded.af_score,
                goals = excluded.goals,
                behinds = excluded.behinds,
                disposals = excluded.disposals,
                kicks = excluded.kicks,
                handballs = excluded.handballs,
                marks = excluded.marks,
                tackles = excluded.tackles,
                hitouts = excluded.hitouts,
                clearances = excluded.clearances,
                metres_gained = excluded.metres_gained,
                goal_assists = excluded.goal_assists,
                time_on_ground_pct = excluded.time_on_ground_pct,
                status = excluded.status,
                scraped_at = excluded.scraped_at
        """, (
            stat["match_id"],
            stat.get("round_id"),
            stat.get("afl_id"),
            stat.get("champion_id"),
            stat["player_name"],
            stat.get("jumper_number"),
            stat["team_code"],
            stat.get("af_score"),
            stat.get("goals"),
            stat.get("behinds"),
            stat.get("disposals"),
            stat.get("kicks"),
            stat.get("handballs"),
            stat.get("marks"),
            stat.get("tackles"),
            stat.get("hitouts"),
            stat.get("clearances"),
            stat.get("metres_gained"),
            stat.get("goal_assists"),
            stat.get("time_on_ground_pct"),
            stat["status"],
            now
        ))

    conn.commit()
    log(f"💾 Upserted {len(stats)} player stat rows to DB", "SUCCESS")

def log_scrape_event(conn, match_id: int, round_id: int | None, status: str):

    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            round_id INTEGER,
            status TEXT,
            scraped_at TEXT NOT NULL,
            UNIQUE(match_id, scraped_at)
        )
    """)

    cur.execute("""
        INSERT INTO scrape_log (match_id, round_id, status, scraped_at)
        VALUES (?, ?, ?, ?)
    """, (match_id, round_id, status, now))
    conn.commit()
    log(f"✅ Scraped information stored into DB", "SUCCESS")

def update_scrape_summary(conn: sqlite3.Connection, match_id: int):
    """
    Aggregates scrape_log data and stores summary for the match.
    """
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS scrape_summary (
            match_id INTEGER PRIMARY KEY,
            round_id INTEGER,
            total_scrapes INTEGER,
            first_scraped TEXT,
            last_scraped TEXT,
            completed_scrape BOOLEAN DEFAULT 0,
            notes TEXT
        )
    """)
    
    cur.execute("""
        SELECT round_id, COUNT(*), MIN(scraped_at), MAX(scraped_at),
               MAX(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END)
        FROM scrape_log
        WHERE match_id = ?
    """, (match_id,))
    
    row = cur.fetchone()
    if not row or row[1] == 0:
        log(f"⚠️ No scrape logs found for match {match_id}", "WARN")
        return

    round_id, count, first, last, completed = row

    cur.execute("""
        INSERT INTO scrape_summary (
            match_id, round_id, total_scrapes, first_scraped, last_scraped, completed_scrape
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            round_id = excluded.round_id,
            total_scrapes = excluded.total_scrapes,
            first_scraped = excluded.first_scraped,
            last_scraped = excluded.last_scraped,
            completed_scrape = excluded.completed_scrape
    """, (match_id, round_id, count, first, last, completed))
    
    conn.commit()
    log(f"✅ Summary updated for match {match_id} ({count} scrapes)", "SUCCESS")

if __name__ == "__main__":
    import_players()
