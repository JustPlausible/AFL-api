import json
from copy import deepcopy
from pathlib import Path

import pytest

from afl_json import (
    AflJsonAuthenticationError,
    AflJsonClient,
    AflJsonInvalidResponse,
    AflJsonResourceUnavailable,
    AflJsonResponse,
    HttpPolicy,
    MatchRosterCollector,
    RosterStatus,
    compare_rosters,
)

FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FixtureClient:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get(self, endpoint, *, path_parameters=None, params=None):
        self.calls.append((endpoint, path_parameters, params))
        if self.error:
            raise self.error
        return AflJsonResponse(endpoint, 200, self.payload, {})


def collect(name):
    return MatchRosterCollector(FixtureClient(fixture(name))).collect("CD_R2026_18")


def test_verified_top_level_list_and_home_away_rosters_are_normalised():
    result = collect("match_rosters_available.json")

    assert result.status is RosterStatus.PUBLISHED
    assert result.round_provider_id == "CD_R2026_18"
    assert result.publication_state == "PUBLISHED"
    assert result.provider_timestamp == "2026-07-25T08:30:00Z"
    assert result.rosters[0]["round_number"] == 18
    assert result.rosters[0]["competition_provider_id"] == "CD_C014"
    assert (result.rosters[0]["match_provider_id"], result.rosters[0]["afl_match_id"]) == (
        "CD_M100", 100
    )
    assert [(team["team_provider_id"], team["side"]) for team in result.rosters[0]["teams"]] == [
        ("CD_T1", "home"), ("CD_T2", "away")
    ]
    assert result.rosters[0]["provider_fields"]["venue"]["providerId"] == "CD_V1"
    assert result.rosters[0]["provider_fields"]["teamPlayers"][0]["teamId"] == "CD_T1"
    # Live current-round payloads use an empty object for this optional field.
    assert result.rosters[0]["teams"][0]["provider_fields"]["lateChanges"] == {}


def test_optional_team_fields_only_parse_lists_and_preserve_unresolved_shapes():
    source = fixture("match_rosters_available.json")
    optional_fields = ("positions", "ins", "outs", "lateChanges", "clubDebuts", "milestones")

    for field in optional_fields:
        object_payload = deepcopy(source)
        object_payload[0]["matchRoster"]["homeTeam"][field] = {"unresolved": field}
        result = MatchRosterCollector(FixtureClient(object_payload)).collect("CD_R1")
        assert result.rosters[0]["teams"][0]["provider_fields"][field] == {
            "unresolved": field
        }

        null_payload = deepcopy(source)
        null_payload[0]["matchRoster"]["homeTeam"][field] = None
        result = MatchRosterCollector(FixtureClient(null_payload)).collect("CD_R1")
        assert field not in result.rosters[0]["teams"][0]["provider_fields"]


def test_positions_and_nested_change_players_preserve_verified_meaning():
    result = collect("match_rosters_available.json")
    ada_position = next(item for item in result.selections
                        if item["champion_data_player_id"] == "CD_I1"
                        and item["record_kind"] == "selection")
    assert (ada_position["player_name"], ada_position["jumper_number"],
            ada_position["captain"]) == ("Ada Able", 7, True)
    assert (ada_position["source_collection"], ada_position["selection_state"]) == (
        "positions", "FORWARDS"
    )
    assert ada_position["provider_fields"] == {"selectionNote": "verified nested wrapper"}

    incoming = next(item for item in result.selections if item["source_collection"] == "ins")
    outgoing = next(item for item in result.selections if item["source_collection"] == "outs")
    assert (incoming["champion_data_player_id"], incoming["reason"]) == ("CD_I1", "Selected")
    assert (outgoing["champion_data_player_id"], outgoing["reason"]) == ("CD_I4", "Managed")
    assert incoming["record_kind"] == outgoing["record_kind"] == "change"
    assert all(item["record_kind"] == "selection" for item in result.selections
               if item["source_collection"] == "positions")


def test_completed_and_current_verified_shapes_are_deterministic():
    current_one = collect("match_rosters_available.json")
    current_two = collect("match_rosters_available.json")
    completed_one = collect("match_rosters_completed.json")
    completed_two = collect("match_rosters_completed.json")

    assert current_one == current_two
    assert completed_one == completed_two
    assert completed_one.status is RosterStatus.PUBLISHED
    assert completed_one.publication_state == "CONCLUDED"
    assert {item["source_collection"] for item in completed_one.selections} >= {
        "positions", "milestones"
    }


def test_null_future_round_is_unavailable_non_authentication_and_non_destructive():
    published = collect("match_rosters_available.json")
    future = collect("match_rosters_unpublished.json")

    assert future.status is RosterStatus.UNAVAILABLE
    assert future.selections == []
    diff = compare_rosters(published, future)
    assert diff.replacement_safe is False
    assert diff.removed == []


