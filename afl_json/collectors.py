"""Collection and normalisation for AFL's public metadata hierarchy.

The collectors deliberately return plain dictionaries and do not write to the
application database.  Source objects are retained in ``source`` so a newly
introduced provider field is not lost while the explicit keys provide a stable
interface to downstream adapters.
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .client import AflJsonClient, AflJsonInvalidResponse, AflJsonResourceUnavailable
from .contracts import IDENTIFIER_TYPES, Pagination, get_endpoint

logger = logging.getLogger(__name__)


class CollectionError(RuntimeError):
    """A response cannot be collected or selected deterministically."""


class PaginationError(CollectionError):
    """Pagination metadata is malformed or does not make progress."""


@dataclass(frozen=True, slots=True)
class CollectionResult:
    competition: dict[str, Any]
    season: dict[str, Any]
    rounds: list[dict[str, Any]]
    teams: list[dict[str, Any]]
    matches: list[dict[str, Any]]
    # The competition's independently determined current season (by the same
    # current-flag/date semantics as automatic selection), or None when it
    # cannot be determined unambiguously. This may differ from ``season`` when
    # a specific (e.g. historical) season was explicitly requested.
    current_season_afl_id: int | None = None


@dataclass(frozen=True, slots=True)
class CollectionDiagnostic:
    """A non-secret, record-level problem found during collection."""

    code: str
    message: str
    context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PlayerCollectionResult:
    """Permanent player identities and their distinct season listings."""

    players: list[dict[str, Any]]
    player_seasons: list[dict[str, Any]]
    diagnostics: list[CollectionDiagnostic]
    status: str = "published"


class RawResponseWriter:
    """Write opt-in source captures below one dedicated directory."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def write(self, endpoint: str, payload: Any, *, scope: Mapping[str, object], page: int) -> Path:
        parts = [endpoint]
        for key, value in sorted(scope.items()):
            parts.append(f"{_safe_component(key)}-{_safe_component(value)}")
        path = self.directory / endpoint / ("__".join(parts) + f"__page-{page:04d}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        # A collector-owned deterministic filename may be refreshed, but cannot
        # collide with fixtures or production files outside the configured root.
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def _safe_component(value: object) -> str:
    text = str(value)
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in text)


