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
                        first_updated, source, scraped_at, current
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(afl_id, updated) DO UPDATE SET
                        club=excluded.club, player_name=excluded.player_name,
                        injury=excluded.injury, return_info=excluded.return_info,
                        source=excluded.source, scraped_at=excluded.scraped_at, current=1
                """, (record.afl_id, record.club_code, source.player_name, source.injury,
                      source.estimated_return, source.updated, source.updated,
                      document.source_url, document.acquired_at))
                rows_persisted += 1
            # Preserve the safety rule: incomplete identity resolution must not
            # expire otherwise-current rows.
            if current_ids and not (rows_unresolved or rows_ambiguous):
                placeholders = ",".join("?" for _ in current_ids)
                self._conn.execute(
                    f"UPDATE injuries SET current=0 WHERE current=1 AND afl_id NOT IN ({placeholders})",
                    tuple(current_ids),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return InjuryPersistenceResult(
            len(resolved.records), rows_resolved, rows_persisted, rows_unresolved,
            rows_ambiguous, "partial" if rows_unresolved or rows_ambiguous else "success",
            resolved.diagnostics,
        )
