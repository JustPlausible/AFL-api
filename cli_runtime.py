"""Operation-specific CLI handlers.

This module itself is lightweight. Each handler imports only the runtime family
needed for the selected operation.
"""
import json
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path

from collection.diagnostics import CollectionDiagnostic


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _diagnostic_output(diagnostic, *, details=None, pretty=False):
    """Serialize one envelope and its non-conflicting domain-specific details."""
    return json.dumps(diagnostic.to_dict(details=details), default=_json_default,
                      indent=2 if pretty else None)


def _season_sync_summary(result):
    lines = [
        f"AFL season sync {result.requested_season}: {result.outcome}",
        ("matches: "
         f"selected={result.total_matches_discovered} eligible={result.eligible_matches} "
         f"collected={result.collected_successfully} "
         f"already_complete={result.already_complete_unchanged} "
         f"stats_not_expected={result.stats_not_expected} "
         f"selection={result.selection_status}"),
        ("skipped: "
         f"scheduled={result.skipped_scheduled} "
         f"live_or_postgame={result.skipped_live_or_postgame} "
         f"future_placeholder={result.skipped_future_placeholder} "
         f"missing_provider={result.skipped_missing_provider_identity}"),
        ("material: "
         f"unavailable={result.unavailable_unpublished} empty={result.empty} "
         f"partial={result.partial} unknown={result.unknown} "
         f"unresolved_lifecycle={result.unresolved_lifecycle} failed={result.failed} "
         f"explicit_unsatisfied={result.explicit_matches_unsatisfied}"),
        ("statistic rows: "
         f"inserted={result.statistic_rows_inserted} "
         f"updated={result.statistic_rows_updated} "
         f"unchanged={result.statistic_rows_unchanged}"),
        (f"audit: outcome={result.audit_outcome} failures={result.audit_failures} "
         f"run={result.audit_id} correlation={result.correlation_id}"),
    ]
    actionable = [
        f"{match.match_id}:{match.outcome}" for match in result.matches
        if match.outcome not in {
            "collected", "already_complete", "scheduled", "live_or_postgame",
            "future_placeholder",
            "stats_not_expected",
        }
    ]
    if actionable:
        lines.append("actionable matches: " + ", ".join(actionable))
    return "\n".join(lines)


def import_clubs_to_db():
    """Load clubs from the canonical seed and import using a shared connection."""
    from db.club_seed import upsert_club_seed
    from db.connection import get_db_connection
    from utils.log import log

    conn = get_db_connection()
    count = upsert_club_seed(conn)
    conn.commit()
    conn.close()
    log(f"✅ Imported {count} canonical clubs into DB", "SUCCESS")


def _scrape_all_clubs(skip_existing=False):
    from scraper.scrape_afl_clubs import save_club_players_to_json
    from utils.club_lookup import load_clubs

    summaries = [save_club_players_to_json(club, skip_existing=skip_existing)
                 for club in load_clubs()]
    print("\n📊 Scrape Summary:")
    print(f"{'Club':<30} {'Total':>5}  {'Missing Image':>14}  {'Missing CD ID':>15}  {'Missing Club ID':>15}")
    print("-" * 85)
    for summary in summaries:
        print(f"{summary['club']:<30} {summary['total']:>5}  {summary['missing_image']:>14}  "
              f"{summary['missing_champion_id']:>15}  {summary['missing_club_id']:>15}")


def _enrich_all_clubs():
    from merge.helpers import resolve_players_for_club

    for path in sorted(Path("data").glob("players-*-raw.json")):
        resolve_players_for_club(path.stem.replace("players-", "").replace("-raw", ""))


