"""Offline contract regression suite for the maintained AFL JSON corpus."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from afl_json.client import (
    AflJsonAuthenticationError,
    AflJsonClient,
    AflJsonResourceUnavailable,
    HttpPolicy,
    WMCTokenProvider,
)
from afl_json.collectors import CollectionError, PublicAflCollector
from afl_json.contracts import ENDPOINTS
from afl_json.player_stats import PlayerStatsStatus, normalise_player_stats
from afl_json.rosters import MatchRosterCollector, RosterStatus

FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"
MANIFEST = FIXTURES / "manifest.json"
DATA_ENDPOINTS = set(ENDPOINTS) - {"wmc_token"}


def load_fixture(path):
    return json.loads((FIXTURES / path).read_text())


class FixtureClient:
    def __init__(self, responses):
        self.responses = list(responses) if isinstance(responses, list) else [responses]
        self.calls = []

    def get(self, endpoint, **kwargs):
        kwargs = {**kwargs, "params": dict(kwargs.get("params", {}))} if "params" in kwargs else kwargs
        self.calls.append((getattr(endpoint, "name", endpoint), kwargs))
        return SimpleNamespace(data=self.responses.pop(0))


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Make an accidental network call a local, explanatory test failure."""
    def blocked(*_args, **_kwargs):
        raise AssertionError("AFL JSON fixture contracts must run without network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


def test_manifest_is_complete_well_formed_and_has_a_baseline_for_every_data_endpoint():
    manifest = load_fixture("manifest.json")
    assert manifest["schema_version"] == 1
    required = set(manifest["metadata_required"])
    represented = set()
    scenarios = {}
    for entry in manifest["fixtures"]:
        assert required <= set(entry), entry["id"]
        assert entry["capture_date"].count("-") == 2
        assert entry["source_url_pattern"].startswith("https://")
        assert entry["full"] is False
        assert entry["sanitisation"] and entry["reduction"]
        assert entry["path"] not in represented
        represented.add(entry["path"])
        scenarios.setdefault(entry["endpoint_family"], set()).add(entry["scenario"])
        assert (FIXTURES / entry["path"]).is_file()

    files = {path.relative_to(FIXTURES).as_posix() for path in FIXTURES.rglob("*.json")}
    assert represented == files - {"manifest.json"}
    inventory = {item["name"] for item in manifest["endpoint_families"]}
    assert inventory == DATA_ENDPOINTS
    assert all(any("normal" in state or state in {"concluded"} for state in scenarios[name])
               for name in DATA_ENDPOINTS)


def test_payload_corpus_contains_no_authentication_material_or_sensitive_headers():
    forbidden = ("x-media-mis-token", "wmctok", "authorization", "set-cookie", "cookie:",
                 "bearer ")
    for path in FIXTURES.rglob("*.json"):
        if path == MANIFEST:
            continue
        text = path.read_text().casefold()
        assert not any(value in text for value in forbidden), path


@pytest.mark.parametrize(("endpoint", "fixture", "expected_key", "expected"), [
    ("competitions", "competitions.json", "provider_id", "CD_C014"),
    ("competition_seasons", "seasons.json", "year", 2026),
    ("rounds", "rounds.json", "round_number", 0),
    ("teams", "teams.json", "provider_id", "CD_T10"),
    ("matches", "matches_round_1.json", "status", "CONCLUDED"),
])
def test_valid_public_fixtures_use_production_normalisation(endpoint, fixture, expected_key, expected):
    collector = PublicAflCollector(FixtureClient(load_fixture(fixture)))
    if endpoint == "competition_seasons": records = collector.competition_seasons(1)
    elif endpoint == "rounds": records = collector.rounds(85)
    elif endpoint == "teams": records = collector.teams(85)
    elif endpoint == "matches": records = collector.matches(1, 85, 1)
    else: records = collector.competitions()
    assert any(record[expected_key] == expected for record in records)


@pytest.mark.parametrize("endpoint", [
    "competitions", "competition_seasons", "rounds", "teams", "matches", "match_detail"
])
def test_empty_public_collections_are_supported(endpoint):
    client = FixtureClient(load_fixture(f"contracts/{endpoint}__empty.json"))
    assert PublicAflCollector(client).collect_endpoint(endpoint) == []


@pytest.mark.parametrize("endpoint", [
    "competitions", "competition_seasons", "rounds", "teams", "matches", "match_detail"
])
def test_missing_required_public_field_fails_at_the_record(endpoint):
    client = FixtureClient(load_fixture(f"contracts/{endpoint}__missing_required.json"))
    with pytest.raises(CollectionError, match=rf"{endpoint} record 0.*providerId"):
        PublicAflCollector(client).collect_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", [
    "competitions", "competition_seasons", "rounds", "teams", "matches", "match_detail"
])
def test_malformed_public_record_fails_locally(endpoint):
    client = FixtureClient(load_fixture(f"contracts/{endpoint}__malformed_record.json"))
    with pytest.raises(CollectionError, match=rf"{endpoint} collection contains a non-object record"):
        PublicAflCollector(client).collect_endpoint(endpoint)


