import json
from pathlib import Path

import pytest

from afl_json import (
    AflJsonAuthenticationError, AflJsonResourceUnavailable, AflJsonResponse,
    PublicAflCollector,
)

FIXTURES = Path(__file__).parent / "fixtures" / "afl_json"
def fixture(name): return json.loads((FIXTURES / name).read_text())

class FixtureClient:
    def __init__(self, responses): self.responses, self.calls = responses, []
    def get(self, endpoint, *, path_parameters=None, params=None):
        name = endpoint.name if hasattr(endpoint, "name") else endpoint
        self.calls.append((name, dict(params or {})))
        value = self.responses[name]
        if callable(value): value = value(dict(params or {}))
        elif isinstance(value, list): value = value.pop(0)
        return AflJsonResponse(name, 200, value, {})

def test_collects_and_joins_mapped_and_unmapped_players_without_live_requests():
    client = FixtureClient({"player_id_map": fixture("player_id_map.json"), "season_players": fixture("season_players_complete.json")})
    result = PublicAflCollector(client).collect_players("CD_S2026")
    assert result.players == [
        {"champion_data_player_id":"CD_I100","afl_player_id":7001,"name":"Ada Example",
         "given_name":"Ada","family_name":"Example"},
        {"champion_data_player_id":"CD_I999","afl_player_id":None,"name":"Una Mapped",
         "given_name":"Una","family_name":"Mapped"},
    ]
    assert result.player_seasons[0]["team_abbreviation"] == "CAT"
    assert [(x.code,x.context["champion_data_player_id"]) for x in result.diagnostics] == [("unmapped_player","CD_I999")]

def test_complete_default_response_does_not_request_explicit_pages():
    client = FixtureClient({"season_players": fixture("season_players_complete.json")})
    records, diagnostics = PublicAflCollector(client).season_players("CD_S2026")
    assert len(records) == 2 and diagnostics == []
    assert client.calls == [("season_players", {"seasonId":"CD_S2026"})]

def test_incomplete_default_response_explicitly_pages_and_deduplicates_overlap():
    initial={"players":[{"playerId":"CD_I100"}],"totalResults":3}
    pages={1:{"players":{"Count":2,"Items":[{"playerId":"CD_I100"},{"playerId":"CD_I200"}]},"totalResults":3},2:{"players":{"Count":2,"Items":[{"playerId":"CD_I200"},{"playerId":"CD_I300"}]},"totalResults":3}}
    client=FixtureClient({"season_players":lambda p: initial if "pageNum" not in p else pages[p["pageNum"]]})
    records, diagnostics=PublicAflCollector(client,page_size=2).season_players("CD_S2026")
    assert [x["playerId"] for x in records] == ["CD_I100","CD_I200","CD_I300"]
    assert [x[1].get("pageNum") for x in client.calls] == [None,1,2]
    assert [x.code for x in diagnostics] == ["duplicate_season_player"]

def test_malformed_and_duplicate_season_records_are_diagnosed_not_corrupting_data():
    payload={"players":[{"playerName":"Broken"},{"playerId":"CD_I100"},{"playerId":"CD_I100"}],"totalResults":1}
    records, diagnostics=PublicAflCollector(FixtureClient({"season_players":payload})).season_players("CD_S1")
    assert [x["playerId"] for x in records] == ["CD_I100"]
    assert {x.code for x in diagnostics} == {"malformed_season_player","duplicate_season_player"}

def test_duplicate_and_contradictory_id_map_rows_are_diagnosed():
    payload={"idMapResponse":{"ids":[{"playerId":"CD_I1","aflPlayerId":1},{"playerId":"CD_I1","aflPlayerId":1},{"playerId":"CD_I1","aflPlayerId":2},{"playerId":"CD_I2","aflPlayerId":1},{"playerId":None,"aflPlayerId":"bad"}]}}
    mappings, diagnostics=PublicAflCollector(FixtureClient({"player_id_map":payload})).player_id_map()
    assert mappings == {"CD_I1":1}
    assert {x.code for x in diagnostics} == {"duplicate_player_id_map_row","contradictory_champion_data_id","contradictory_afl_id","malformed_player_id_map"}

def test_transferred_player_keeps_distinct_season_associations():
    first={"players":[{"playerId":"CD_I100","team":{"teamId":"CD_T1"}}],"totalResults":1}; second={"players":[{"playerId":"CD_I100","team":{"teamId":"CD_T2"}}],"totalResults":1}
    collector=PublicAflCollector(FixtureClient({"season_players":[first,second]}))
    one=collector.collect_players("CD_S1",{"CD_I100":7001}); two=collector.collect_players("CD_S2",{"CD_I100":7001})
    assert {(x["provider_season_id"],x["team_id"]) for x in one.player_seasons+two.player_seasons} == {("CD_S1","CD_T1"),("CD_S2","CD_T2")}


def test_unavailable_is_distinct_and_authentication_failure_propagates():
    class FailingClient(FixtureClient):
        error = None
        def get(self, endpoint, **kwargs):
            name = endpoint.name if hasattr(endpoint, "name") else endpoint
            if name == "season_players":
                raise self.error
            return super().get(endpoint, **kwargs)

    client = FailingClient({"player_id_map": fixture("player_id_map.json")})
    client.error = AflJsonResourceUnavailable("not published", endpoint="season_players")
    assert PublicAflCollector(client).collect_players("CD_S1").status == "unavailable"

    client.error = AflJsonAuthenticationError("bad token", endpoint="season_players")
    with pytest.raises(AflJsonAuthenticationError, match="bad token"):
        PublicAflCollector(client).collect_players("CD_S1")

def test_player_raw_diagnostic_files_have_stable_endpoint_season_and_page_names(tmp_path):
    initial={"players":[{"playerId":"CD_I1"}],"totalResults":2}; page={"players":[{"playerId":"CD_I1"},{"playerId":"CD_I2"}],"totalResults":2}
    collector=PublicAflCollector(FixtureClient({"season_players":[initial,page],"player_id_map":fixture("player_id_map.json")}),page_size=2,raw_directory=tmp_path)
    collector.player_id_map(); collector.season_players("CD_S1")
    files=sorted(x.relative_to(tmp_path).as_posix() for x in tmp_path.rglob("*.json"))
    assert files == ["player_id_map/player_id_map__page-0001.json","season_players/season_players__seasonId-CD_S1__page-0000.json","season_players/season_players__seasonId-CD_S1__page-0001.json"]