def handle_collect_afl_data(args):
    from afl_json import (AflJsonClient, BatchCollectionError,
                          CollectionOrchestrator, CollectionRequest)

    families = (tuple(item.strip() for item in args.collection_endpoints.split(",")
                      if item.strip()) if args.collection_endpoints else
                ("metadata", "players", "fixtures", "rosters", "lineups", "player-stats"))
    mode = "overwrite" if args.collection_overwrite else ("resume" if args.collection_resume else "new")
    request = CollectionRequest(
        season=args.afl_season, output=args.collection_output,
        rounds=tuple(args.collection_round), matches=tuple(args.collection_match),
        endpoint_families=families, competition_code=args.afl_competition_code,
        competition_provider_id=args.afl_competition_provider_id, mode=mode,
    )
    try:
        with AflJsonClient() as client:
            summary = CollectionOrchestrator(client).run(request)
    except (BatchCollectionError, FileExistsError, ValueError) as exc:
        diagnostic = CollectionDiagnostic(
            operation="collect_afl_data", domain="orchestration",
            source_family="public_afl_json+cfs_json", collector="CollectionOrchestrator",
            mode="database_free", database_opened=False, persistence_target="none",
            result_status="failed", result_detail=exc.__class__.__name__,
            fallback_allowed=False, fallback_occurred=False,
        )
        print(_diagnostic_output(diagnostic), file=sys.stderr)
        raise SystemExit(1) from None
    status = summary.get("status", "unknown")
    common_status = "success" if status in {"success", "successful"} else status
    diagnostic = CollectionDiagnostic(
        operation="collect_afl_data", domain="orchestration",
        source_family="public_afl_json+cfs_json", collector="CollectionOrchestrator",
        mode="database_free", database_opened=False, persistence_target="none",
        result_status=common_status if common_status in {"success", "partial", "failed", "skipped"} else "unknown",
        fallback_allowed=False, fallback_occurred=False,
        season_id=args.afl_season,
    )
    print(_diagnostic_output(diagnostic, details=summary))
    if summary["status"] == "failed":
        raise SystemExit(1)


def _print_statspro_report(report):
    print(
        f"StatsPro {report.source_context} {report.provider_id}: success\n"
        f"players: returned={report.players_returned} resolved={report.players_resolved} "
        f"unresolved={report.players_unresolved} zero_game={report.zero_game_players}\n"
        f"rows: inserted={report.inserted} updated={report.updated} unchanged={report.unchanged}\n"
        f"collected_at: {report.collected_at}"
    )


def _resolve_statspro_season(conn, *, year, competition_code, competition_provider_id):
    """Resolve one persisted season through the configured AFL competition identity."""
    competitions = conn.execute(
        "SELECT afl_id FROM afl_competitions WHERE code=? AND provider_id=? ORDER BY afl_id",
        (competition_code, competition_provider_id),
    ).fetchall()
    if len(competitions) != 1:
        raise ValueError(
            "configured AFL competition could not be resolved uniquely "
            f"(code={competition_code!r}, provider_id={competition_provider_id!r})"
        )
    seasons = conn.execute(
        "SELECT afl_id,provider_id FROM afl_seasons WHERE competition_id=? AND year=? ORDER BY afl_id",
        (competitions[0]["afl_id"], year),
    ).fetchall()
    if len(seasons) != 1:
        raise ValueError(
            f"AFL season {year} could not be resolved uniquely for the configured competition"
        )
    if not seasons[0]["provider_id"]:
        raise ValueError("season is persisted but has no provider ID")
    return seasons[0]


def handle_collect_statspro_season(args):
    from afl_json.client import AflJsonClient
    from afl_json.statspro import StatsProCollector, persist_season
    from db.connection import get_db_connection
    conn = get_db_connection()
    try:
        season = _resolve_statspro_season(
            conn, year=args.collect_statspro_season,
            competition_code=args.afl_competition_code,
            competition_provider_id=args.afl_competition_provider_id,
        )
        with AflJsonClient() as client:
            records, collected_at = StatsProCollector(client).fetch_season(season["provider_id"])
        report = persist_season(conn, records, season_id=season["afl_id"],
                                season_provider_id=season["provider_id"], collected_at=collected_at)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"StatsPro SEASON_TOTAL collection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        conn.close()
    _print_statspro_report(report)


def handle_collect_statspro_round(args):
    from afl_json.client import AflJsonClient
    from afl_json.statspro import StatsProCollector, persist_round
    from db.connection import get_db_connection
    conn = get_db_connection()
    try:
        round_row = conn.execute("SELECT round_id,season_id FROM rounds WHERE provider_id=?", (args.collect_statspro_round,)).fetchone()
        if round_row is None:
            raise ValueError("round provider ID is not persisted")
        with AflJsonClient() as client:
            records, collected_at = StatsProCollector(client).fetch_round(args.collect_statspro_round)
        report = persist_round(conn, records, season_id=round_row["season_id"], round_id=round_row["round_id"],
                               round_provider_id=args.collect_statspro_round, collected_at=collected_at)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"StatsPro LEAGUE_ROUND_TOTAL collection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        conn.close()
    _print_statspro_report(report)