def test_response_driven_pagination_replays_all_fixture_pages():
    client = FixtureClient([load_fixture("contracts/competitions__paginated_page_1.json"),
                            load_fixture("contracts/competitions__paginated_page_2.json")])
    records = PublicAflCollector(client, page_size=500).competitions()
    assert [record["provider_id"] for record in records] == ["CD_C999", "CD_C014"]
    assert [call[1]["params"]["pageNum"] for call in client.calls] == [1, 2]


def test_id_map_empty_malformed_and_renamed_collection_contracts():
    empty, diagnostics = PublicAflCollector(
        FixtureClient(load_fixture("contracts/player_id_map__empty.json"))).player_id_map()
    assert empty == {} and diagnostics == []
    mappings, diagnostics = PublicAflCollector(
        FixtureClient(load_fixture("contracts/player_id_map__malformed_record.json"))).player_id_map()
    assert mappings == {"CD_I100": 7001}
    assert [item.code for item in diagnostics] == ["malformed_player_id_map"]
    with pytest.raises(Exception, match="idMapResponse.ids"):
        PublicAflCollector(FixtureClient(
            load_fixture("contracts/player_id_map__missing_required.json"))).player_id_map()


def test_season_players_empty_optional_required_and_continuation_contracts():
    empty, diagnostics = PublicAflCollector(
        FixtureClient(load_fixture("contracts/season_players__empty.json"))).season_players("CD_S1")
    assert empty == [] and diagnostics == []
    optional, diagnostics = PublicAflCollector(FixtureClient(
        load_fixture("contracts/season_players__missing_optional.json"))).season_players("CD_S1")
    assert optional[0]["playerId"] == "CD_I100" and diagnostics == []
    malformed_payload = load_fixture("contracts/season_players__missing_required.json")
    malformed, diagnostics = PublicAflCollector(FixtureClient([
        malformed_payload, {"players": [], "totalResults": 1}
    ])).season_players("CD_S1")
    assert malformed == [] and [item.code for item in diagnostics] == ["malformed_season_player",
                                                                        "unreconciled_season_player_total"]
    pages = [load_fixture("contracts/season_players__paginated_probe.json"),
             load_fixture("contracts/season_players__paginated_page_1.json"),
             load_fixture("contracts/season_players__paginated_page_2.json")]
    records, diagnostics = PublicAflCollector(FixtureClient(pages), page_size=1).season_players("CD_S1")
    assert [record["playerId"] for record in records] == ["CD_I100", "CD_I200"]
    assert diagnostics == []


def test_roster_empty_malformed_and_unpublished_contracts():
    empty = load_fixture("contracts/match_rosters__empty.json")
    assert MatchRosterCollector(FixtureClient([empty])).collect("CD_R1").status is RosterStatus.EMPTY
    with pytest.raises(Exception, match="homeTeam"):
        MatchRosterCollector(FixtureClient([
            load_fixture("match_rosters_malformed.json")
        ])).collect("CD_R1")
    assert MatchRosterCollector(FixtureClient(None)).collect("CD_R1").status is RosterStatus.UNAVAILABLE


def test_live_partial_and_concluded_stats_are_distinct_and_preserve_final_fields():
    live = normalise_player_stats(load_fixture("match_player_stats_live_partial.json"), "CD_M1",
                                  collected_at="2026-07-28T00:00:00Z")
    final = normalise_player_stats(load_fixture("match_player_stats_concluded.json"), "CD_M1",
                                   collected_at="2026-07-28T01:00:00Z")
    assert live.status is PlayerStatsStatus.LIVE_PARTIAL
    assert live.records[0].disposals is None
    assert final.status is PlayerStatsStatus.CONCLUDED
    assert (final.records[0].goals, final.records[0].disposals, final.records[0].hitouts) == (2, 18, 0)


class Response:
    def __init__(self, status, payload): self.status_code, self.payload, self.headers = status, payload, {}
    def json(self): return self.payload


class ResponseSession:
    def __init__(self, responses): self.responses = list(responses)
    def request(self, *_args, **_kwargs): return self.responses.pop(0)
    def close(self): pass


def test_sanitised_401_and_cfssds001_fixtures_use_production_error_classification():
    auth = load_fixture("contracts/http_401_authentication_failure.json")
    client = AflJsonClient(session=ResponseSession([Response(401, auth), Response(401, auth)]),
                           policy=HttpPolicy(max_attempts=1),
                           token_provider=WMCTokenProvider(lambda: "test-only-non-fixture-token"))
    with pytest.raises(AflJsonAuthenticationError):
        client.get("season_players", params={"seasonId": "CD_S1"})

    unpublished = load_fixture("contracts/cfs_unpublished_resource.json")
    client = AflJsonClient(session=ResponseSession([Response(404, unpublished)]),
                           policy=HttpPolicy(max_attempts=1),
                           token_provider=WMCTokenProvider(lambda: "test-only-non-fixture-token"))
    with pytest.raises(AflJsonResourceUnavailable):
        client.get("match_player_statistics", path_parameters={"match_provider_id": "CD_M1"})
