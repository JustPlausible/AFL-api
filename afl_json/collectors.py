"""Collection and normalisation for AFL's public metadata hierarchy.

The collectors deliberately return plain dictionaries and do not write to the
application database.  Source objects are retained in ``source`` so a newly
introduced provider field is not lost while the explicit keys provide a stable
interface to downstream adapters.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .client import AflJsonClient, AflJsonInvalidResponse
from .contracts import IDENTIFIER_TYPES, Pagination, get_endpoint


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
            for record in page_records:
                if not isinstance(record, dict):
                    raise CollectionError(f"{endpoint} collection contains a non-object record")
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

    def collect(self, *, competition_code: str = "AFL", competition_provider_id: str = "CD_C014",
                season: str | int | None = None, relevant_date: date | None = None) -> CollectionResult:
        competition = resolve_competition(
            self.competitions(), code=competition_code, provider_id=competition_provider_id
        )
        selected = select_season(
            self.competition_seasons(competition["afl_id"]), selector=season,
            relevant_date=relevant_date,
        )
        rounds = self.rounds(selected["afl_id"])
        teams = self.teams(selected["afl_id"])
        matches: list[dict[str, Any]] = []
        for round_record in rounds:
            # Explicitly test for None: Opening Round has the valid value zero.
            number = round_record["round_number"]
            if number is None:
                raise CollectionError(f"Round {round_record['afl_id']} has no roundNumber")
            matches.extend(self.matches(competition["afl_id"], selected["afl_id"], number))
        return CollectionResult(competition, selected, rounds, teams, matches)


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


def select_season(seasons: Iterable[dict[str, Any]], *, selector: str | int | None = None,
                  relevant_date: date | None = None) -> dict[str, Any]:
    values = list(seasons)
    if selector is not None:
        text = str(selector).casefold()
        candidates = [item for item in values if text in {
            str(item.get("afl_id", "")).casefold(), str(item.get("provider_id", "")).casefold(),
            str(item.get("year", "")).casefold(), str(item.get("name", "")).casefold(),
            str(item.get("short_name", "")).casefold(),
        }]
    else:
        candidates = [item for item in values if item.get("current") is True]
        if not candidates:
            target = relevant_date or datetime.now(timezone.utc).date()
            candidates = [item for item in values if _contains_date(item, target)]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        instruction = f" selector {selector!r}" if selector is not None else " response flags/dates"
        raise CollectionError(f"No competition season matched{instruction}; specify --afl-season YEAR or provider ID")
    raise CollectionError(
        f"Competition season selection is ambiguous ({len(candidates)} matches); specify --afl-season YEAR or provider ID"
    )


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
            "metadata": item.get("metadata")}


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