class PublicAflCollector:
    """Orchestrate public metadata endpoints using the maintained contracts."""

    def __init__(self, client: AflJsonClient, *, page_size: int = 100,
                 raw_directory: str | Path | None = None, max_pages: int = 1000):
        if page_size < 1 or max_pages < 1:
            raise ValueError("page_size and max_pages must be positive")
        self.client = client
        self.page_size = page_size
        self.max_pages = max_pages
        self.raw_writer = RawResponseWriter(raw_directory) if raw_directory is not None else None

    @staticmethod
    def _diagnose(diagnostics: list[CollectionDiagnostic], code: str, message: str,
                  **context: Any) -> None:
        diagnostic = CollectionDiagnostic(code, message, context)
        diagnostics.append(diagnostic)
        logger.warning("%s: %s (%s)", code, message, context)

    def collect_endpoint(self, endpoint: str, *, path_parameters: Mapping[str, object] | None = None,
                         params: Mapping[str, object] | None = None) -> list[dict[str, Any]]:
        """Collect every unique record using response pagination metadata."""
        definition = get_endpoint(endpoint)
        if definition.pagination is Pagination.NONE:
            page_params = dict(params or {})
        else:
            page_params = {**(params or {}), "pageSize": self.page_size, "pageNum": 1}
        records: list[dict[str, Any]] = []
        seen: set[object] = set()
        page = 1

        while True:
            if page > self.max_pages:
                raise PaginationError(f"{endpoint} exceeded the safe limit of {self.max_pages} pages")
            if definition.pagination is not Pagination.NONE:
                page_params["pageNum"] = page
            response = self.client.get(definition, path_parameters=path_parameters, params=page_params)
            payload = response.data
            if self.raw_writer:
                scope = {**(path_parameters or {}), **(params or {})}
                self.raw_writer.write(endpoint, payload, scope=scope, page=page)
            page_records = _extract_collection(payload, definition.collection_paths, endpoint)
            added = 0
            for record_index, record in enumerate(page_records):
                if not isinstance(record, dict):
                    raise CollectionError(f"{endpoint} collection contains a non-object record")
                missing_fields = [
                    field for field in definition.required_record_fields
                    if field not in record or record[field] is None
                ]
                if missing_fields:
                    raise CollectionError(
                        f"{endpoint} record {record_index} is missing required field(s): "
                        f"{', '.join(missing_fields)}"
                    )
                identity = _record_identity(record, definition.identifier_type)
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(deepcopy(record))
                added += 1

            if definition.pagination is Pagination.NONE:
                break
            next_page = _next_page(payload, page, len(page_records), len(records), endpoint)
            if next_page is None:
                break
            if next_page <= page:
                raise PaginationError(f"{endpoint} pagination did not progress from page {page}")
            if added == 0:
                raise PaginationError(f"{endpoint} page {page} added no new records while more pages were advertised")
            page = next_page
        return records

    def competitions(self) -> list[dict[str, Any]]:
        return [_normalise_competition(item) for item in self.collect_endpoint("competitions")]

    def competition_seasons(self, competition_id: int) -> list[dict[str, Any]]:
        items = self.collect_endpoint("competition_seasons", path_parameters={"competition_id": competition_id})
        return [_normalise_season(item) for item in items]

    def rounds(self, season_id: int) -> list[dict[str, Any]]:
        items = self.collect_endpoint("rounds", path_parameters={"comp_season_id": season_id})
        return [_normalise_round(item) for item in items]

    def teams(self, season_id: int) -> list[dict[str, Any]]:
        items = self.collect_endpoint("teams", params={"compSeasonId": season_id})
        return [_normalise_team(item) for item in items]

    def matches(self, competition_id: int, season_id: int, round_number: int) -> list[dict[str, Any]]:
        items = self.collect_endpoint("matches", params={
            "competitionId": competition_id, "compSeasonId": season_id, "roundNumber": round_number,
        })
        return [_normalise_match(item) for item in items]

    def player_id_map(self) -> tuple[dict[str, int], list[CollectionDiagnostic]]:
        """Collect and validate the independently reusable CD-to-AFL crosswalk."""
        response = self.client.get("player_id_map")
        payload = response.data
        if self.raw_writer:
            self.raw_writer.write("player_id_map", payload, scope={}, page=1)
        rows = _extract_id_map_rows(payload)
        mappings: dict[str, int] = {}
        reverse: dict[int, str] = {}
        diagnostics: list[CollectionDiagnostic] = []
        seen_rows: set[tuple[str, int]] = set()
        for champion_id, afl_id in rows:
            if not _valid_provider_player_id(champion_id) or not _valid_afl_id(afl_id):
                self._diagnose(diagnostics, "malformed_player_id_map",
                               "Player ID-map row has invalid identifiers",
                               champion_data_player_id=champion_id, afl_player_id=afl_id)
                continue
            row = (champion_id, afl_id)
            if row in seen_rows:
                self._diagnose(diagnostics, "duplicate_player_id_map_row",
                               "Duplicate player ID-map row", champion_data_player_id=champion_id,
                               afl_player_id=afl_id)
                continue
            seen_rows.add(row)
            if champion_id in mappings and mappings[champion_id] != afl_id:
                self._diagnose(diagnostics, "contradictory_champion_data_id",
                               "Champion Data ID maps to multiple AFL IDs",
                               champion_data_player_id=champion_id,
                               afl_player_ids=[mappings[champion_id], afl_id])
                continue
            if afl_id in reverse and reverse[afl_id] != champion_id:
                self._diagnose(diagnostics, "contradictory_afl_id",
                               "AFL ID maps to multiple Champion Data IDs", afl_player_id=afl_id,
                               champion_data_player_ids=[reverse[afl_id], champion_id])
                continue
            mappings[champion_id] = afl_id
            reverse[afl_id] = champion_id
        return mappings, diagnostics

    def season_players(self, provider_season_id: str) -> tuple[list[dict[str, Any]], list[CollectionDiagnostic]]:
        """Collect a complete season population, paging only when totals require it."""
        if not provider_season_id:
            raise ValueError("provider_season_id is required")
        diagnostics: list[CollectionDiagnostic] = []
        records: list[dict[str, Any]] = []
        seen: set[str] = set()

        def consume(payload: Any, page: int) -> int:
            page_records = _extract_season_players(payload)
            if self.raw_writer:
                self.raw_writer.write("season_players", payload,
                                      scope={"seasonId": provider_season_id}, page=page)
            for index, item in enumerate(page_records):
                if not isinstance(item, dict) or not _valid_provider_player_id(item.get("playerId")):
                    self._diagnose(diagnostics, "malformed_season_player",
                                   "Season-player record has no valid Champion Data player ID",
                                   provider_season_id=provider_season_id, page=page, record_index=index)
                    continue
                player_id = item["playerId"]
                if player_id in seen:
                    self._diagnose(diagnostics, "duplicate_season_player",
                                   "Duplicate player in provider season",
                                   provider_season_id=provider_season_id,
                                   champion_data_player_id=player_id, page=page)
                    continue
                seen.add(player_id)
                records.append(deepcopy(item))
            return len(page_records)

        initial = self.client.get("season_players", params={"seasonId": provider_season_id}).data
        # Page zero denotes the endpoint's unpaged/default completeness probe;
        # explicit page numbers therefore cannot overwrite its raw capture.
        initial_count = consume(initial, 0)
        total = _reported_total(initial)
        represented_count = _represented_player_count(initial)
        if represented_count is not None and represented_count != initial_count:
            self._diagnose(diagnostics, "season_player_count_mismatch",
                           "players.Count does not match returned player records",
                           provider_season_id=provider_season_id,
                           players_count=represented_count, returned_count=initial_count)
        if total is None:
            self._diagnose(diagnostics, "missing_season_player_total",
                           "Season-player response did not report totalResults",
                           provider_season_id=provider_season_id, collected_count=len(records))
            return records, diagnostics
        if len(records) < total:
            # The unpaged response is only a completeness probe. Explicit pages
            # are collected from page one because its implicit size/order is not
            # assumed to match the API's paged view.
            records.clear()
            seen.clear()
            for page in range(1, self.max_pages + 1):
                payload = self.client.get("season_players", params={
                    "seasonId": provider_season_id, "pageSize": self.page_size, "pageNum": page,
                }).data
                page_count = consume(payload, page)
                page_total = _reported_total(payload)
                if page_total is not None and page_total != total:
                    self._diagnose(diagnostics, "season_player_total_changed",
                                   "totalResults changed during pagination",
                                   provider_season_id=provider_season_id,
                                   initial_total=total, page_total=page_total, page=page)
                if len(records) >= total:
                    break
                if page_count == 0:
                    break
            else:
                raise PaginationError(f"season_players exceeded the safe limit of {self.max_pages} pages")
        if len(records) != total:
            self._diagnose(diagnostics, "unreconciled_season_player_total",
                           "Collected unique players do not match totalResults",
                           provider_season_id=provider_season_id,
                           collected_count=len(records), total_results=total)
        return records, diagnostics

    def collect_players(self, provider_season_id: str,
                        id_map: Mapping[str, int] | None = None) -> PlayerCollectionResult:
        """Normalise one season and join it to an independent player ID map."""
        if id_map is None:
            mappings, diagnostics = self.player_id_map()
        else:
            mappings, diagnostics = dict(id_map), []
        try:
            raw_players, season_diagnostics = self.season_players(provider_season_id)
        except AflJsonResourceUnavailable:
            return PlayerCollectionResult([], [], diagnostics, "unavailable")
        diagnostics.extend(season_diagnostics)
        identities: list[dict[str, Any]] = []
        associations: list[dict[str, Any]] = []
        for item in raw_players:
            champion_id = item["playerId"]
            afl_id = mappings.get(champion_id)
            player_name = item.get("playerName")
            name = _player_name(player_name)
            identities.append({"champion_data_player_id": champion_id,
                               "afl_player_id": afl_id, "name": name,
                               "given_name": (player_name.get("givenName") if isinstance(player_name, dict) else None),
                               "family_name": (player_name.get("surname") if isinstance(player_name, dict) else None)})
            associations.append(_normalise_player_season(item, provider_season_id))
            if afl_id is None:
                self._diagnose(diagnostics, "unmapped_player",
                               "No AFL numeric ID for season player",
                               provider_season_id=provider_season_id,
                               champion_data_player_id=champion_id)
        status = "empty" if not raw_players else ("partial" if season_diagnostics else "published")
        return PlayerCollectionResult(identities, associations, diagnostics, status)

    def collect(self, *, competition_code: str = "AFL", competition_provider_id: str = "CD_C014",
                season: str | int | None = None, relevant_date: date | None = None,
                current_season_year: str | int | None = None) -> CollectionResult:
        competition = resolve_competition(
            self.competitions(), code=competition_code, provider_id=competition_provider_id
        )
        all_seasons = self.competition_seasons(competition["afl_id"])
        selected = select_season(all_seasons, selector=season, relevant_date=relevant_date)
        rounds = self.rounds(selected["afl_id"])
        teams = self.teams(selected["afl_id"])
        matches: list[dict[str, Any]] = []
        for round_record in rounds:
            # Explicitly test for None: Opening Round has the valid value zero.
            number = round_record["round_number"]
            if number is None:
                raise CollectionError(f"Round {round_record['afl_id']} has no roundNumber")
            round_matches = self.matches(competition["afl_id"], selected["afl_id"], number)
            for match in round_matches:
                # Some match payloads omit their round object. Preserve the
                # resolved collection context so persistence can always retain
                # the stable internal round relationship.
                if not isinstance(match.get("round"), dict):
                    match["round"] = {
                        "id": round_record.get("afl_id"),
                        "providerId": round_record.get("provider_id"),
                        "roundNumber": round_record.get("round_number"),
                    }
            # The supported AFL fixture payload marks finals matches with the
            # source-owned metadata.finals_match_label field.  Classify the
            # canonical round from that semantic marker, never its number/name.
            finals_flags = {
                bool(isinstance(match.get("metadata"), Mapping)
                     and match["metadata"].get("finals_match_label"))
                for match in round_matches
            }
            if len(finals_flags) > 1:
                raise CollectionError(
                    f"Round {round_record['afl_id']} mixes finals and Home & Away fixture semantics"
                )
            round_record["competition_phase"] = (
                "FINALS" if finals_flags == {True} else
                "HOME_AND_AWAY" if finals_flags == {False} else None
            )
            matches.extend(round_matches)
        current = resolve_current_season(all_seasons, selected, rounds,
                                         configured_year=current_season_year,
                                         relevant_date=relevant_date)
        return CollectionResult(competition, selected, rounds, teams, matches,
                                current_season_afl_id=current["afl_id"] if current is not None else None)