def handle_build_player_stat_summaries(args):
    from afl_json.home_away_summaries import (SummaryNotReady,
        build_home_and_away_player_summaries)
    from db.connection import get_db_connection
    conn = get_db_connection()
    try:
        season = _resolve_statspro_season(
            conn, year=args.build_player_stat_summaries,
            competition_code=args.afl_competition_code,
            competition_provider_id=args.afl_competition_provider_id,
        )
        report = build_home_and_away_player_summaries(conn, season["afl_id"])
    except SummaryNotReady as exc:
        conn.rollback()
        print(f"Season: {args.build_player_stat_summaries}\nScope: home_and_away\n"
              f"Status: NOT READY\n{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        conn.close()
    print(
        f"Season: {args.build_player_stat_summaries}\nScope: {report.scope}\n"
        f"H&A matches selected: {report.matches_selected}\n"
        f"Authoritative match-stat snapshots included: {report.authoritative_snapshots}\n"
        f"Reviewed stats-not-expected fixtures: {report.reviewed_exceptions}\n"
        f"Season player population: {report.population}\nPlayers with >= 1 H&A game: {report.players_with_games}\n"
        f"Zero-game players retained: {report.zero_game_players}\n"
        f"Rows: inserted={report.inserted} updated={report.updated} unchanged={report.unchanged}\n"
        f"Unsupported aggregation fields: {', '.join(report.unsupported_fields)}\n"
        f"Source max updated-at: {report.source_max_updated_at}\nStatus: {report.status}"
    )


def handle_scrape_club(args):
    from scraper.scrape_afl_clubs import save_club_players_to_json
    from utils.club_lookup import get_club
    from utils.log import log

    club = get_club(args.scrape_club.lower())
    if club:
        save_club_players_to_json(club)
    else:
        log(f"❌ Unknown club: {args.scrape_club}", "ERROR")


def handle_scrape_clubs(args):
    _scrape_all_clubs(skip_existing=args.skip_existing)


def handle_enrich_club(args):
    from merge.helpers import resolve_players_for_club
    resolve_players_for_club(args.enrich_club.lower())


def handle_enrich_clubs(args):
    _enrich_all_clubs()


def handle_scrape_enrich_all(args):
    from db.import_to_db import import_players
    _scrape_all_clubs(skip_existing=args.skip_existing)
    _enrich_all_clubs()
    import_players()


def handle_scrape_injuries(args):
    from collection.source_policy import OperationalDomain, collect_operational
    from db.scrape_runs import TRIGGER_CLI

    outcome = collect_operational(OperationalDomain.INJURIES, trigger_source=TRIGGER_CLI)
    diagnostic = CollectionDiagnostic(
        operation="scrape_injuries", domain="injuries", source_family="html",
        collector=outcome.collector, mode="persistent", database_opened=True,
        persistence_target="injuries,injury_history", persistence_action="upsert",
        records_received=outcome.rows_read, rows_written=outcome.rows_written,
        result_status=outcome.status, fallback_allowed=False, fallback_occurred=False,
        diagnostic_count=len((outcome.details or {}).get("diagnostics", ())),
    )
    print(_diagnostic_output(diagnostic, details=outcome.details,
                             pretty=args.print_json))


def handle_scrape_lineups(args):
    from db.connection import get_db_connection
    from db.import_to_db import save_lineups_to_db
    from db.scrape_runs import audited_scrape_run
    from scraper.scrape_afl_lineups import scrape_team_lineups
    from utils.log import log

    log(f"🧹 Scraping team lineups for Round {args.scrape_lineups}", "INFO")
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    with audited_scrape_run("lineup", target_type="round",
                            target_identifier=args.scrape_lineups, conn=conn) as audit:
        players = scrape_team_lineups(round_number=args.scrape_lineups)
        audit["rows_read"] = len(players)
        save_lineups_to_db(players, conn, args.scrape_lineups)
        audit["rows_written"] = len(players)
        diagnostic = CollectionDiagnostic(
            operation="scrape_lineups", domain="lineups", source_family="html",
            collector="scraper.scrape_afl_lineups", mode="persistent",
            database_opened=True, persistence_target="lineups,player_lineups",
            persistence_action="upsert", records_received=len(players),
            rows_written=len(players), result_status="success" if players else "empty",
            fallback_allowed=False, fallback_occurred=False,
            round_id=args.scrape_lineups, audit_id=audit["run_id"],
        )
        details = {"records": players} if args.print_json else None
        print(_diagnostic_output(diagnostic, details=details, pretty=args.print_json))
    conn.close()


def handle_import_clubs(_args):
    from utils.log import log
    log("📥 Importing clubs from JSON to DB...", "INFO")
    import_clubs_to_db()


def handle_export_clubs(_args):
    from db.import_to_db import export_clubs_from_db
    from utils.log import log
    log("📤 Exporting clubs from DB to backup JSON...", "INFO")
    export_clubs_from_db()


def _run_api_key_operation(operation, *operation_args, **operation_kwargs):
    """Convert a missing configured database into a clean, non-zero exit.

    ``scripts.manage_api_keys`` opens the database through the shared
    ``db.connection.get_db_connection`` policy, which raises
    ``FileNotFoundError`` rather than silently creating an unintended
    database at an incorrectly configured ``DB_PATH``. That failure is
    already logged clearly by the connection helper; this only avoids
    surfacing a raw traceback for it.
    """
    try:
        operation(*operation_args, **operation_kwargs)
    except FileNotFoundError:
        raise SystemExit(1) from None


def handle_add_api_key(args):
    from scripts.manage_api_keys import add_api_key
    _run_api_key_operation(add_api_key, args.add_api_key)


def handle_list_api_keys(_args):
    from scripts.manage_api_keys import list_api_keys
    _run_api_key_operation(list_api_keys)


def handle_remove_api_key(args):
    from scripts.manage_api_keys import remove_api_key
    _run_api_key_operation(remove_api_key, args.remove_api_key)


def handle_grant_api_key_capability(args):
    from scripts.manage_api_keys import set_api_key_capability
    _run_api_key_operation(set_api_key_capability, *args.grant_api_key_capability, grant=True)


def handle_revoke_api_key_capability(args):
    from scripts.manage_api_keys import set_api_key_capability
    _run_api_key_operation(set_api_key_capability, *args.revoke_api_key_capability, grant=False)


def handle_scrape_round(args):
    from scraper import scrape_afl_matches
    from utils.log import log
    log(f"📥 Scraping match data for round_id {args.scrape_round}", "INFO")
    scrape_afl_matches.run(round_id=args.scrape_round)


def handle_scrape_all_rounds(_args):
    from scraper import scrape_afl_matches
    from utils.log import log
    log("📥 Scraping all match data from DB rounds...", "INFO")
    scrape_afl_matches.run(round_id=None)


def handle_scrape_match(args):
    from scraper import scrape_afl_player_stats
    from utils.log import log
    diagnostic = CollectionDiagnostic(
        operation="scrape_match", domain="match_player_stats", source_family="html",
        collector="scraper.scrape_afl_player_stats", mode="legacy_persistent",
        database_opened=True, persistence_target="player_stats", persistence_action="upsert",
        result_status="success", fallback_allowed=False, fallback_occurred=False,
        match_id=args.scrape_match,
    )
    log("📊 Explicit legacy player-stat collection " +
        " ".join(f"{key}={value}" for key, value in diagnostic.to_dict(omit_none=True).items()), "INFO")
    scrape_afl_player_stats.run_scraper(match_id=args.scrape_match, once=True)


def handle_collect_match_player_stats(args):
    from afl_json import (AflJsonClient, MatchPlayerStatsCollector, later_match_status,
                          reconcile_match_status, upsert_player_stats)
    from db.connection import get_db_connection
    from db.scrape_runs import audited_scrape_run

    conn = get_db_connection()
    try:
        with AflJsonClient() as client:
            reconciliation = reconcile_match_status(
                conn, client, match_provider_id=args.collect_match_player_stats,
                afl_match_id=args.afl_match_id)
            resolved_status = later_match_status(reconciliation.resolved_status, args.source_status)
            status_resolution = ("explicit" if args.source_status == resolved_status
                                 and args.source_status != reconciliation.resolved_status
                                 else reconciliation.resolution_source)
            with audited_scrape_run("match_player_stats", target_type="match",
                                    target_identifier=args.collect_match_player_stats,
                                    conn=conn) as audit:
                result = MatchPlayerStatsCollector(client, raw_directory=args.afl_raw_directory).collect(
                    args.collect_match_player_stats, afl_match_id=reconciliation.afl_match_id,
                    canonical_match_status=resolved_status)
                conn.execute("BEGIN")
                try:
                    rows_written = upsert_player_stats(conn, result)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                audit["rows_read"] = len(result.records)
                audit["rows_written"] = rows_written
    finally:
        conn.close()
    output = {
        "fallback_reason": None,
        "match_provider_id": result.match_provider_id, "status": result.status.value,
        "endpoint_source_status": result.endpoint_source_status,
        "stored_canonical_status": reconciliation.stored_status,
        "direct_match_detail_status": reconciliation.direct_status,
        "resolved_match_status": result.resolved_match_status,
        "status_resolution": status_resolution,
        "canonical_match_refreshed": reconciliation.canonical_refreshed,
        "afl_match_id": reconciliation.afl_match_id, "collected_at": result.collected_at,
        "records_collected": len(result.records), "rejected_records": result.rejected_records,
        "diagnostics": [*reconciliation.diagnostics, *result.diagnostics], "records": result.records,
    }
    diagnostic = CollectionDiagnostic(
        operation="collect_match_player_stats", domain="match_player_stats",
        source_family="cfs_json", collector="MatchPlayerStatsCollector", mode="persistent",
        database_opened=True, persistence_target="cfs_player_stats", persistence_action="upsert",
        source_endpoint=result.source_endpoint, records_received=len(result.records) + result.rejected_records,
        records_normalised=len(result.records), records_rejected=result.rejected_records,
        rows_written=rows_written, result_status=result.status.value,
        result_detail=result.endpoint_source_status, fallback_allowed=False,
        fallback_occurred=False, diagnostic_count=len(output["diagnostics"]),
        match_id=reconciliation.afl_match_id, provider_match_id=result.match_provider_id,
        audit_id=audit["run_id"],
    )
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=output, pretty=True))
    else:
        output.pop("records")
        output["diagnostics"] = [diagnostic.code for diagnostic in output["diagnostics"]]
        print(_diagnostic_output(diagnostic, details=output))


