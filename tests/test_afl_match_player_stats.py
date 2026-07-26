import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from afl_json.client import AflJsonHttpError, AflJsonInvalidResponse, AflJsonResourceUnavailable
from afl_json.player_stats import (
    CanonicalPlayerStat, MatchPlayerStatsCollector, PlayerStatsStatus,
    normalise_player_stats, resolve_authoritative_match_status,
    resolve_canonical_match_status, upsert_player_stats,
)
FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"


class FixtureClient:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.payload)


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def collect(name, at="2026-07-25T10:00:00+00:00"):
    client = FixtureClient(fixture(name))
    result = MatchPlayerStatsCollector(
        client, clock=lambda: datetime.fromisoformat(at)
    ).collect("CD_M1", afl_match_id=101)
    assert client.calls == [("match_player_statistics", {
        "path_parameters": {"match_provider_id": "CD_M1"}})]
    return result


def test_unpublished_null_and_provider_404_are_not_authentication_errors():
    result = collect("match_player_stats_unpublished.json")
    assert result.status is PlayerStatsStatus.UNAVAILABLE
    assert result.records == []
    error = AflJsonResourceUnavailable("not published", endpoint="match_player_statistics")
    assert MatchPlayerStatsCollector(FixtureClient(error=error)).collect("CD_M1").status is PlayerStatsStatus.UNAVAILABLE


def test_live_partial_preserves_null_zero_decimal_unknown_and_metadata():
    result = collect("match_player_stats_live_partial.json")
    assert result.status is PlayerStatsStatus.LIVE_PARTIAL
    assert len(result.records) == 1
    record = result.records[0]
    assert isinstance(record, CanonicalPlayerStat)
    assert record.natural_key == ("CD_M1", "CD_I1")
    assert record.side == "home" and record.team_provider_id == "CD_T1"
    assert record.goals == 0 and record.behinds == 1 and record.kicks == Decimal("2.5")
    assert record.disposals is None and record.hitouts is None
    assert record.extra_stats == {"unknownMetric": {"value": 9}}
    assert record.collected_at == result.collected_at
    assert record.source_endpoint.endswith("/{matchProviderId}")
    assert {item.code for item in result.diagnostics} == {"null_team_array"}


def test_one_missing_team_array_is_partial_and_warned():
    payload = fixture("match_player_stats_live_partial.json")
    del payload["awayTeamPlayerStats"]
    result = normalise_player_stats(payload, "CD_M1", collected_at="2026-01-01T00:00:00+00:00")
    assert result.status is PlayerStatsStatus.LIVE_PARTIAL
    assert "missing_team_array" in {item.code for item in result.diagnostics}


def test_concluded_maps_both_arrays_and_all_eight_fields():
    result = collect("match_player_stats_concluded.json")
    assert result.status is PlayerStatsStatus.CONCLUDED
    assert {record.side for record in result.records} == {"home", "away"}
    assert len({record.natural_key for record in result.records}) == 2
    home = result.records[0]
    assert (home.goals, home.behinds, home.kicks, home.handballs, home.disposals,
            home.marks, home.tackles, home.hitouts) == (2, 1, 10, 8, 18, 4, 3, 0)
    assert home.extra_stats == {"ratingPoints": 7.25}


def test_forward_endpoint_status_is_retained_and_advances_conflicting_metadata():
    payload = fixture("match_player_stats_concluded.json")
    result = normalise_player_stats(
        payload, "CD_M1", collected_at="now", canonical_match_status="LIVE"
    )
    assert result.status is PlayerStatsStatus.CONCLUDED
    assert result.endpoint_source_status == "CONCLUDED"
    assert result.resolved_match_status == "LIVE"
    assert result.source_status == "CONCLUDED"
    assert "conflicting_match_status" in {item.code for item in result.diagnostics}
    assert all(record.endpoint_source_status == "CONCLUDED" for record in result.records)
    assert all(record.resolved_match_status == "LIVE" for record in result.records)


def test_stale_endpoint_status_cannot_downgrade_reconciled_concluded_metadata():
    payload = fixture("match_player_stats_concluded.json")
    payload["status"] = "LIVE"
    result = normalise_player_stats(
        payload, "CD_M1", collected_at="now", canonical_match_status="CONCLUDED"
    )
    assert result.endpoint_source_status == "LIVE"
    assert result.resolved_match_status == "CONCLUDED"
    assert result.status is PlayerStatsStatus.CONCLUDED
    assert "conflicting_match_status" in {item.code for item in result.diagnostics}