def resolve_competition(competitions: Iterable[dict[str, Any]], *, code: str | None,
                        provider_id: str | None) -> dict[str, Any]:
    values = list(competitions)
    if code is None and provider_id is None:
        raise CollectionError("Premiership competition selection requires a configured code or provider ID")

    code_matches = [item for item in values
                    if code is not None and str(item.get("code", "")).casefold() == code.casefold()]
    provider_matches = [item for item in values
                        if provider_id is not None and item.get("provider_id") == provider_id]
    if code is not None and provider_id is not None:
        # When both stable selectors are configured they describe one expected
        # identity. Requiring both prevents a stale/mistyped value from silently
        # selecting a record using only the other value.
        matches = [item for item in code_matches if item in provider_matches]
    else:
        matches = code_matches if code is not None else provider_matches
    if len(matches) == 1:
        return matches[0]
    requested = f"code={code!r}, providerId={provider_id!r}"
    if not matches:
        if code is not None and provider_id is not None and code_matches and provider_matches:
            raise CollectionError(
                f"Premiership competition selectors are inconsistent: {requested} matched different records"
            )
        raise CollectionError(f"Premiership competition was not found using {requested}; configure a stable code or provider ID")
    raise CollectionError(f"Premiership competition selection is ambiguous using {requested}; matched {len(matches)} records")