def handle_collect_match_rosters(args):
    from afl_json import AflJsonClient, MatchRosterCollector
    with AflJsonClient() as client:
        result = MatchRosterCollector(client, raw_directory=args.afl_raw_directory).collect(
            args.collect_match_rosters)
    output = {
        "persistence_performed": False, "fallback_reason": None,
        "round_provider_id": result.round_provider_id, "status": result.status.value,
        "publication_state": result.publication_state,
        "provider_timestamp": result.provider_timestamp,
        "provider_version": result.provider_version, "rosters": result.rosters,
        "selections": result.selections,
    }
    diagnostic = CollectionDiagnostic(
        operation="collect_match_rosters", domain="match_rosters", source_family="cfs_json",
        collector="MatchRosterCollector", mode="read_only", database_opened=False,
        persistence_target="none", records_received=len(result.selections),
        records_normalised=len(result.selections),
        result_status="success" if result.status.value == "published" else result.status.value,
        result_detail=result.publication_state, fallback_allowed=False, fallback_occurred=False,
        round_id=result.round_provider_id,
    )
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=output, pretty=True))
    else:
        output.pop("rosters")
        output["selections"] = len(result.selections)
        print(_diagnostic_output(diagnostic, details=output))


def handle_bootstrap_afl_season(args):
    from afl_json import AflJsonClient
    from afl_json.season_sync import bootstrap_afl_season
    from config import AFL_SEASON_YEAR
    from db.connection import get_db_connection
    from db.scrape_runs import audited_scrape_run
    from db.team_seasons import count_team_participants

    conn = get_db_connection()
    try:
        with audited_scrape_run("afl_metadata_bootstrap", target_type="season",
                                target_identifier=args.bootstrap_afl_season, conn=conn) as audit:
            with AflJsonClient() as client:
                result = bootstrap_afl_season(
                    client, conn, season=args.bootstrap_afl_season,
                    competition_code=args.afl_competition_code,
                    competition_provider_id=args.afl_competition_provider_id,
                    raw_directory=args.afl_raw_directory,
                    current_season_year=AFL_SEASON_YEAR)
            summary, player_summary = result.metadata, result.players
            audit["rows_read"] = summary.records_read + player_summary.records_read
            audit["rows_written"] = summary.inserted + summary.updated + player_summary.rows_written
            persisted_counts = {
                "teams": count_team_participants(conn, result.season_id),
                "rounds": conn.execute(
                    "SELECT COUNT(*) FROM rounds WHERE season_id=?", (result.season_id,)
                ).fetchone()[0],
                "matches": conn.execute(
                    "SELECT COUNT(*) FROM matches WHERE season_id=?", (result.season_id,)
                ).fetchone()[0],
                "players": conn.execute(
                    "SELECT COUNT(DISTINCT player_id) FROM competition_season_players "
                    "WHERE competition_season_id=?", (result.season_id,)
                ).fetchone()[0],
                "season_memberships": conn.execute(
                    "SELECT COUNT(*) FROM competition_season_players "
                    "WHERE competition_season_id=?", (result.season_id,)
                ).fetchone()[0],
            }
    finally:
        conn.close()
    details = {
        "competition": result.competition_name, "season": result.season_name,
        "resolved_competition_id": result.competition_id,
        "resolved_season_id": result.season_id,
        "records_read": summary.records_read, "inserted": summary.inserted,
        "updated": summary.updated, "unchanged": summary.unchanged, "failed": summary.failed,
        "player_collection_status": player_summary.status,
        "players_collected": player_summary.records_read,
        "canonical_players_inserted": player_summary.players_inserted,
        "canonical_players_updated": player_summary.players_updated,
        "provider_mappings_inserted": player_summary.mappings_inserted,
        "player_seasons_inserted": player_summary.associations_inserted,
        "player_seasons_updated": player_summary.associations_updated,
        "player_seasons_unchanged": player_summary.unchanged,
        "missing_team_links": player_summary.missing_team_links,
        "player_diagnostics": list(result.player_diagnostics),
        "persisted_counts": persisted_counts,
    }
    status = "unavailable" if player_summary.status == "unavailable" else (
        "unchanged" if summary.inserted + summary.updated + player_summary.rows_written == 0
        else "success")
    diagnostic = CollectionDiagnostic(
        operation="bootstrap_afl_season", domain="season_bootstrap",
        source_family="public_afl_json+cfs_json", collector="PublicAflCollector",
        mode="composite", database_opened=True,
        persistence_target="afl_metadata,canonical_players,player_seasons",
        persistence_action="upsert", records_received=summary.records_read + player_summary.records_read,
        rows_inserted=summary.inserted + player_summary.players_inserted + player_summary.mappings_inserted + player_summary.associations_inserted,
        rows_updated=summary.updated + player_summary.players_updated + player_summary.associations_updated,
        rows_unchanged=summary.unchanged + player_summary.unchanged,
        rows_written=summary.inserted + summary.updated + player_summary.rows_written,
        result_status=status, result_detail=player_summary.status,
        fallback_allowed=False, fallback_occurred=False,
        diagnostic_count=len(result.player_diagnostics), season_id=result.season_id,
        audit_id=audit["run_id"],
    )
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=details, pretty=True))
        return

    completion = "SUCCESS" if status in {"success", "unchanged"} else status.upper()
    print(f"AFL {args.bootstrap_afl_season} bootstrap complete")
    print(f"\nCompetition: {result.competition_name or args.afl_competition_code}")
    print(f"Season: {result.season_name or args.bootstrap_afl_season}")
    print(f"Teams: {persisted_counts['teams']}")
    print(f"Rounds: {persisted_counts['rounds']}")
    print(f"Matches: {persisted_counts['matches']}")
    print(f"Players: {persisted_counts['players']}")
    print(f"Season memberships: {persisted_counts['season_memberships']}")
    print(f"\nResult: {completion}")
    print(f"Next: python cli.py --sync-afl-season {args.bootstrap_afl_season}")
    print(f"Then verify: python cli.py --report-afl-season {args.bootstrap_afl_season}")