@pytest.mark.parametrize("metadata_status, expected", [
    ("CONCLUDED", PlayerStatsStatus.CONCLUDED),
    ("COMPLETED", PlayerStatsStatus.CONCLUDED),
    ("LIVE", PlayerStatsStatus.LIVE_PARTIAL),
    (None, PlayerStatsStatus.UNKNOWN),
])
def test_canonical_match_metadata_fallback_when_endpoint_status_is_absent(metadata_status, expected):
    payload = fixture("match_player_stats_concluded.json")
    del payload["status"]
    result = normalise_player_stats(
        payload, "CD_M1", collected_at="now", canonical_match_status=metadata_status
    )
    assert result.status is expected
    assert result.endpoint_source_status is None
    assert result.resolved_match_status == metadata_status
    assert all(record.endpoint_source_status is None for record in result.records)


def test_match_status_resolves_from_canonical_database_by_provider_or_afl_id():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE matches (match_id INTEGER, match_provider_id TEXT, status TEXT)")
    conn.execute("INSERT INTO matches VALUES (101, 'CD_M1', 'CONCLUDED')")
    assert resolve_canonical_match_status(conn, match_provider_id="CD_M1") == "CONCLUDED"
    assert resolve_canonical_match_status(conn, afl_match_id=101) == "CONCLUDED"
    assert resolve_canonical_match_status(conn, match_provider_id="CD_MISSING") is None


class DetailClient:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(data=self.payload)


def _match_db(status):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE matches (match_id INTEGER, match_provider_id TEXT, status TEXT)")
    conn.execute("INSERT INTO matches VALUES (8207, 'CD_M20260142007', ?)", (status,))
    return conn


def test_stored_postgame_is_refreshed_from_direct_concluded_detail():
    conn = _match_db("POSTGAME")
    client = DetailClient(fixture("match_detail_concluded.json"))
    resolved = resolve_authoritative_match_status(
        conn, client, match_provider_id="CD_M20260142007"
    )
    assert resolved.status == resolved.direct_status == "CONCLUDED"
    assert resolved.stored_status == "POSTGAME"
    assert resolved.resolution == "public_match_detail"
    assert resolved.canonical_refreshed is True
    assert conn.execute("SELECT status FROM matches").fetchone() == ("CONCLUDED",)


def test_direct_reconciliation_updates_updated_at_but_not_bulk_scraped_at():
    conn = _match_db("POSTGAME")
    conn.execute("ALTER TABLE matches ADD COLUMN scraped_at TEXT")
    conn.execute("ALTER TABLE matches ADD COLUMN updated_at TEXT")
    conn.execute("UPDATE matches SET scraped_at='bulk-time', updated_at='old-update'")
    resolve_authoritative_match_status(
        conn, DetailClient(fixture("match_detail_concluded.json")),
        match_provider_id="CD_M20260142007",
    )
    scraped_at, updated_at = conn.execute(
        "SELECT scraped_at, updated_at FROM matches"
    ).fetchone()
    assert scraped_at == "bulk-time"
    assert updated_at != "old-update"


def test_stale_list_live_is_superseded_by_direct_concluded_detail():
    stale = fixture("matches_round_stale_live.json")["matches"][0]
    conn = _match_db(stale["status"])
    resolved = resolve_authoritative_match_status(
        conn, DetailClient(fixture("match_detail_concluded.json")),
        match_provider_id=stale["providerId"], afl_match_id=stale["id"],
    )
    assert (resolved.stored_status, resolved.direct_status, resolved.status) == (
        "LIVE", "CONCLUDED", "CONCLUDED"
    )


def test_stored_concluded_avoids_network_and_cannot_be_downgraded():
    conn = _match_db("CONCLUDED")
    client = DetailClient({"matches": [{"id": 8207, "providerId": "CD_M20260142007",
                                         "status": "LIVE"}]})
    resolved = resolve_authoritative_match_status(
        conn, client, match_provider_id="CD_M20260142007"
    )
    assert resolved.status == "CONCLUDED"
    assert resolved.direct_status is None
    assert client.calls == []


def test_direct_detail_unavailable_falls_back_to_canonical_database():
    conn = _match_db("POSTGAME")
    error = AflJsonHttpError("not found", endpoint="match_detail", status_code=404)
    resolved = resolve_authoritative_match_status(
        conn, DetailClient(error=error), match_provider_id="CD_M20260142007"
    )
    assert resolved.status == "POSTGAME"
    assert resolved.resolution == "canonical_match_database"
    assert resolved.canonical_refreshed is False
    assert "unavailable" in resolved.diagnostic


@pytest.mark.parametrize("match_status, expected", [
    ("SCHEDULED", PlayerStatsStatus.SCHEDULED),
    ("LIVE", PlayerStatsStatus.LIVE_PARTIAL),
    ("POSTGAME", PlayerStatsStatus.POSTGAME),
    ("CONCLUDED", PlayerStatsStatus.CONCLUDED),
])
def test_known_match_lifecycle_has_explicit_publication_classification(match_status, expected):
    payload = fixture("match_player_stats_concluded.json")
    del payload["status"]
    result = normalise_player_stats(
        payload, "CD_M1", collected_at="now", canonical_match_status=match_status
    )
    assert result.status is expected