def _auto_current_candidates(values: list[Mapping[str, Any]],
                             relevant_date: date | None) -> list[Mapping[str, Any]]:
    """Season(s) matching the upstream current flag, else the relevant date's range.

    Used by :func:`select_season`'s automatic (no selector) branch to choose
    which season to collect when none is explicitly requested. This is a
    distinct concern from :func:`is_current_season`, which independently
    marks whichever season *was* collected as canonically current.
    """
    candidates = [item for item in values if item.get("current") is True]
    if not candidates:
        target = relevant_date or datetime.now(timezone.utc).date()
        candidates = [item for item in values if _contains_date(item, target)]
    return candidates


def _selector_matches(item: Mapping[str, Any], selector: str | int) -> bool:
    """Flexible identity match shared by explicit season selection and any
    other configured season identifier (year, AFL ID, provider ID, or name)."""
    text = str(selector).casefold()
    return text in {
        str(item.get("afl_id", "")).casefold(), str(item.get("provider_id", "")).casefold(),
        str(item.get("year", "")).casefold(), str(item.get("name", "")).casefold(),
        str(item.get("short_name", "")).casefold(),
    }


def select_season(seasons: Iterable[dict[str, Any]], *, selector: str | int | None = None,
                  relevant_date: date | None = None) -> dict[str, Any]:
    values = list(seasons)
    if selector is not None:
        candidates = [item for item in values if _selector_matches(item, selector)]
    else:
        candidates = _auto_current_candidates(values, relevant_date)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        instruction = f" selector {selector!r}" if selector is not None else " response flags/dates"
        raise CollectionError(f"No competition season matched{instruction}; specify --afl-season YEAR or provider ID")
    raise CollectionError(
        f"Competition season selection is ambiguous ({len(candidates)} matches); specify --afl-season YEAR or provider ID"
    )