def test_empty_list_is_empty_but_conservatively_not_replacement_safe():
    published = collect("match_rosters_available.json")
    empty = MatchRosterCollector(FixtureClient([])).collect("CD_R1")

    assert empty.status is RosterStatus.EMPTY
    assert compare_rosters(published, empty).replacement_safe is False
    assert compare_rosters(published, empty).removed == []


def test_changed_verified_roster_detects_moves_additions_removals_and_change_records():
    initial = collect("match_rosters_available.json")
    repeated = collect("match_rosters_available.json")
    changed = collect("match_rosters_changed.json")

    same = compare_rosters(initial, repeated)
    assert not same.added and not same.removed and not same.changed
    assert len(same.unchanged) == len(initial.selections)
    diff = compare_rosters(initial, changed)
    assert any(item["after"]["champion_data_player_id"] == "CD_I1"
               and item["after"]["selection_state"] == "INTERCHANGE" for item in diff.changed)
    assert any(item["champion_data_player_id"] == "CD_I6"
               and item["record_kind"] == "selection" for item in diff.added)
    assert any(item["champion_data_player_id"] == "CD_I2"
               and item["record_kind"] == "selection" for item in diff.removed)
    assert any(item["source_collection"] == "lateChanges" for item in diff.added)
    assert len({_identity(item) for item in changed.selections}) == len(changed.selections)


def test_position_and_player_array_reordering_is_unchanged_with_stable_ids():
    initial_payload = fixture("match_rosters_available.json")
    positions = initial_payload[0]["matchRoster"]["homeTeam"]["positions"]
    # Put two players in one verified positional group so both group and player
    # ordering can change without changing selection membership or state.
    positions[0]["players"].extend(positions[1]["players"])
    positions[1]["players"] = []
    initial_payload[0]["matchRoster"]["status"] = "UNCONFIRMED_TEAMS"
    reordered_payload = deepcopy(initial_payload)
    reordered_payload[0]["matchRoster"]["status"] = "LIVE"
    reordered_payload[0]["matchRoster"]["lastUpdated"] = "2026-07-25T09:03:00Z"
    reordered = reordered_payload[0]["matchRoster"]["homeTeam"]["positions"]
    reordered.reverse()
    for group in reordered:
        group["players"].reverse()

    initial = MatchRosterCollector(FixtureClient(initial_payload)).collect("CD_R1")
    later = MatchRosterCollector(FixtureClient(reordered_payload)).collect("CD_R1")
    diff = compare_rosters(initial, later)

    assert diff.added == []
    assert diff.removed == []
    assert diff.changed == []
    assert len(diff.unchanged) == len(initial.selections)
    assert {item["champion_data_player_id"] for item in diff.unchanged} >= {"CD_I1", "CD_I2"}
    assert initial.provider_timestamp != later.provider_timestamp


def _identity(item):
    return (item["match_provider_id"], item["team_provider_id"],
            item["champion_data_player_id"], item["record_kind"],
            item["source_collection"] if item["record_kind"] == "change" else None)


def test_malformed_nested_response_raises_safe_validation_error():
    with pytest.raises(AflJsonInvalidResponse, match="homeTeam is not an object"):
        collect("match_rosters_malformed.json")
    for value in (17, "wrong", {"matchRoster": {}}):
        with pytest.raises(AflJsonInvalidResponse, match="not a list or null"):
            MatchRosterCollector(FixtureClient(value)).collect("CD_R1")


def test_genuine_http_401_remains_an_authentication_failure():
    class Response:
        headers = {}
        def __init__(self, status, payload): self.status_code, self.payload = status, payload
        def json(self): return self.payload
    class Session:
        def __init__(self):
            self.responses = [Response(200, {"token": "first"}),
                              Response(401, {"code": "CFSAPI001"}),
                              Response(200, {"token": "second"}),
                              Response(401, {"code": "CFSAPI001"})]
        def request(self, *_args, **_kwargs): return self.responses.pop(0)
        def close(self): pass
    client = AflJsonClient(session=Session(), policy=HttpPolicy(max_attempts=1))

    with pytest.raises(AflJsonAuthenticationError):
        MatchRosterCollector(client).collect("CD_R1")


def test_provider_404_is_unavailable_and_raw_capture_preserves_list_and_null(tmp_path):
    unavailable = MatchRosterCollector(FixtureClient(error=AflJsonResourceUnavailable(
        "not published", endpoint="match_rosters", status_code=404, error_code="CFSSDS001"
    ))).collect("CD_R1")
    assert unavailable.status is RosterStatus.UNAVAILABLE

    MatchRosterCollector(FixtureClient(fixture("match_rosters_available.json")),
                         raw_directory=tmp_path).collect("CD_R1")
    MatchRosterCollector(FixtureClient(None), raw_directory=tmp_path).collect("CD_R2")
    available_path = tmp_path / "match_rosters/match_rosters__roundProviderId-CD_R1__page-0001.json"
    null_path = tmp_path / "match_rosters/match_rosters__roundProviderId-CD_R2__page-0001.json"
    assert isinstance(json.loads(available_path.read_text()), list)
    assert null_path.read_text() == "null\n"
    assert "x-media-mis-token" not in available_path.read_text()
