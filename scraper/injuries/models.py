from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class InjurySourceDocument:
    html: str
    source_url: str
    acquired_at: str
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class ParsedInjuryRecord:
    player_name: str
    injury: str
    estimated_return: str
    updated: str
    club_image_src: str
    club_image_alt: str


@dataclass(frozen=True, slots=True)
class InjuryParserDiagnostic:
    code: str
    message: str
    team_index: int | None = None


@dataclass(frozen=True, slots=True)
class InjuryParseResult:
    records: tuple[ParsedInjuryRecord, ...]
    team_count: int
    diagnostics: tuple[InjuryParserDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedInjuryRecord:
    source: ParsedInjuryRecord
    status: str
    club_code: str | None = None
    canonical_player_id: int | None = None
    afl_id: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InjuryResolutionResult:
    records: tuple[ResolvedInjuryRecord, ...]
    diagnostics: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class InjuryPersistenceResult:
    rows_parsed: int
    rows_resolved: int
    rows_persisted: int
    rows_unresolved: int
    rows_ambiguous: int
    status: str
    diagnostics: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class InjuryCollectionOutcome:
    source_url: str
    acquired_at: str
    acquisition_elapsed_ms: int
    team_count: int
    rows_parsed: int
    rows_resolved: int
    rows_persisted: int
    rows_unresolved: int
    rows_ambiguous: int
    status: str
    diagnostics: tuple[dict, ...] = field(default_factory=tuple)
