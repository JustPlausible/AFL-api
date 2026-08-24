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
class ParsedTeamBlock:
    """One source team block's observed coverage, independent of row count.

    Every team block recognised on the page produces exactly one of these,
    whether or not it has any player rows -- a team block with zero rows is
    an authoritative empty injury list for that team, which is different
    from a team not appearing on the page at all. Persistence relies on
    this explicit list rather than inferring coverage from ``records``.
    """

    team_index: int
    club_image_src: str
    club_image_alt: str
    updated: str
    row_count: int


@dataclass(frozen=True, slots=True)
class InjuryParseResult:
    records: tuple[ParsedInjuryRecord, ...]
    team_count: int
    diagnostics: tuple[InjuryParserDiagnostic, ...] = ()
    teams: tuple[ParsedTeamBlock, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedInjuryRecord:
    source: ParsedInjuryRecord
    status: str
    club_code: str | None = None
    canonical_player_id: int | None = None
    afl_id: int | None = None
    reason: str | None = None
    canonical_team_id: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedTeamCoverage:
    """Canonical identity for one observed source team block.

    ``status`` is ``"resolved"`` only when the team block's own club marker
    resolves to a canonical club/team -- an unresolved club marker must not
    be used to scope persistence expiry, because it is not safely known
    which team the block represents.
    """

    team_index: int
    status: str
    club_code: str | None = None
    canonical_team_id: int | None = None
    row_count: int = 0
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InjuryResolutionResult:
    records: tuple[ResolvedInjuryRecord, ...]
    diagnostics: tuple[dict, ...] = ()
    observed_teams: tuple[ResolvedTeamCoverage, ...] = ()


@dataclass(frozen=True, slots=True)
class InjuryPersistenceResult:
    rows_parsed: int
    rows_resolved: int
    rows_persisted: int
    rows_unresolved: int
    rows_ambiguous: int
    status: str
    diagnostics: tuple[dict, ...] = ()
    teams_observed: int = 0


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
    teams_observed: int = 0
