import re

import pytest

from afl_json.contracts import (
    CFS_ERROR_AUTH,
    CFS_ERROR_NOT_PUBLISHED,
    ENDPOINTS,
    IDENTIFIER_TYPES,
    HttpMethod,
    Pagination,
    RETRYABLE_HTTP_STATUSES,
    SOURCE_PRIORITY,
    SUCCESS_HTTP_STATUSES,
    classify_cfs_response,
    get_endpoint,
)


EXPECTED_NAMES = {
    "wmc_token", "competitions", "competition_seasons", "rounds", "teams", "matches",
    "match_detail", "player_id_map", "season_players", "match_rosters", "match_player_statistics",
}


def test_catalogue_has_all_initial_endpoint_names():
    assert set(ENDPOINTS) == EXPECTED_NAMES
    assert all(key == endpoint.name for key, endpoint in ENDPOINTS.items())


@pytest.mark.parametrize("endpoint", ENDPOINTS.values(), ids=lambda endpoint: endpoint.name)
def test_url_placeholders_are_declared_required_path_parameters(endpoint):
    placeholders = set(re.findall(r"{([^}]+)}", endpoint.url_template))
    assert placeholders == set(endpoint.required_path_parameters)
    assert endpoint.url_template.startswith("https://")


def test_required_query_parameters_and_authentication_flags():
    assert ENDPOINTS["teams"].required_query_parameters == ("compSeasonId",)
    assert ENDPOINTS["matches"].required_query_parameters == (
        "competitionId", "compSeasonId", "roundNumber",
    )
    assert ENDPOINTS["match_detail"].path_template == "/matches/{afl_match_id}"
    assert ENDPOINTS["match_detail"].required_path_parameters == ("afl_match_id",)
    assert ENDPOINTS["season_players"].required_query_parameters == ("seasonId",)
    assert {item.name for item in ENDPOINTS.values() if item.requires_auth} == {
        "season_players", "match_rosters", "match_player_statistics",
    }
    assert ENDPOINTS["wmc_token"].method is HttpMethod.POST


def test_collection_paths_and_pagination_contracts():
    assert ENDPOINTS["player_id_map"].collection_paths == ("idMapResponse.ids",)
    assert ENDPOINTS["match_player_statistics"].collection_paths == (
        "homeTeamPlayerStats", "awayTeamPlayerStats",
    )
    assert ENDPOINTS["season_players"].pagination is Pagination.VERIFY_TOTAL_THEN_RESPONSE_DRIVEN
    assert ENDPOINTS["competitions"].pagination is Pagination.RESPONSE_DRIVEN


def test_numeric_and_provider_identifiers_are_distinct():
    assert IDENTIFIER_TYPES["match"].numeric_field == "id"
    assert IDENTIFIER_TYPES["match"].provider_field == "providerId"
    assert IDENTIFIER_TYPES["match"].provider_prefix == "CD_M"
    assert IDENTIFIER_TYPES["player"].numeric_field == "afl_player_id"
    assert IDENTIFIER_TYPES["player"].provider_prefix == "CD_I"


def test_known_cfs_error_contracts():
    assert SUCCESS_HTTP_STATUSES == (200,)
    assert RETRYABLE_HTTP_STATUSES == (429, 500, 502, 503, 504)
    assert CFS_ERROR_AUTH == "CFSAPI001"
    assert classify_cfs_response(401, CFS_ERROR_AUTH) == "refresh_token_and_retry_once"
    assert CFS_ERROR_NOT_PUBLISHED == "CFSSDS001"
    assert classify_cfs_response(404, CFS_ERROR_NOT_PUBLISHED) == "not_published"
    assert classify_cfs_response(404, None) == "fail"


def test_source_priority_has_domain_specific_html_fallbacks():
    assert SOURCE_PRIORITY["match_statistics"] == (
        "cfs_match_player_statistics", "html_scraper",
    )
    assert SOURCE_PRIORITY["competition_season_round_team_match"][0] == "public_api"


def test_unknown_endpoint_has_explanatory_error():
    with pytest.raises(KeyError, match="Unknown AFL JSON endpoint"):
        get_endpoint("missing")
