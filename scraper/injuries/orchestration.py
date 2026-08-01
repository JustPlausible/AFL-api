from __future__ import annotations

from db.scrape_runs import audited_scrape_run
from .acquisition import InjuryAcquirer
from .models import InjuryCollectionOutcome
from .parser import parse_injuries_html
from .persistence import InjuryPersistenceAdapter
from .resolution import InjuryResolver


def collect_injuries(conn, *, acquirer=None, parser=parse_injuries_html,
                     resolver_factory=InjuryResolver,
                     persistence_factory=InjuryPersistenceAdapter,
                     trigger_source=None, correlation_id=None) -> InjuryCollectionOutcome:
    """Own the complete injury operation and its single scrape-run lifecycle."""
    with audited_scrape_run(
        "injury", target_type="injury_list", trigger_source=trigger_source,
        correlation_id=correlation_id, conn=conn,
    ) as audit:
        document = (acquirer or InjuryAcquirer()).acquire()
        parsed = parser(document.html)
        resolved = resolver_factory(conn).resolve(parsed)
        persisted = persistence_factory(conn).persist(resolved, document)
        audit["rows_read"] = persisted.rows_parsed
        audit["rows_written"] = persisted.rows_persisted
        audit["status"] = persisted.status
        parser_diagnostics = tuple({
            "code": item.code, "message": item.message, "team_index": item.team_index
        } for item in parsed.diagnostics)
        return InjuryCollectionOutcome(
            document.source_url, document.acquired_at, document.elapsed_ms,
            parsed.team_count, persisted.rows_parsed, persisted.rows_resolved,
            persisted.rows_persisted, persisted.rows_unresolved,
            persisted.rows_ambiguous, persisted.status,
            parser_diagnostics + persisted.diagnostics,
        )
