import config

def get_fixture_url():
    return f"{config.AFL_BASE_URL}/fixture"

def get_fixture_url_for_round(round_id: int) -> str:
    return f"{config.AFL_BASE_URL}/fixture?Competition={config.AFL_COMPETITION_ID}&Season={config.AFL_SEASON_ID}&Round={round_id}"

def get_injuries_url():
    return f"{config.AFL_BASE_URL}/matches/injury-list"

def get_lineups_url():
    return f"{config.AFL_BASE_URL}/matches/team-lineups"

def get_stats_url():
    return f"{config.AFL_BASE_URL}/stats"

def get_players_url():
    return f"{config.AFL_BASE_URL}/players"