def _season_date_window(season: Mapping[str, Any],
                        rounds: Iterable[Mapping[str, Any]]) -> tuple[date | None, date | None]:
    """The season's start/end, preferring its own fields, else its rounds' span.

    The live competition-season endpoint's payload carries neither a current
    flag nor season-level start/end dates (only id/providerId/name/shortName/
    currentRoundNumber -- see docs/investigation/afl-json/ENDPOINT_CATALOG.md
    E02). Its rounds, however, do carry real ``utcStartTime``/``utcEndTime``
    values, so the season's fixture window is derived from them when the
    season itself has none.
    """
    start = _as_date(season.get("start_time"))
    end = _as_date(season.get("end_time"))
    if start is not None and end is not None:
        return start, end
    round_starts = [value for value in (_as_date(item.get("start_time")) for item in rounds)
                    if value is not None]
    round_ends = [value for value in (_as_date(item.get("end_time")) for item in rounds)
                 if value is not None]
    return (min(round_starts) if round_starts else start,
           max(round_ends) if round_ends else end)


def is_current_season(season: Mapping[str, Any], rounds: Iterable[Mapping[str, Any]] = (), *,
                      relevant_date: date | None = None) -> bool | None:
    """Whether ``season`` is the AFL competition's current season, or None.

    Trusts an explicit upstream ``current``/``isCurrent`` boolean when
    present; otherwise falls back to whether ``relevant_date`` falls within
    the season's fixture window (see :func:`_season_date_window`). Returns
    None -- never guessed from the highest persisted ID/year -- when neither
    signal is available, so callers must not invent a current season.
    """
    flag = season.get("current")
    if isinstance(flag, bool):
        return flag
    start, end = _season_date_window(season, rounds)
    if start is None or end is None:
        return None
    target = relevant_date or datetime.now(timezone.utc).date()
    return start <= target <= end


def resolve_current_season(all_seasons: Iterable[Mapping[str, Any]], selected: Mapping[str, Any],
                           rounds: Iterable[Mapping[str, Any]] = (), *,
                           configured_year: str | int | None = None,
                           relevant_date: date | None = None) -> Mapping[str, Any] | None:
    """Resolve the AFL competition's canonical current season for persistence.

    An operator can bootstrap/sync an explicit (e.g. historical) season
    without changing which season is canonically current, so this is
    independent of ``selected`` -- the season actually collected this run.
    Precedence:

    1. An explicitly configured season identifier (e.g. the ``AFL_SEASON_YEAR``
       deployment setting), validated by resolving it uniquely against
       ``all_seasons`` -- never blindly trusted, and never assumed current
       when unset, unresolvable, or ambiguous.
    2. ``selected``'s own explicit upstream current flag or fixture date
       window (see :func:`is_current_season`).

    Returns None -- never guessed from the highest persisted ID/year --
    when neither resolves unambiguously.
    """
    if configured_year not in (None, ""):
        matches = [item for item in all_seasons if _selector_matches(item, configured_year)]
        if len(matches) == 1:
            return matches[0]
    return selected if is_current_season(selected, rounds, relevant_date=relevant_date) else None


