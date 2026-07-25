"""Declarative AFL source schema and endpoint registry.

This module is intentionally independent of HTTP and database libraries.  It is
an implementation contract for collectors, normalisers and persistence adapters.
Unknown fields should be preserved in raw payloads and must not be silently
coerced to defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal


PUBLIC_API_BASE: Final = "https://aflapi.afl.com.au/afl/v2"
CFS_API_BASE: Final = "https://api.afl.com.au/cfs/afl"


class SourceSystem(str, Enum):
    PUBLIC_API = "public_api"
    CFS = "cfs"


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"


class RefreshClass(str, Enum):
    BOOTSTRAP = "bootstrap"
    YEARLY = "yearly"
    WEEKLY = "weekly"
    DAILY = "daily"
    HOURLY = "hourly"
    LIVE = "live"
    ON_DEMAND = "on_demand"


class MissingPolicy(str, Enum):
    ERROR_BATCH = "error_batch"
    ERROR_RECORD = "error_record"
    SET_NULL = "set_null"
    USE_FALLBACK = "use_fallback"
    PRESERVE_UNKNOWN = "preserve_unknown"


class MatchStatus(str, Enum):
    PLACEHOLDER = "PLACEHOLDER"
    SCHEDULED = "SCHEDULED"
    UNCONFIRMED_TEAMS = "UNCONFIRMED_TEAMS"
    CONFIRMED_TEAMS = "CONFIRMED_TEAMS"
    LIVE = "LIVE"
    CONCLUDED = "CONCLUDED"


@dataclass(frozen=True, slots=True)
class EndpointDefinition:
    key: str
    method: HttpMethod
    source: SourceSystem
    path_template: str
    requires_token: bool
    entity: str
    collection_path: str | None
    source_primary_key: str | None
    refresh_class: RefreshClass
    required_params: tuple[str, ...] = ()
    optional_params: tuple[str, ...] = ()
    notes: str = ""

    @property
    def base_url(self) -> str:
        return CFS_API_BASE if self.source is SourceSystem.CFS else PUBLIC_API_BASE

    @property
    def url_template(self) -> str:
        return f"{self.base_url}{self.path_template}"


ENDPOINTS: Final[dict[str, EndpointDefinition]] = {
    "wmc_token": EndpointDefinition(
        key="wmc_token",
        method=HttpMethod.POST,
        source=SourceSystem.CFS,
        path_template="/WMCTok",
        requires_token=False,
        entity="authentication",
        collection_path=None,
        source_primary_key=None,
        refresh_class=RefreshClass.ON_DEMAND,
        notes="Cache token; refresh on HTTP 401 and retry once.",
    ),
    "competitions": EndpointDefinition(
        key="competitions",
        method=HttpMethod.GET,
        source=SourceSystem.PUBLIC_API,
        path_template="/competitions",
        requires_token=False,
        entity="competition",
        collection_path="competitions",
        source_primary_key="id",
        refresh_class=RefreshClass.YEARLY,
        optional_params=("pageSize", "pageNum"),
    ),
    "competition_seasons": EndpointDefinition(
        key="competition_seasons",
        method=HttpMethod.GET,
        source=SourceSystem.PUBLIC_API,
        path_template="/competitions/{competition_id}/compseasons",
        requires_token=False,
        entity="competition_season",
        collection_path="compSeasons",
        source_primary_key="id",
        refresh_class=RefreshClass.DAILY,
        required_params=("competition_id",),
        optional_params=("pageSize", "pageNum"),
        notes="Provider may cap page size; follow response pagination.",
    ),
    "rounds": EndpointDefinition(
        key="rounds",
        method=HttpMethod.GET,
        source=SourceSystem.PUBLIC_API,
        path_template="/compseasons/{comp_season_id}/rounds",
        requires_token=False,
        entity="round",
        collection_path="rounds",
        source_primary_key="id",
        refresh_class=RefreshClass.DAILY,
        required_params=("comp_season_id",),
        optional_params=("roundNumber", "pageSize", "pageNum"),
    ),
    "teams": EndpointDefinition(
        key="teams",
        method=HttpMethod.GET,
        source=SourceSystem.PUBLIC_API,
        path_template="/teams",
        requires_token=False,
        entity="team",
        collection_path="teams",
        source_primary_key="id",
        refresh_class=RefreshClass.WEEKLY,
        required_params=("compSeasonId",),
        optional_params=("pageSize", "pageNum"),
    ),
    "matches": EndpointDefinition(
        key="matches",
        method=HttpMethod.GET,
        source=SourceSystem.PUBLIC_API,
        path_template="/matches",
        requires_token=False,
        entity="match",
        collection_path="matches",
        source_primary_key="id",
        refresh_class=RefreshClass.HOURLY,
        required_params=("competitionId", "compSeasonId"),
        optional_params=("roundNumber", "pageSize", "pageNum"),
    ),
    "player_id_map": EndpointDefinition(
        key="player_id_map",
        method=HttpMethod.GET,
        source=SourceSystem.PUBLIC_API,
        path_template="/players/idmap",
        requires_token=False,
        entity="player_external_id",
        collection_path="idMapResponse.ids",
        source_primary_key="object_key",
        refresh_class=RefreshClass.WEEKLY,
        notes="Object keys are CD_I identifiers; values are AFL numeric player IDs.",
    ),
    "season_players": EndpointDefinition(
        key="season_players",
        method=HttpMethod.GET,
        source=SourceSystem.CFS,
        path_template="/players",
        requires_token=True,
        entity="player_season",
        collection_path="players",
        source_primary_key="playerId",
        refresh_class=RefreshClass.DAILY,
        required_params=("seasonId",),
        optional_params=("pageSize", "pageNum", "sortBy", "teamIds", "playerPosition"),
        notes="Validate returned count against totalResults and paginate if necessary.",
    ),
    "match_player_stats": EndpointDefinition(
        key="match_player_stats",
        method=HttpMethod.GET,
        source=SourceSystem.CFS,
        path_template="/playerStats/match/{match_provider_id}",
        requires_token=True,
        entity="player_match_stats",
        collection_path=None,
        source_primary_key="player.player.player.playerId",
        refresh_class=RefreshClass.LIVE,
        required_params=("match_provider_id",),
        notes="Flatten both homeTeamPlayerStats and awayTeamPlayerStats.",
    ),
    "round_match_rosters": EndpointDefinition(
        key="round_match_rosters",
        method=HttpMethod.GET,
        source=SourceSystem.CFS,
        path_template="/matchRosters/round/{round_provider_id}",
        requires_token=True,
        entity="match_roster",
        collection_path=None,
        source_primary_key=None,
        refresh_class=RefreshClass.HOURLY,
        required_params=("round_provider_id",),
        notes="Response schema still needs a fixture and documented natural key.",
    ),
    "stats_centre_players": EndpointDefinition(
        key="stats_centre_players",
        method=HttpMethod.GET,
        source=SourceSystem.CFS,
        path_template="/statsCentre/players",
        requires_token=True,
        entity="stats_centre_player",
        collection_path=None,
        source_primary_key=None,
        refresh_class=RefreshClass.ON_DEMAND,
        optional_params=("competitionId", "teamIds"),
        notes="Low priority until unique value over other player/stat endpoints is established.",
    ),
}


@dataclass(frozen=True, slots=True)
class FieldMapping:
    canonical_field: str
    database_column: str | None
    json_paths: tuple[str, ...]
    value_type: str
    required: bool
    missing_policy: MissingPolicy
    transform: str | None = None
    notes: str = ""


PLAYER_MATCH_STAT_FIELDS: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(
        "champion_data_player_id",
        "player_stats.champion_id",
        ("player.player.player.playerId",),
        "str",
        True,
        MissingPolicy.ERROR_RECORD,
        notes="Preserve the complete CD_I identifier.",
    ),
    FieldMapping(
        "player_given_name",
        None,
        ("player.player.player.playerName.givenName",),
        "str",
        True,
        MissingPolicy.ERROR_RECORD,
        transform="strip",
    ),
    FieldMapping(
        "player_surname",
        None,
        ("player.player.player.playerName.surname",),
        "str",
        True,
        MissingPolicy.ERROR_RECORD,
        transform="strip",
    ),
    FieldMapping(
        "jumper_number",
        "player_stats.jumper_number",
        ("player.jumperNumber", "player.player.player.playerJumperNumber"),
        "int | None",
        False,
        MissingPolicy.SET_NULL,
        transform="integral_number_or_null",
    ),
    FieldMapping(
        "team_provider_id",
        None,
        ("player.teamId",),
        "str",
        True,
        MissingPolicy.ERROR_RECORD,
    ),
    FieldMapping("goals", "player_stats.goals", ("playerStats.stats.goals",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("behinds", "player_stats.behinds", ("playerStats.stats.behinds",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("kicks", "player_stats.kicks", ("playerStats.stats.kicks",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("handballs", "player_stats.handballs", ("playerStats.stats.handballs",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("disposals", "player_stats.disposals", ("playerStats.stats.disposals",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("marks", "player_stats.marks", ("playerStats.stats.marks",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("tackles", "player_stats.tackles", ("playerStats.stats.tackles",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("hitouts", "player_stats.hitouts", ("playerStats.stats.hitouts",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("clearances", "player_stats.clearances", ("playerStats.stats.clearances.totalClearances",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping("metres_gained", "player_stats.metres_gained", ("playerStats.stats.metresGained",), "int | float | None", False, MissingPolicy.SET_NULL),
    FieldMapping("goal_assists", "player_stats.goal_assists", ("playerStats.stats.goalAssists",), "int | None", False, MissingPolicy.SET_NULL, "integral_number_or_null"),
    FieldMapping(
        "fantasy_score",
        "player_stats.af_score",
        ("playerStats.stats.dreamTeamPoints",),
        "int | None",
        False,
        MissingPolicy.SET_NULL,
        "integral_number_or_null",
        "Confirm dreamTeamPoints is the required AF score source.",
    ),
    FieldMapping(
        "time_on_ground_percent",
        "player_stats.time_on_ground_pct",
        (),
        "float | None",
        False,
        MissingPolicy.SET_NULL,
        notes="Not verified in the supplied JSON sample.",
    ),
)


@dataclass(frozen=True, slots=True)
class IdentifierRule:
    entity: str
    public_id_field: str | None
    provider_id_field: str
    provider_prefix: str


IDENTIFIER_RULES: Final[tuple[IdentifierRule, ...]] = (
    IdentifierRule("competition", "id", "providerId", "CD_C"),
    IdentifierRule("competition_season", "id", "providerId", "CD_S"),
    IdentifierRule("round", "id", "providerId", "CD_R"),
    IdentifierRule("match", "id", "providerId", "CD_M"),
    IdentifierRule("team", "id", "providerId", "CD_T"),
    IdentifierRule("club", "id", "providerId", "CD_O"),
    IdentifierRule("venue", "id", "providerId", "CD_V"),
    IdentifierRule("player", "afl_player_id", "playerId", "CD_I"),
)


CFS_ERROR_AUTH: Final = "CFSAPI001"
CFS_ERROR_NOT_PUBLISHED: Final = "CFSSDS001"

RetryDecision = Literal["refresh_token_and_retry_once", "not_published", "fail"]


def classify_cfs_response(http_status: int, error_code: str | None) -> RetryDecision:
    """Classify known CFS failures without hiding unknown errors."""
    if http_status == 401 or error_code == CFS_ERROR_AUTH:
        return "refresh_token_and_retry_once"
    if http_status == 404 and error_code == CFS_ERROR_NOT_PUBLISHED:
        return "not_published"
    return "fail"
