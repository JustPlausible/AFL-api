"""Timezone policy for AFL metadata timestamps and scheduler match days."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import config


class MetadataTimestampError(ValueError):
    """A timestamp cannot be interpreted safely under the metadata contract."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def parse_metadata_timestamp(value: object) -> datetime:
    """Parse an aware ISO-8601 metadata timestamp and return its UTC instant.

    AFL/public metadata must identify an instant. Missing, malformed, and naive
    values are rejected rather than being assigned an inferred timezone.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise MetadataTimestampError("timestamp_missing", "metadata timestamp is missing")
    if not isinstance(value, str):
        raise MetadataTimestampError("timestamp_malformed", "metadata timestamp is not a string")

    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MetadataTimestampError("timestamp_malformed", "metadata timestamp is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MetadataTimestampError("timestamp_naive", "metadata timestamp has no UTC offset")
    return parsed.astimezone(timezone.utc)


def match_day_timezone() -> ZoneInfo:
    """Return the configured IANA timezone used to define an AFL match day."""
    return ZoneInfo(config.AFL_MATCH_DAY_TIMEZONE)


def match_day_bounds(now: datetime | None = None, *, zone: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """Return UTC boundaries for the configured match-day containing ``now``."""
    zone = zone or match_day_timezone()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("match-day reference time must be timezone-aware")
    local_date = now.astimezone(zone).date()
    start_local = datetime.combine(local_date, time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