def handle_sync_afl_season(args):
    from afl_json import AflJsonClient
    from afl_json.season_sync import SeasonSynchronizer, SeasonSyncOptions
    from config import AFL_SEASON_YEAR
    from db.connection import get_db_connection

    conn = get_db_connection()
    try:
        with AflJsonClient() as client:
            result = SeasonSynchronizer(client, conn).run(
                season=args.sync_afl_season,
                competition_code=args.afl_competition_code,
                competition_provider_id=args.afl_competition_provider_id,
                raw_directory=args.afl_raw_directory,
                current_season_year=AFL_SEASON_YEAR,
                options=SeasonSyncOptions(
                    round_number=args.round, round_from=args.round_from,
                    round_to=args.round_to, match_ids=tuple(args.match_id),
                    refresh_complete=args.refresh_complete,
                ),
            )
    finally:
        conn.close()
    diagnostic = CollectionDiagnostic(
        operation="sync_afl_season", domain="season_sync",
        source_family="public_afl_json+cfs_json",
        collector="SeasonSynchronizer+MatchPlayerStatsCollector", mode="persistent",
        database_opened=True,
        persistence_target="afl_metadata,canonical_players,player_seasons,cfs_player_stats",
        persistence_action="upsert", records_received=result.total_matches_discovered,
        rows_inserted=result.statistic_rows_inserted,
        rows_updated=result.statistic_rows_updated,
        rows_unchanged=result.statistic_rows_unchanged,
        rows_written=result.statistic_rows_inserted + result.statistic_rows_updated,
        result_status=result.outcome, fallback_allowed=False, fallback_occurred=False,
        season_id=result.season_id, audit_id=result.audit_id,
        correlation_id=result.correlation_id,
    )
    envelope_fields = diagnostic.to_dict().keys()
    details = {key: value for key, value in result.to_dict().items()
               if key not in envelope_fields}
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=details, pretty=True))
    else:
        print(_season_sync_summary(result))
    if result.outcome != "success":
        raise SystemExit(1)


