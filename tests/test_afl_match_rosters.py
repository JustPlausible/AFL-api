import json
from pathlib import Path

import pytest

from afl_json import (
    AflJsonInvalidResponse,
    AflJsonResourceUnavailable,
    AflJsonResponse,
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


def test_available_roster_preserves_ids_states_groups_order_and_narrow_unknown_fields():
    result = collect("match_rosters_available.json")

    assert result.status is RosterStatus.PUBLISHED
    assert result.round_provider_id == "CD_R2026_18"
    assert result.provider_version == 4
    assert len(result.rosters) == 2
    assert len(result.selections) == 5
    ada = next(item for item in result.selections if item["champion_data_player_id"] == "CD_I1")
    assert (ada["match_provider_id"], ada["afl_match_id"]) == ("CD_M100", 100)
    assert (ada["team_provider_id"], ada["afl_team_id"]) == ("CD_T1", 1)
    assert (ada["afl_player_id"], ada["player_name"], ada["jumper_number"]) == (101, "Ada Able", 7)
    assert (ada["named"], ada["selection_state"], ada["selection_group"]) == (True, "FIELD", "namedPlayers")
    assert ada["provider_fields"] == {"providerRank": 1}
    assert result.rosters[0]["teams"][0]["provider_fields"] == {"coachCode": "C1"}
    emergency = next(item for item in result.selections if item["champion_data_player_id"] == "CD_I3")
    assert emergency["emergency"] is True
    assert emergency["provider_fields"] == {"providerRole": "T"}
    assert "weatherNote" in result.rosters[0]["provider_fields"]


def test_empty_and_unpublished_are_valid_non_authentication_outcomes():
    empty = MatchRosterCollector(FixtureClient({"matchRosters": []})).collect("CD_R1")
    unpublished = collect("match_rosters_unpublished.json")
    provider_404 = MatchRosterCollector(FixtureClient(error=AflJsonResourceUnavailable(
        "not published", endpoint="match_rosters", status_code=404, error_code="CFSSDS001"
    ))).collect("CD_R1")

    assert empty.status is RosterStatus.EMPTY
    assert unpublished.status is RosterStatus.UNAVAILABLE
    assert provider_404.status is RosterStatus.UNAVAILABLE
    assert compare_rosters(collect("match_rosters_available.json"), unpublished).replacement_safe is False
    assert compare_rosters(collect("match_rosters_available.json"), unpublished).removed == []


def test_repeated_and_changed_rosters_are_deterministic_and_diffable_without_duplicates():
    initial = collect("match_rosters_available.json")
    repeated = collect("match_rosters_available.json")
    changed = collect("match_rosters_changed.json")

    assert initial.selections == repeated.selections
    assert len({(x["match_provider_id"], x["team_provider_id"],
                x["champion_data_player_id"], x["selection_group"])
               for x in repeated.selections}) == len(repeated.selections)
    same = compare_rosters(initial, repeated)
    assert not same.added and not same.removed and not same.changed
    assert len(same.unchanged) == 5
    diff = compare_rosters(initial, changed)
    assert [x["champion_data_player_id"] for x in diff.added] == ["CD_I6"]
    assert {x["champion_data_player_id"] for x in diff.removed} == {"CD_I2", "CD_I4", "CD_I5"}
    assert diff.changed[0]["after"]["champion_data_player_id"] == "CD_I1"
    assert [x["champion_data_player_id"] for x in diff.unchanged] == ["CD_I3"]


def test_malformed_structure_raises_safe_validation_error():
    with pytest.raises(AflJsonInvalidResponse) as caught:
        collect("match_rosters_malformed.json")
    assert "collection is not a list" in str(caught.value)
    assert "must-not-appear" not in str(caught.value)


def test_raw_capture_uses_established_deterministic_noncredential_convention(tmp_path):
    MatchRosterCollector(FixtureClient(fixture("match_rosters_available.json")),
                         raw_directory=tmp_path).collect("CD_R1")
    files = list(tmp_path.rglob("*.json"))
    assert [path.relative_to(tmp_path).as_posix() for path in files] == [
        "match_rosters/match_rosters__roundProviderId-CD_R1__page-0001.json"
    ]
    assert "x-media-mis-token" not in files[0].read_text()
