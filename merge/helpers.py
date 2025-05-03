import re
import json
from pathlib import Path
from utils.log import log
from enrich.afl_com import resolve_player

def extract_club_player_id(url: str) -> int:
    match = re.search(r"/players/(\d+)", url)
    return int(match.group(1)) if match else None

def resolve_players_for_club(club_name: str, skip_existing=False):
    """
    Resolves all players from a club's raw JSON file and writes enriched output.
    Expects:
      - data/players-<club>-raw.json
      - Writes to: data/players-<club>.json
    """
    raw_path = Path(f"data/players-{club_name}-raw.json")
    output_path = Path(f"data/players-{club_name}.json")

    if skip_existing and output_path.exists():
        log(f"Skipping {club_name.title()} (enriched file exists)", "DEBUG")
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

    log(f"\n✅ Enriched {len(enriched_players)} {club_name.title()} players saved to: {output_path}", "INFO")
