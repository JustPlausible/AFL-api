from dataclasses import dataclass, field

@dataclass(frozen=True, slots=True)
class MovementSourceDocument:
    html: str
    source_url: str
    observed_at: str
    source_archived_at: str | None = None

@dataclass(frozen=True, slots=True)
class ParsedMovementRecord:
    player_name: str
    team_name: str
    movement_type: str
    source_label: str
    source_detail: str | None
    article_url: str | None

@dataclass(frozen=True, slots=True)
class MovementParseResult:
    records: tuple[ParsedMovementRecord, ...]
    team_count: int
    counts_by_type: dict[str, int]

@dataclass(frozen=True, slots=True)
class ResolvedMovementRecord:
    source: ParsedMovementRecord
    status: str
    canonical_player_id: int | None = None
    from_team_id: int | None = None
    reason: str | None = None

@dataclass(frozen=True, slots=True)
class MovementResolutionResult:
    records: tuple[ResolvedMovementRecord, ...]

@dataclass(frozen=True, slots=True)
class MovementImportOutcome:
    movement_season_year: int
    source_url: str
    source_archived_at: str | None
    observed_at: str
    rows_parsed: int
    rows_resolved: int
    rows_unresolved: int
    rows_ambiguous: int
    inserted: int
    updated: int
    unchanged: int
    counts_by_type: dict[str, int] = field(default_factory=dict)
    canonical_membership_mutations: int = 0
