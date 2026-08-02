"""Lightweight, reusable diagnostics for collection boundaries.

This module intentionally imports no collectors, database code, or application
configuration so parser-only CLI operations remain cheap to import.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any, Mapping


_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|authorization|cookie|password|secret|token)"
    r"\b\s*[:=]\s*([^\s,;&]+)"
)


def _safe_text(value: str | None) -> str | None:
    return _SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", value) if value else value


class DiagnosticStatus(str, Enum):
    SUCCESS = "success"
    UNCHANGED = "unchanged"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"
    LIVE_PARTIAL = "live_partial"
    CONCLUDED = "concluded"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"
    FAILED = "failed"


class DiagnosticMode(str, Enum):
    READ_ONLY = "read_only"
    DATABASE_FREE = "database_free"
    PERSISTENT = "persistent"
    LEGACY_PERSISTENT = "legacy_persistent"
    COMPOSITE = "composite"


@dataclass(frozen=True, slots=True)
class CollectionDiagnostic:
    """Stable source/persistence envelope assembled at an operation boundary.

    ``None`` is the sole representation for unavailable or non-applicable
    values. Counts must only be supplied when an underlying result supports
    them; in particular, zero means a measured zero rather than "unknown".
    """

    operation: str
    domain: str
    source_family: str
    collector: str
    mode: str
    database_opened: bool
    persistence_target: str | None
    result_status: str
    fallback_allowed: bool
    fallback_occurred: bool
    source_endpoint: str | None = None
    persistence_action: str | None = None
    records_received: int | None = None
    records_normalised: int | None = None
    records_rejected: int | None = None
    rows_inserted: int | None = None
    rows_updated: int | None = None
    rows_unchanged: int | None = None
    rows_written: int | None = None
    result_detail: str | None = None
    diagnostic_count: int | None = None
    season_id: int | str | None = None
    round_id: int | str | None = None
    match_id: int | str | None = None
    provider_match_id: str | None = None
    audit_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if self.result_status not in {status.value for status in DiagnosticStatus}:
            raise ValueError(f"unsupported diagnostic status: {self.result_status}")
        if self.mode not in {mode.value for mode in DiagnosticMode}:
            raise ValueError(f"unsupported diagnostic mode: {self.mode}")
        if not self.database_opened and self.persistence_target not in (None, "none"):
            raise ValueError("database-free diagnostics cannot report a database persistence target")
        object.__setattr__(self, "source_endpoint", _safe_text(self.source_endpoint))
        object.__setattr__(self, "result_detail", _safe_text(self.result_detail))

    def to_dict(self, *, omit_none: bool = False,
                details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return deterministic, JSON-ready fields with optional domain details."""
        values = asdict(self)
        if omit_none:
            values = {key: value for key, value in values.items() if value is not None}
        if details:
            conflicts = set(values).intersection(details)
            if conflicts:
                raise ValueError(f"domain details conflict with diagnostic fields: {sorted(conflicts)}")
            values.update(details)
        return values


def human_summary(diagnostic: CollectionDiagnostic) -> str:
    """Render the concise operator summary from the same envelope as JSON."""
    return "\n".join(
        f"{key}={str(value).lower() if isinstance(value, bool) else value}"
        for key, value in diagnostic.to_dict(omit_none=True).items()
    )
