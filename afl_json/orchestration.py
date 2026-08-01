"""Database-free orchestration for operator and integration collection runs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

from .collectors import PublicAflCollector, resolve_competition, select_season
from .contracts import get_endpoint
from .player_stats import MatchPlayerStatsCollector
from .rosters import MatchRosterCollector


SUPPORTED_FAMILIES = ("metadata", "players", "fixtures", "rosters", "lineups", "player-stats")
# Recognised names which deliberately have no maintained JSON collector yet.
UNSUPPORTED_FAMILIES = ("injuries", "commentary", "interchange")
ALL_FAMILIES = SUPPORTED_FAMILIES + UNSUPPORTED_FAMILIES


@dataclass(frozen=True, slots=True)
class CollectionRequest:
    season: str | int
    output: Path
    rounds: tuple[int, ...] = ()
    matches: tuple[str, ...] = ()
    endpoint_families: tuple[str, ...] = SUPPORTED_FAMILIES
    competition_code: str = "AFL"
    competition_provider_id: str = "CD_C014"
    mode: str = "new"


class BatchCollectionError(RuntimeError):
    """A required hierarchy resource failed and the batch cannot continue."""


class CollectionOrchestrator:
    """Compose maintained collectors and write a deterministic, database-free run."""

    def __init__(self, client: Any, *, clock: Callable[[], datetime] | None = None):
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, request: CollectionRequest) -> dict[str, Any]:
        output = Path(request.output)
        self._prepare_output(output, request.mode)
        raw = output / "raw"
        normalised = output / "normalised"
        collector = PublicAflCollector(self.client, raw_directory=raw)
        resources: list[dict[str, Any]] = []
        started = self.clock().astimezone(timezone.utc).isoformat()
        metadata = self._request_metadata(request, started)
        self._write(output / "request.json", metadata)

        requested = tuple(dict.fromkeys(request.endpoint_families))
        unknown = sorted(set(requested) - set(ALL_FAMILIES))
        if unknown:
            raise ValueError(f"unknown endpoint family: {', '.join(unknown)}")
        for family in requested:
            if family in UNSUPPORTED_FAMILIES:
                resources.append(self._resource(family, family, "skipped", "unsupported endpoint family"))

        try:
            competition = resolve_competition(
                collector.competitions(), code=request.competition_code,
                provider_id=request.competition_provider_id,
            )
            season = select_season(
                collector.competition_seasons(competition["afl_id"]), selector=request.season
            )
            need_rounds = bool({"metadata", "fixtures", "rosters", "lineups", "player-stats"}
                               & set(requested) or request.rounds or request.matches)
            rounds = collector.rounds(season["afl_id"]) if need_rounds else []
            teams = collector.teams(season["afl_id"]) if "metadata" in requested else []
            selected_rounds = self._select_rounds(rounds, request.rounds)
            matches: list[dict[str, Any]] = []
            need_matches = bool({"metadata", "fixtures", "player-stats"} & set(requested)
                                or request.matches)
            for round_record in selected_rounds if need_matches else ():
                number = round_record.get("round_number")
                if number is None:
                    raise BatchCollectionError(f"round {round_record.get('afl_id')} has no round number")
                for match in collector.matches(competition["afl_id"], season["afl_id"], number):
                    if not isinstance(match.get("round"), dict):
                        match["round"] = {"id": round_record.get("afl_id"),
                                          "providerId": round_record.get("provider_id"),
                                          "roundNumber": number}
                    matches.append(match)
            selected_matches = self._select_matches(matches, request.matches)
        except Exception as exc:
            resources.append(self._resource("metadata", str(request.season), "failed", self._safe_error(exc)))
            summary = self._summary(metadata, resources, batch_failed=True)
            self._write(output / "summary.json", summary)
            raise BatchCollectionError(self._safe_error(exc)) from exc

        if "metadata" in requested:
            self._normalised(normalised, "metadata", "competition", competition, metadata, resources)
            self._normalised(normalised, "metadata", "season", season, metadata, resources)
            self._normalised(normalised, "metadata", "teams", teams, metadata, resources)
        if "fixtures" in requested:
            self._normalised(normalised, "fixtures", "rounds", selected_rounds, metadata, resources)
            self._normalised(normalised, "fixtures", "matches", selected_matches, metadata, resources)

        if "players" in requested:
            try:
                players = collector.collect_players(season["provider_id"])
                self._normalised(normalised, "players", "players", players.players, metadata, resources)
                self._normalised(normalised, "players", "player-seasons", players.player_seasons,
                                 metadata, resources, status=players.status)
            except Exception as exc:
                resources.append(self._resource("players", season["provider_id"], "failed",
                                                self._safe_error(exc)))

        roster_results: dict[str, Any] = {}
        if {"rosters", "lineups"} & set(requested):
            for round_record in self._rounds_for_matches(selected_rounds, selected_matches):
                identifier = round_record.get("provider_id")
                if not identifier:
                    resources.append(self._resource("rosters", str(round_record.get("round_number")),
                                                    "skipped", "round has no provider ID"))
                    continue
                try:
                    result = MatchRosterCollector(self.client, raw_directory=raw).collect(identifier)
                    roster_results[identifier] = result
                    if "rosters" in requested:
                        self._normalised(normalised, "rosters", identifier, result.rosters,
                                         metadata, resources, status=result.status.value)
                    if "lineups" in requested:
                        self._normalised(normalised, "lineups", identifier, result.selections,
                                         metadata, resources, status=result.status.value)
                except Exception as exc:
                    for family in ({"rosters", "lineups"} & set(requested)):
                        resources.append(self._resource(family, identifier, "failed", self._safe_error(exc)))

        if "player-stats" in requested:
            for match in selected_matches:
                identifier = match.get("provider_id")
                if not identifier:
                    resources.append(self._resource("player-stats", str(match.get("afl_id")),
                                                    "skipped", "match has no provider ID"))
                    continue
                try:
                    result = MatchPlayerStatsCollector(self.client, raw_directory=raw).collect(
                        identifier, afl_match_id=match.get("afl_id"),
                        canonical_match_status=match.get("status"),
                    )
                    self._normalised(normalised, "player-stats", identifier, result,
                                     metadata, resources, status=result.status.value)
                except Exception as exc:
                    resources.append(self._resource("player-stats", identifier, "failed",
                                                    self._safe_error(exc)))

        summary = self._summary(metadata, resources,
                                batch_failed=any(item["status"] == "failed" for item in resources))
        self._write(output / "summary.json", summary)
        return summary

    @staticmethod
    def _prepare_output(output: Path, mode: str) -> None:
        if mode not in {"new", "overwrite", "resume"}:
            raise ValueError("mode must be new, overwrite or resume")
        if output.exists() and mode == "new" and any(output.iterdir()):
            raise FileExistsError(f"output directory is not empty: {output}; use --overwrite or --resume")
        output.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _select_rounds(rounds: list[dict[str, Any]], requested: Iterable[int]) -> list[dict[str, Any]]:
        wanted = set(requested)
        selected = [item for item in rounds if not wanted or item.get("round_number") in wanted]
        missing = wanted - {item.get("round_number") for item in selected}
        if missing:
            raise BatchCollectionError(f"round(s) not found: {', '.join(map(str, sorted(missing)))}")
        return selected

    @staticmethod
    def _select_matches(matches: list[dict[str, Any]], requested: Iterable[str]) -> list[dict[str, Any]]:
        wanted = {str(item) for item in requested}
        selected = [item for item in matches if not wanted or
                    str(item.get("afl_id")) in wanted or str(item.get("provider_id")) in wanted]
        found = {value for item in selected for value in
                 (str(item.get("afl_id")), str(item.get("provider_id")))}
        missing = wanted - found
        if missing:
            raise BatchCollectionError(f"match(es) not found: {', '.join(sorted(missing))}")
        return selected

    @staticmethod
    def _rounds_for_matches(rounds: list[dict[str, Any]], matches: list[dict[str, Any]]):
        numbers = {item.get("round", {}).get("roundNumber") for item in matches
                   if isinstance(item.get("round"), dict)}
        return [item for item in rounds if not matches or item.get("round_number") in numbers]

    def _normalised(self, root: Path, family: str, identifier: str, data: Any,
                    metadata: dict[str, Any], resources: list[dict[str, Any]], *, status="successful"):
        path = root / family / f"{identifier}.json"
        self._write(path, {"metadata": {**metadata, "endpoint_family": family}, "data": data})
        resources.append(self._resource(family, identifier, "successful", None,
                                        path.as_posix()))

    @staticmethod
    def _resource(family: str, identifier: str, status: str, detail: str | None,
                  path: str | None = None) -> dict[str, Any]:
        return {"resource_type": family, "identifier": identifier, "status": status,
                "detail": detail, "path": path}

    @staticmethod
    def _request_metadata(request: CollectionRequest, started: str) -> dict[str, Any]:
        return {"schema_version": 1, "collected_at": started, "database_writes": False,
                "request": {"season": request.season, "rounds": list(request.rounds),
                            "matches": list(request.matches),
                            "endpoint_families": list(request.endpoint_families)},
                "sources": {name: {"endpoint": get_endpoint(name).path_template,
                                   "authentication": "required" if get_endpoint(name).requires_auth else "none"}
                            for name in ("competitions", "competition_seasons", "rounds", "teams",
                                         "matches", "player_id_map", "season_players", "match_rosters",
                                         "match_player_statistics")}}

    @staticmethod
    def _summary(metadata: dict[str, Any], resources: list[dict[str, Any]], *, batch_failed: bool):
        grouped = {status: {} for status in ("successful", "skipped", "failed")}
        for item in resources:
            bucket = grouped[item["status"]]
            bucket[item["resource_type"]] = bucket.get(item["resource_type"], 0) + 1
        return {"metadata": metadata, "status": "failed" if batch_failed else "successful",
                "counts": grouped, "resources": resources}

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        # Shared AFL exceptions are intentionally body-free. Avoid serialising
        # repr/attributes from arbitrary transports, where credentials may live.
        return f"{type(exc).__name__}: collection failed"

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True,
                                        default=_json_default) + "\n", encoding="utf-8")
        os.replace(temporary, path)


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
