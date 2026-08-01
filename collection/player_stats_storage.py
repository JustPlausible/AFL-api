"""Named player-stat storage boundaries.

These names are intentionally not a table-selection heuristic.  New features
must choose the authoritative CFS model; the legacy name exists only for
explicit scraper and compatibility surfaces.
"""

AUTHORITATIVE_PLAYER_STATS_TABLE = "cfs_player_stats"
LEGACY_SCRAPER_PLAYER_STATS_TABLE = "player_stats"


def authoritative_player_stats_table() -> str:
    """Return the only player-stat table new application readers should use."""
    return AUTHORITATIVE_PLAYER_STATS_TABLE