def test_malformed_entries_are_rejected_or_diagnosed_without_losing_good_record():
    result = collect("match_player_stats_malformed.json")
    codes = [item.code for item in result.diagnostics]
    assert codes.count("invalid_numeric") == 2
    assert "missing_player_id" in codes
    assert "duplicate_player_id" in codes
    assert "player_on_both_sides" in codes
    assert "malformed_player" in codes
    bad = next(record for record in result.records if record.champion_data_player_id == "CD_BAD")
    assert bad.goals is None and bad.behinds is None
    assert bad.extra_stats["futureStat"] == "retained"
    assert result.rejected_records == 4


@pytest.mark.parametrize("payload", [[], "wrong", 17, {}, {"mystery": []}])
def test_unknown_top_level_shapes_fail_clearly(payload):
    with pytest.raises(AflJsonInvalidResponse):
        normalise_player_stats(payload, "CD_M1", collected_at="now")


def _schema(conn):
    # Import numeric migration modules through the runner's supported loader.
    from db.migration_runner import discover_migrations
    migration = next(item for item in discover_migrations() if item.identifier == "0006")
    migration.module.migrate(conn)


def test_final_supersedes_live_is_idempotent_and_cannot_be_downgraded():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    live = collect("match_player_stats_live_partial.json", "2026-07-25T10:00:00+00:00")
    final = collect("match_player_stats_concluded.json", "2026-07-25T12:00:00+00:00")
    stale_live = collect("match_player_stats_live_partial.json", "2026-07-25T13:00:00+00:00")
    assert upsert_player_stats(conn, live) == 1
    assert upsert_player_stats(conn, final) == 2
    assert upsert_player_stats(conn, final) == 0
    assert upsert_player_stats(conn, stale_live) == 0
    row = conn.execute("SELECT goals, endpoint_source_status, snapshot_authority FROM cfs_player_stats WHERE champion_data_player_id='CD_I1'").fetchone()
    assert row == (2, "CONCLUDED", 2)


def test_metadata_conclusion_receives_final_snapshot_authority():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    payload = fixture("match_player_stats_concluded.json")
    del payload["status"]
    result = normalise_player_stats(
        payload, "CD_M1", collected_at="2026-07-25T12:00:00+00:00",
        canonical_match_status="CONCLUDED",
    )
    assert result.status is PlayerStatsStatus.CONCLUDED
    assert upsert_player_stats(conn, result) == 2
    assert conn.execute("SELECT DISTINCT snapshot_authority FROM cfs_player_stats").fetchall() == [(2,)]


def test_46_records_write_as_concluded_after_detail_resolution():
    conn = sqlite3.connect(":memory:")
    _schema(conn)
    conn.execute("CREATE TABLE matches (match_id INTEGER, match_provider_id TEXT, status TEXT)")
    conn.execute("INSERT INTO matches VALUES (8207, 'CD_M20260142007', 'POSTGAME')")
    resolved = resolve_authoritative_match_status(
        conn, DetailClient(fixture("match_detail_concluded.json")),
        match_provider_id="CD_M20260142007",
    )
    template = fixture("match_player_stats_concluded.json")["homeTeamPlayerStats"][0]
    players = []
    for number in range(46):
        entry = deepcopy(template)
        entry["player"]["player"]["player"]["player"]["playerId"] = f"CD_I{number:03d}"
        players.append(entry)
    result = normalise_player_stats(
        {"homeTeamPlayerStats": players, "awayTeamPlayerStats": []},
        "CD_M20260142007", collected_at="2026-07-26T12:00:00+00:00",
        afl_match_id=8207, canonical_match_status=resolved.status,
    )
    assert result.status is PlayerStatsStatus.CONCLUDED
    assert len(result.records) == 46
    assert upsert_player_stats(conn, result) == 46
    assert conn.execute(
        "SELECT COUNT(*), MIN(snapshot_authority), MAX(snapshot_authority) FROM cfs_player_stats"
    ).fetchone() == (46, 2, 2)


def test_raw_capture_is_sanitised_payload_only(tmp_path):
    MatchPlayerStatsCollector(FixtureClient(fixture("match_player_stats_concluded.json")),
                              raw_directory=tmp_path).collect("CD_M1")
    path = tmp_path / "match_player_statistics/match_player_statistics__matchProviderId-CD_M1__page-0001.json"
    assert json.loads(path.read_text()) == fixture("match_player_stats_concluded.json")
    assert "token" not in path.read_text().casefold()
