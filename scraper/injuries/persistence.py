from __future__ import annotations

from .models import InjuryPersistenceResult, InjuryResolutionResult, InjurySourceDocument


class InjuryPersistenceAdapter:
    """Transaction-owning writer for already-resolved injury records."""

    def __init__(self, conn):
        self._conn = conn

    def persist(self, resolved: InjuryResolutionResult,
                document: InjurySourceDocument) -> InjuryPersistenceResult:
        rows_resolved = rows_persisted = rows_unresolved = rows_ambiguous = 0
        current_ids = set()
        try:
            for record in resolved.records:
                if record.status != "resolved" or record.afl_id is None:
                    if record.status == "ambiguous":
                        rows_ambiguous += 1
                    else:
                        rows_unresolved += 1
                    continue
                rows_resolved += 1
                current_ids.add(record.afl_id)
                source = record.source
                self._conn.execute("""
                    INSERT INTO injuries (
                        afl_id, club, player_name, injury, return_info, updated,
                        first_updated, source, scraped_at, current,
                        canonical_player_id, canonical_team_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(afl_id, updated) DO UPDATE SET
                        club=excluded.club, player_name=excluded.player_name,
                        injury=excluded.injury, return_info=excluded.return_info,
                        source=excluded.source, scraped_at=excluded.scraped_at, current=1,
                        canonical_player_id=excluded.canonical_player_id,
                        canonical_team_id=excluded.canonical_team_id
                """, (record.afl_id, record.club_code, source.player_name, source.injury,
                      source.estimated_return, source.updated, source.updated,
                      document.source_url, document.acquired_at,
                      record.canonical_player_id, record.canonical_team_id))
                rows_persisted += 1

            # Observed-team authority: a team block present on the page (with
            # a resolvable club identity) may expire its own previously-current
            # rows that are no longer listed, including down to zero when the
            # block has no rows at all -- that is an authoritative empty list,
            # not missing information. A team absent from the page, or whose
            # club marker itself did not resolve, is left untouched: absence
            # is not evidence of zero injuries. Preserve the existing safety
            # rule that any unresolved/ambiguous identity anywhere on the page
            # blocks expiry entirely, rather than scoping that guard per team.
            observed_team_ids = {
                team.canonical_team_id for team in resolved.observed_teams
                if team.status == "resolved" and team.canonical_team_id is not None
            }
            if observed_team_ids and not (rows_unresolved or rows_ambiguous):
                team_placeholders = ",".join("?" for _ in observed_team_ids)
                if current_ids:
                    id_placeholders = ",".join("?" for _ in current_ids)
                    self._conn.execute(
                        "UPDATE injuries SET current=0 WHERE current=1 "
                        f"AND canonical_team_id IN ({team_placeholders}) "
                        f"AND afl_id NOT IN ({id_placeholders})",
                        (*observed_team_ids, *current_ids),
                    )
                else:
                    self._conn.execute(
                        "UPDATE injuries SET current=0 WHERE current=1 "
                        f"AND canonical_team_id IN ({team_placeholders})",
                        tuple(observed_team_ids),
                    )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return InjuryPersistenceResult(
            len(resolved.records), rows_resolved, rows_persisted, rows_unresolved,
            rows_ambiguous, "partial" if rows_unresolved or rows_ambiguous else "success",
            resolved.diagnostics, len(observed_team_ids),
        )