def handle_sync_match_rosters(args):
    from afl_json import AflJsonClient
    from afl_json.roster_backfill import sync_match_rosters
    from db.connection import get_db_connection
    conn = get_db_connection()
    try:
        with AflJsonClient() as client:
            result = sync_match_rosters(
                client, conn, year=args.sync_match_rosters, round_number=args.round,
                round_from=args.round_from, round_to=args.round_to,
                competition_code=args.afl_competition_code,
                competition_provider_id=args.afl_competition_provider_id,
                raw_directory=args.afl_raw_directory)
    except ValueError as exc:
        print(f"Roster backfill selection error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        conn.close()
    payload = result.to_dict()
    if args.print_json:
        print(json.dumps(payload, indent=2, default=_json_default))
    else:
        totals = payload["aggregates"]
        print(f"AFL match roster sync {result.requested_season}: {result.outcome}")
        print(f"scope: {result.selection}; rounds selected={totals['rounds_selected']} "
              f"published={totals['rounds_published']} unavailable={totals['rounds_unavailable']} "
              f"conservative_empty={totals['rounds_conservative_empty']} failed={totals['rounds_failed']}")
        print(f"rows: rosters={totals['roster_rows_written']} selections={totals['selection_rows_written']} "
              f"context={totals['context_rows_written']}")
        print(f"unmatched: matches={totals['unmatched_matches']} teams={totals['unmatched_teams']}")
        exceptions = [item for item in result.rounds if item.outcome != "published"]
        if exceptions:
            print("rounds requiring attention: " + ", ".join(
                f"R{item.round_number}={item.outcome}" + (f" ({item.error})" if item.error else "")
                for item in exceptions))
        print(f"overall outcome: {result.outcome}")
    if result.outcome != "success":
        raise SystemExit(1)


def handle_report_afl_season(args):
    """Run the reusable reporter over a query-only SQLite connection."""
    from afl_json.season_report import (SeasonCompletenessReporter, exit_code,
                                        render_human)
    from db.connection import (get_db_path, get_read_only_db_connection)

    conn = get_read_only_db_connection()
    try:
        report = SeasonCompletenessReporter(
            conn, database=get_db_path().name,
        ).report(
            args.report_afl_season,
            competition_code=args.afl_competition_code,
            competition_provider_id=args.afl_competition_provider_id,
        )
    finally:
        conn.close()
    if args.print_json:
        print(json.dumps(report.to_dict(), indent=2, default=_json_default))
    else:
        print(render_human(report))
    code = exit_code(report.status)
    if code:
        raise SystemExit(code)


def handle_report_stats_absence_candidates(args):
    from afl_json.match_data_exceptions import detect_stats_absence_candidates
    from afl_json.season_report import list_persisted_afl_seasons
    from db.connection import get_read_only_db_connection
    conn = get_read_only_db_connection()
    try:
        seasons = list_persisted_afl_seasons(
            conn, competition_code=args.afl_competition_code,
            competition_provider_id=args.afl_competition_provider_id,
        )
        matching = [item for item in seasons
                    if item.year == args.report_stats_absence_candidates]
        if not matching:
            raise ValueError(f"season {args.report_stats_absence_candidates} is not persisted")
        if len(matching) != 1:
            raise ValueError(
                f"season {args.report_stats_absence_candidates} is ambiguous for configured competition"
            )
        season = matching[0]
        candidates = detect_stats_absence_candidates(conn, season_id=season.season_id)
    finally:
        conn.close()
    print(json.dumps({"season": args.report_stats_absence_candidates,
                      "candidates": [item.to_dict() for item in candidates]}, indent=2))


def handle_review_stats_not_expected(args):
    from afl_json.match_data_exceptions import review_stats_not_expected
    from db.connection import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = review_stats_not_expected(
            conn, match_id=args.review_stats_not_expected,
            reason_code=args.reason_code, display_reason=args.display_reason,
            evidence_url=args.evidence_url, evidence_note=args.evidence_note,
            actor=args.reviewed_by,
        )
        conn.commit()
        print(json.dumps(asdict(row), indent=2))
    finally:
        conn.close()


def handle_revoke_stats_not_expected(args):
    from afl_json.match_data_exceptions import revoke_stats_not_expected
    from db.connection import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        revoked = revoke_stats_not_expected(conn, match_id=args.revoke_stats_not_expected,
                                            actor=args.reviewed_by)
        conn.commit()
        print(json.dumps({"match_id": args.revoke_stats_not_expected, "revoked": revoked}))
    finally:
        conn.close()


def handle_collect_afl_metadata(args):
    from afl_json import AflJsonClient, PublicAflCollector
    with AflJsonClient() as client:
        result = PublicAflCollector(client, raw_directory=args.afl_raw_directory).collect(
            competition_code=args.afl_competition_code,
            competition_provider_id=args.afl_competition_provider_id, season=args.afl_season)
    output = {"competition": result.competition, "season": result.season,
              "rounds": result.rounds, "teams": result.teams, "matches": result.matches}
    received = 2 + len(result.rounds) + len(result.teams) + len(result.matches)
    diagnostic = CollectionDiagnostic(
        operation="collect_afl_metadata", domain="metadata",
        source_family="public_afl_json", collector="PublicAflCollector",
        mode="read_only", database_opened=False, persistence_target="none",
        records_received=received, records_normalised=received, result_status="success",
        fallback_allowed=False, fallback_occurred=False,
        season_id=result.season.get("afl_id"),
    )
    if args.print_json:
        print(_diagnostic_output(diagnostic, details=output, pretty=True))
    else:
        print(_diagnostic_output(diagnostic, details={"competition": result.competition["name"],
                          "season": result.season["name"], "rounds": len(result.rounds),
                          "teams": len(result.teams), "matches": len(result.matches)}))

def handle_import_player_movements(args):
    from dataclasses import asdict
    from db.connection import get_db_connection
    from scraper.player_movements.acquisition import MovementAcquirer, LIVE_URL
    from scraper.player_movements.orchestration import import_player_movements
    acquirer=MovementAcquirer()
    source_url=args.movement_source_url or LIVE_URL
    document=(acquirer.acquire_file(args.movement_source_file,source_url=source_url,source_archived_at=args.source_archived_at) if args.movement_source_file else acquirer.acquire_url(source_url))
    conn=get_db_connection()
    try: outcome=import_player_movements(conn,document,movement_season_year=args.import_player_movements)
    finally: conn.close()
    if args.print_json: print(json.dumps(asdict(outcome),indent=2))
    else:
        print(f"AFL editorial movements {outcome.movement_season_year} -> {outcome.movement_season_year+1}")
        print(f"Source: {outcome.source_url}\nAs of: {outcome.source_archived_at or 'live observation'}")
        print(f"Rows: parsed={outcome.rows_parsed} resolved={outcome.rows_resolved} unresolved={outcome.rows_unresolved} ambiguous={outcome.rows_ambiguous}")
        print(f"Persistence: inserted={outcome.inserted} updated={outcome.updated} unchanged={outcome.unchanged}")
        print("Movement types: "+", ".join(f"{k}={v}" for k,v in outcome.counts_by_type.items()))
        print("Canonical membership mutations = 0")

HANDLERS = {
    "collect_afl_data": handle_collect_afl_data,
    "scrape_club": handle_scrape_club, "scrape_clubs": handle_scrape_clubs,
    "enrich_club": handle_enrich_club, "enrich_clubs": handle_enrich_clubs,
    "scrape_enrich_all": handle_scrape_enrich_all,
    "scrape_injuries": handle_scrape_injuries, "scrape_lineups": handle_scrape_lineups,
    "import_clubs": handle_import_clubs, "export_clubs": handle_export_clubs,
    "add_api_key": handle_add_api_key, "list_api_keys": handle_list_api_keys,
    "remove_api_key": handle_remove_api_key,
    "grant_api_key_capability": handle_grant_api_key_capability,
    "revoke_api_key_capability": handle_revoke_api_key_capability,
    "scrape_round": handle_scrape_round, "scrape_all_rounds": handle_scrape_all_rounds,
    "scrape_match": handle_scrape_match,
    "collect_match_player_stats": handle_collect_match_player_stats,
    "collect_statspro_season": handle_collect_statspro_season,
    "collect_statspro_round": handle_collect_statspro_round,
    "build_player_stat_summaries": handle_build_player_stat_summaries,
    "import_player_movements": handle_import_player_movements,
    "collect_match_rosters": handle_collect_match_rosters,
    "bootstrap_afl_season": handle_bootstrap_afl_season,
    "sync_afl_season": handle_sync_afl_season,
    "sync_match_rosters": handle_sync_match_rosters,
    "report_afl_season": handle_report_afl_season,
    "report_stats_absence_candidates": handle_report_stats_absence_candidates,
    "review_stats_not_expected": handle_review_stats_not_expected,
    "revoke_stats_not_expected": handle_revoke_stats_not_expected,
    "collect_afl_metadata": handle_collect_afl_metadata,
}


def dispatch(args):
    """Invoke the one selected operation after CLI validation has completed."""
    from cli import OPERATION_FLAGS
    selected = [name for name in OPERATION_FLAGS if name != "version"
                and getattr(args, name) is not None and getattr(args, name) is not False]
    if len(selected) != 1:
        raise ValueError("dispatch requires exactly one validated operation")
    HANDLERS[selected[0]](args)
