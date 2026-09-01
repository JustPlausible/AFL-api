"""Maintained AFL JSON endpoint and collection contracts.

This module contains data only: transports and collectors consume these immutable
definitions rather than maintaining their own URLs, authentication rules, or
response paths. Provider identifiers are opaque strings and must never be
derived from AFL's numeric identifiers (or vice versa).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Literal, Mapping

PUBLIC_API_BASE: Final = "https://aflapi.afl.com.au/afl/v2"
# The CFS service root shared by every CFS-hosted endpoint. Each endpoint
# family models its own path below (e.g. "/afl/players", "/afl/WMCTok",
# "/commentaryFeed/{...}") rather than the root itself encoding the "afl"
# family -- see Issue #199. This is what lets commentaryFeed (which lives
# directly under this root, not under "/afl") resolve correctly without a
# per-endpoint base-URL override or string-manipulation workaround.
CFS_SERVICE_ROOT: Final = "https://api.afl.com.au/cfs"
STATSPRO_SERVICE_ROOT: Final = "https://api.afl.com.au/statspro"
CFS_TOKEN_HEADER: Final = "x-media-mis-token"
CFS_ERROR_AUTH: Final = "CFSAPI001"
CFS_ERROR_NOT_PUBLISHED: Final = "CFSSDS001"
SUCCESS_HTTP_STATUSES: Final = (200,)
RETRYABLE_HTTP_STATUSES: Final = (429, 500, 502, 503, 504)


class SourceSystem(str, Enum):
    PUBLIC = "public_api"
    CFS = "cfs"
    STATSPRO = "statspro"


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"


class Pagination(str, Enum):
    NONE = "none"
    RESPONSE_DRIVEN = "response_driven"
    VERIFY_TOTAL_THEN_RESPONSE_DRIVEN = "verify_total_then_response_driven"


@dataclass(frozen=True, slots=True)
class EndpointDefinition:
    """Complete request and response contract for one named endpoint."""

    name: str
    source: SourceSystem
    method: HttpMethod
    path_template: str
    requires_auth: bool
    entity_type: str
    collection_paths: tuple[str, ...]
    identifier_type: str | None
    required_path_parameters: tuple[str, ...] = ()
    required_query_parameters: tuple[str, ...] = ()
    optional_query_parameters: tuple[str, ...] = ()
    pagination: Pagination = Pagination.NONE
    verified: bool = True
    unverified_fields: tuple[str, ...] = ()
    required_record_fields: tuple[str, ...] = ()

    @property
    def base_url(self) -> str:
        if self.source is SourceSystem.CFS:
            return CFS_SERVICE_ROOT
        if self.source is SourceSystem.STATSPRO:
            return STATSPRO_SERVICE_ROOT
        return PUBLIC_API_BASE

    @property
    def url_template(self) -> str:
        return self.base_url + self.path_template


def _endpoint(name: str, path: str, entity: str, **values: object) -> EndpointDefinition:
    if values.get("source") is SourceSystem.PUBLIC and entity != "player_id_map":
        values.setdefault("required_record_fields", ("providerId",))
    return EndpointDefinition(name=name, path_template=path, entity_type=entity, **values)  # type: ignore[arg-type]


_ENDPOINTS = {
    "wmc_token": _endpoint("wmc_token", "/afl/WMCTok", "authentication", source=SourceSystem.CFS,
        method=HttpMethod.POST, requires_auth=False, collection_paths=("token",), identifier_type=None),
    "competitions": _endpoint("competitions", "/competitions", "competition", source=SourceSystem.PUBLIC,
        method=HttpMethod.GET, requires_auth=False, collection_paths=("competitions",), identifier_type="competition",
        optional_query_parameters=("pageSize", "pageNum"), pagination=Pagination.RESPONSE_DRIVEN),
    "competition_seasons": _endpoint("competition_seasons", "/competitions/{competition_id}/compseasons",
        "competition_season", source=SourceSystem.PUBLIC, method=HttpMethod.GET, requires_auth=False,
        collection_paths=("compSeasons",), identifier_type="competition_season",
        required_path_parameters=("competition_id",), optional_query_parameters=("pageSize", "pageNum"),
        pagination=Pagination.RESPONSE_DRIVEN),
    "rounds": _endpoint("rounds", "/compseasons/{comp_season_id}/rounds", "round", source=SourceSystem.PUBLIC,
        method=HttpMethod.GET, requires_auth=False, collection_paths=("rounds",), identifier_type="round",
        required_path_parameters=("comp_season_id",), optional_query_parameters=("roundNumber", "pageSize", "pageNum"),
        pagination=Pagination.RESPONSE_DRIVEN),
    "teams": _endpoint("teams", "/teams", "team", source=SourceSystem.PUBLIC, method=HttpMethod.GET,
        requires_auth=False, collection_paths=("teams",), identifier_type="team",
        required_query_parameters=("compSeasonId",), optional_query_parameters=("pageSize", "pageNum"),
        pagination=Pagination.RESPONSE_DRIVEN),
    "matches": _endpoint("matches", "/matches", "match", source=SourceSystem.PUBLIC, method=HttpMethod.GET,
        requires_auth=False, collection_paths=("matches",), identifier_type="match",
        required_query_parameters=("competitionId", "compSeasonId", "roundNumber"),
        optional_query_parameters=("pageSize", "pageNum"), pagination=Pagination.RESPONSE_DRIVEN),
    "match_detail": _endpoint("match_detail", "/matches/{afl_match_id}", "match_detail",
        source=SourceSystem.PUBLIC, method=HttpMethod.GET, requires_auth=False,
        collection_paths=("matches",), identifier_type="match",
        required_path_parameters=("afl_match_id",)),
    "player_id_map": _endpoint("player_id_map", "/players/idmap", "player_id_map", source=SourceSystem.PUBLIC,
        method=HttpMethod.GET, requires_auth=False, collection_paths=("idMapResponse.ids",), identifier_type="player"),
    "season_players": _endpoint("season_players", "/afl/players", "season_player", source=SourceSystem.CFS,
        method=HttpMethod.GET, requires_auth=True, collection_paths=("players",), identifier_type="player",
        required_query_parameters=("seasonId",),
        optional_query_parameters=("pageSize", "pageNum", "sortBy", "teamIds", "playerPosition"),
        pagination=Pagination.VERIFY_TOTAL_THEN_RESPONSE_DRIVEN),
    "match_rosters": _endpoint("match_rosters", "/afl/matchRosters/round/{round_provider_id}", "match_roster",
        source=SourceSystem.CFS, method=HttpMethod.GET, requires_auth=True,
        collection_paths=(), identifier_type="player",
        required_path_parameters=("round_provider_id",),
        unverified_fields=("positions semantics", "teamPlayers relationship", "late-change timing")),
    "match_player_statistics": _endpoint("match_player_statistics", "/afl/playerStats/match/{match_provider_id}",
        "match_player_statistics", source=SourceSystem.CFS, method=HttpMethod.GET, requires_auth=True,
        collection_paths=("homeTeamPlayerStats", "awayTeamPlayerStats"), identifier_type="player",
        required_path_parameters=("match_provider_id",),
        unverified_fields=("time-on-ground path", "dreamTeamPoints fantasy-score semantics")),
    "statspro_season_total": _endpoint("statspro_season_total", "/playersStats/seasons/{season_provider_id}",
        "statspro_season_total", source=SourceSystem.STATSPRO, method=HttpMethod.GET,
        requires_auth=True, collection_paths=("players",), identifier_type="player",
        required_path_parameters=("season_provider_id",),
        required_query_parameters=("includeBenchmarks", "playerNameLike", "playerPosition", "teamId")),
    "statspro_round_total": _endpoint("statspro_round_total", "/playersStats/rounds/{round_provider_id}",
        "statspro_round_total", source=SourceSystem.STATSPRO, method=HttpMethod.GET,
        requires_auth=True, collection_paths=("players",), identifier_type="player",
        required_path_parameters=("round_provider_id",), required_query_parameters=("teamId",)),
}

ENDPOINTS: Final[Mapping[str, EndpointDefinition]] = MappingProxyType(_ENDPOINTS)


@dataclass(frozen=True, slots=True)
class IdentifierType:
    entity: str
    numeric_field: str | None
    provider_field: str
    provider_prefix: str


IDENTIFIER_TYPES: Final[Mapping[str, IdentifierType]] = MappingProxyType({
    rule.entity: rule for rule in (
        IdentifierType("competition", "id", "providerId", "CD_C"),
        IdentifierType("competition_season", "id", "providerId", "CD_S"),
        IdentifierType("round", "id", "providerId", "CD_R"),
        IdentifierType("match", "id", "providerId", "CD_M"),
        IdentifierType("team", "id", "providerId", "CD_T"),
        IdentifierType("club", "id", "providerId", "CD_O"),
        IdentifierType("venue", "id", "providerId", "CD_V"),
        IdentifierType("player", "afl_player_id", "playerId", "CD_I"),
    )
})

SOURCE_PRIORITY: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({
    "competition_season_round_team_match": ("public_api", "html_scraper"),
    "season_player": ("cfs_season_players", "html_scraper"),
    "player_crosswalk": ("public_player_id_map", "unmapped"),
    "selection_roster": ("cfs_match_rosters", "html_scraper"),
    "match_statistics": ("cfs_match_player_statistics", "html_scraper"),
})

ResponseDecision = Literal["refresh_token_and_retry_once", "not_published", "fail"]


def classify_cfs_response(http_status: int, error_code: str | None) -> ResponseDecision:
    """Apply the documented authentication and not-published error policy."""
    if http_status == 401 or error_code == CFS_ERROR_AUTH:
        return "refresh_token_and_retry_once"
    if http_status == 404 and error_code == CFS_ERROR_NOT_PUBLISHED:
        return "not_published"
    return "fail"


def get_endpoint(name: str) -> EndpointDefinition:
    """Return a contract by its stable collector-facing name."""
    try:
        return ENDPOINTS[name]
    except KeyError as error:
        raise KeyError(f"Unknown AFL JSON endpoint: {name}") from error