def _contains_date(item: Mapping[str, Any], target: date) -> bool:
    start = _as_date(item.get("start_time"))
    end = _as_date(item.get("end_time"))
    return start is not None and end is not None and start <= target <= end


def _as_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _extract_collection(payload: Any, paths: tuple[str, ...], endpoint: str) -> list[Any]:
    if not isinstance(payload, dict):
        raise AflJsonInvalidResponse("AFL collection response is not an object", endpoint=endpoint)
    for path in paths:
        value: Any = payload
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                break
            value = value[part]
        else:
            if isinstance(value, list):
                return value
    raise AflJsonInvalidResponse(
        f"AFL response is missing collection path(s): {', '.join(paths)}", endpoint=endpoint
    )


def _extract_season_players(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise AflJsonInvalidResponse("Season-player response is not an object", endpoint="season_players")
    players = payload.get("players")
    if isinstance(players, list):
        return players
    if isinstance(players, dict):
        for key in ("items", "Items", "results", "Results"):
            if isinstance(players.get(key), list):
                return players[key]
    raise AflJsonInvalidResponse("Season-player response has no players collection", endpoint="season_players")


def _reported_total(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    direct = _integer(payload, "totalResults", "total", "totalCount")
    metadata = _pagination_metadata(payload)
    return direct if direct is not None else (_integer(metadata, "totalResults", "total", "totalCount")
                                               if metadata is not None else None)


def _represented_player_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    players = payload.get("players")
    if isinstance(players, dict):
        return _integer(players, "Count", "count")
    return _integer(payload, "players.Count")


def _extract_id_map_rows(payload: Any) -> list[tuple[Any, Any]]:
    if not isinstance(payload, dict):
        raise AflJsonInvalidResponse("Player ID-map response is not an object", endpoint="player_id_map")
    container = payload.get("idMapResponse")
    ids = container.get("ids") if isinstance(container, dict) else None
    if isinstance(ids, dict):
        return list(ids.items())
    if isinstance(ids, list):
        rows = []
        for item in ids:
            if isinstance(item, dict):
                rows.append((item.get("playerId", item.get("championDataPlayerId")),
                             item.get("aflPlayerId", item.get("aflId"))))
            else:
                rows.append((None, None))
        return rows
    raise AflJsonInvalidResponse("Player ID-map response has no idMapResponse.ids", endpoint="player_id_map")


def _valid_provider_player_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_afl_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _player_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        parts = [value.get("givenName"), value.get("surname")]
        name = " ".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        return name or None
    return None


def _normalise_player_season(item: dict[str, Any], provider_season_id: str) -> dict[str, Any]:
    team = item.get("team") if isinstance(item.get("team"), dict) else {}
    return {
        "champion_data_player_id": item["playerId"],
        "provider_season_id": provider_season_id,
        "team_id": team.get("teamId"),
        "team_abbreviation": team.get("teamAbbr"),
        "team_name": team.get("teamName"),
        "jumper_number": item.get("jumperNumber"),
        "listed_position": item.get("playerPosition"),
        "photo_url": item.get("photoURL"),
        "source": deepcopy(item),
    }


def _record_identity(record: Mapping[str, Any], entity: str | None) -> object:
    rule = IDENTIFIER_TYPES.get(entity or "")
    if rule:
        provider = record.get(rule.provider_field)
        numeric = record.get(rule.numeric_field) if rule.numeric_field else None
        if provider is not None or numeric is not None:
            return (numeric, provider)
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _pagination_metadata(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    candidates = [payload.get("pagination"), payload.get("pageInfo"), payload.get("meta")]
    if isinstance(payload.get("meta"), dict):
        candidates.append(payload["meta"].get("pagination"))
    return next((item for item in candidates if isinstance(item, dict)), None)


def _integer(metadata: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) or isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _next_page(payload: Any, current: int, page_count: int, unique_count: int, endpoint: str) -> int | None:
    metadata = _pagination_metadata(payload)
    if metadata is None:
        # Absence of pagination metadata denotes a single-page response; never
        # guess from requested/returned page size.
        return None
    next_value = metadata.get("nextPage") if "nextPage" in metadata else metadata.get("nextPageNum")
    if next_value is not None:
        try:
            return int(next_value)
        except (TypeError, ValueError) as error:
            raise PaginationError(f"{endpoint} returned malformed next-page metadata") from error
    total_pages = _integer(metadata, "totalPages", "pageCount", "pages")
    response_page = _integer(metadata, "pageNum", "pageNumber", "currentPage", "page")
    total_results = _integer(metadata, "totalResults", "total", "totalCount")
    if response_page is not None and response_page != current:
        raise PaginationError(f"{endpoint} requested page {current} but response identified page {response_page}")
    if total_pages is not None:
        if total_pages < current:
            raise PaginationError(f"{endpoint} returned totalPages={total_pages} on page {current}")
        return current + 1 if current < total_pages else None
    if total_results is not None:
        if total_results < unique_count:
            raise PaginationError(f"{endpoint} totalResults decreased below collected records")
        if unique_count < total_results:
            if page_count == 0:
                raise PaginationError(f"{endpoint} returned an empty page before totalResults was reached")
            return current + 1
    return None


def _base(item: dict[str, Any]) -> dict[str, Any]:
    return {"afl_id": item.get("id"), "provider_id": item.get("providerId"), "source": deepcopy(item)}


def _normalise_competition(item: dict[str, Any]) -> dict[str, Any]:
    return {**_base(item), "code": item.get("code"), "name": item.get("name"), "metadata": item.get("metadata")}


def _normalise_season(item: dict[str, Any]) -> dict[str, Any]:
    return {**_base(item), "name": item.get("name"), "short_name": item.get("shortName"),
            "year": _season_year(item), "current": item.get("current", item.get("isCurrent")),
            "current_round_number": item.get("currentRoundNumber"),
            "start_time": item.get("utcStartTime", item.get("startDate")),
            "end_time": item.get("utcEndTime", item.get("endDate")), "metadata": item.get("metadata")}


def _season_year(item: Mapping[str, Any]) -> int | None:
    """Return the explicit year, or a documented unambiguous source fallback.

    The live competition-season response has no top-level ``year`` or date: its
    name is currently shaped like ``2026 Toyota AFL Premiership``. Provider IDs
    are opaque and deliberately excluded from year extraction.
    """
    explicit = item.get("year")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and 1000 <= explicit <= 9999:
        return explicit
    if isinstance(explicit, str) and re.fullmatch(r"\d{4}", explicit):
        return int(explicit)

    named_years: set[int] = set()
    for field in ("name", "shortName"):
        value = item.get(field)
        if isinstance(value, str):
            named_years.update(int(match) for match in re.findall(r"(?<!\d)(\d{4})(?!\d)", value))
    if len(named_years) == 1:
        return named_years.pop()

    start = item.get("utcStartTime", item.get("startDate"))
    start_date = _as_date(start)
    return start_date.year if not named_years and start_date is not None else None


def _normalise_round(item: dict[str, Any]) -> dict[str, Any]:
    return {**_base(item), "name": item.get("name"), "abbreviation": item.get("abbreviation"),
            "round_number": item.get("roundNumber"), "start_time": item.get("utcStartTime"),
            "end_time": item.get("utcEndTime"), "byes": deepcopy(item.get("byes", [])),
            "metadata": item.get("metadata"), "competition_phase": None}


def _normalise_team(item: dict[str, Any]) -> dict[str, Any]:
    names = {key: item.get(key) for key in ("name", "abbreviation", "nickname", "displayName", "shortName")}
    return {**_base(item), **names, "team_type": item.get("teamType"), "metadata": deepcopy(item.get("metadata")),
            "club": deepcopy(item.get("club"))}


def _normalise_match(item: dict[str, Any]) -> dict[str, Any]:
    home = deepcopy(item.get("home"))
    away = deepcopy(item.get("away"))
    return {**_base(item), "status": item.get("status"),
            "competition_season": deepcopy(item.get("competitionSeason", item.get("compSeason"))),
            "round": deepcopy(item.get("round")), "home": home, "away": away,
            "home_team": home.get("team") if isinstance(home, dict) else None,
            "away_team": away.get("team") if isinstance(away, dict) else None,
            "home_score": home.get("score") if isinstance(home, dict) else None,
            "away_score": away.get("score") if isinstance(away, dict) else None,
            "venue": deepcopy(item.get("venue")), "utc_start_time": item.get("utcStartTime"),
            "start_time": item.get("startTime"), "metadata": deepcopy(item.get("metadata"))}
