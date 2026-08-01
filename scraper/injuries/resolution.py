from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from merge.helpers import build_canonical_injury_player_resolver
from utils.club_lookup import get_canonical_club, resolve_club_code
from .models import InjuryParseResult, InjuryResolutionResult, ResolvedInjuryRecord


def resolve_source_club(image_src: str, alt_text: str = "") -> dict | None:
    """Resolve maintained club identifiers without guessing from a player name."""
    if alt_text and (code := resolve_club_code(alt_text)):
        if club := get_canonical_club(code):
            return club
    stem = Path(unquote(urlsplit(image_src).path)).stem
    for token in re.split(r"[_\-\s]+", stem):
        if club := get_canonical_club(token):
            return club
    cleaned = re.sub(r"[^a-z]", "", stem.casefold())
    return get_canonical_club(cleaned)


class InjuryResolver:
    def __init__(self, conn, *, club_resolver=resolve_source_club, player_resolver=None):
        self._club_resolver = club_resolver
        self._player_resolver = player_resolver or build_canonical_injury_player_resolver(conn)

    def resolve(self, parsed: InjuryParseResult) -> InjuryResolutionResult:
        records, diagnostics = [], []
        for source in parsed.records:
            club = self._club_resolver(source.club_image_src, source.club_image_alt)
            if club is None:
                result = ResolvedInjuryRecord(
                    source, "unresolved", reason="source club marker is not canonical"
                )
            else:
                identity = self._player_resolver.resolve(source.player_name, club["code"])
                result = ResolvedInjuryRecord(
                    source, identity.status, club["code"], identity.canonical_player_id,
                    identity.afl_id, identity.reason,
                )
            records.append(result)
            if result.status != "resolved":
                diagnostics.append({
                    "player_name": source.player_name,
                    "club": result.club_code,
                    "status": result.status,
                    "reason": result.reason,
                })
        return InjuryResolutionResult(tuple(records), tuple(diagnostics))
