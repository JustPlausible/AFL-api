# utils/time_format.py
"""Shared normalisation for persisted UTC ISO-8601 timestamps."""
from __future__ import annotations

from datetime import datetime, timezone


def normalize_utc_iso(value: str | None) -> str | None:
    """Return `value` as a canonical UTC ISO-8601 string ending in 'Z'.

    Accepts any ISO-8601 string with an explicit UTC offset (including a
    trailing 'Z') and returns the same instant as
    `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`. Performs no timezone inference: the
    input must already carry an explicit offset.
    """
    if not value:
        return value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"expected a timezone-aware timestamp, got {value!r}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
