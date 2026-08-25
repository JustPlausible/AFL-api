# admin.py
import os
import secrets
from fastapi import Depends, FastAPI, Request, HTTPException, Query, Form, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from html import escape
from datetime import datetime, timedelta, timezone
import sqlite3
from utils.log import log
import traceback
import json
from db.import_to_db import export_clubs_from_db, diff_clubs
import httpx
from collections import defaultdict
from api_key_security import api_key_prefix, generate_api_key, hash_api_key
from db.init_db import create_api_keys_table
from db.connection import get_db_path, get_read_only_db_connection
from afl_json.season_report import SeasonCompletenessReporter, list_persisted_afl_seasons
from analytics.reporting import AnalyticsReporter
from admin_csrf import csrf_input, require_csrf
from logging_sources import (
    STATUS_AVAILABLE, STATUS_DISABLED, STATUS_NOT_CREATED, STATUS_UNAVAILABLE,
    LOG_SOURCES, LogSourceStatus, get_log_source_statuses,
)

security = HTTPBasic()

SCHEDULER_BASE_URL = "http://afl-scheduler:8000"
SCHEDULER_HEALTH_URL = f"{SCHEDULER_BASE_URL}/scheduler/health"
MANUAL_TRIGGER_ENDPOINTS = {
    "injuries": "/scheduler/manual/injuries",
    "fixtures_round": "/scheduler/manual/fixtures/round",
    "lineups_round": "/scheduler/manual/lineups/round",
    "lineups_match": "/scheduler/manual/lineups/match",
    "player_stats_match": "/scheduler/manual/player-stats/match",
}

def _parse_positive_int(value: str | None, label: str) -> tuple[int | None, str | None]:
    if value is None or not str(value).strip():
        return None, f"{label} is required."
    text = str(value).strip()
    if not text.isdecimal():
        return None, f"{label} must be a positive numeric identifier."
    parsed = int(text)
    if parsed <= 0:
        return None, f"{label} must be greater than zero."
    return parsed, None

def _identifier_exists(table: str, column: str, value: int) -> bool:
    conn = sqlite3.connect(get_db_path())
    try:
        return conn.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (value,)).fetchone() is not None
    finally:
        conn.close()

def _manual_message(request: Request, message: str, status_code: int = 200):
    return templates.TemplateResponse(request=request, name="message.html", context={"message": message}, status_code=status_code)

def _post_manual_trigger(kind: str, payload: dict):
    endpoint = MANUAL_TRIGGER_ENDPOINTS[kind]
    response = httpx.post(f"{SCHEDULER_BASE_URL}{endpoint}", json=payload, timeout=5)
    if response.status_code == 409:
        return response.json()
    response.raise_for_status()
    return response.json()

def _format_trigger_response(data: dict) -> str:
    details = (
        f"Source: {data.get('selected_source', 'unknown')}; "
        f"collector: {data.get('collector', 'unknown')}; "
        f"behaviour: {data.get('persistence', 'unknown')}; "
        f"rows collected: {data.get('rows_collected') if data.get('rows_collected') is not None else 'pending'}; "
        f"rows persisted: {data.get('rows_persisted') if data.get('rows_persisted') is not None else 'pending'}; "
        f"outcome: {data.get('outcome_status', data.get('status', 'unknown'))}; "
        f"fallback: {'yes' if data.get('fallback_occurred') else 'no'}"
        + (f" ({data.get('fallback_reason')})" if data.get('fallback_reason') else "")
        + "."
    )
    if data.get("status") == "already_running":
        return f"ℹ️ Equivalent manual job is already queued or running: {data.get('job_id', 'unknown job')}. {details}"
    return f"✅ Manual job queued: {data.get('job_id', 'unknown job')}. {details} Acceptance means queued, not completed; inspect scheduler and scrape-run audit status for final row counts and status."



def verify_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    expected_username = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD")

    if not expected_password:
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            log("❌ ADMIN_PASSWORD must be set in production", "ERROR")
            raise HTTPException(status_code=503, detail="Admin authentication is not configured")
        expected_password = "admin"

    username_ok = secrets.compare_digest(credentials.username, expected_username)
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


app = FastAPI(dependencies=[Depends(verify_admin)])
templates = Jinja2Templates(directory="templates")

session_secret = os.getenv("SESSION_SECRET")
if not session_secret:
    if os.getenv("ENVIRONMENT", "development").lower() == "production":
        raise RuntimeError("SESSION_SECRET must be set in production")
    session_secret = secrets.token_urlsafe(32)

app.state.csrf_secret = session_secret
templates.env.globals["csrf_input"] = csrf_input
app.add_middleware(SessionMiddleware, secret_key=session_secret)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    log("📥 index accessed", "INFO")
    return templates.TemplateResponse(request=request, name="index.html", context={})


def _schedule_group_label(job: dict) -> str:
    """Pick a display group for a scheduler job.

    Prefers structured persisted registry metadata (job_type/match_id/round_id),
    since persisted-only rows (e.g. CFS polling attempts) have no `func` to
    parse. Falls back to substring-matching the APScheduler func reference for
    in-memory jobs that lack persisted metadata.
    """
    persisted = job.get("persisted") or {}
    job_type = job.get("persisted_job_type") or persisted.get("job_type")
    match_id = persisted.get("match_id")
    round_id = persisted.get("round_id")

    if round_id not in (None, ""):
        return f"Round {round_id}"
    if match_id not in (None, ""):
        return f"Match {match_id}"
    if job_type:
        job_type = str(job_type)
        if "injur" in job_type.lower():
            return "Daily Injuries"
        return job_type.replace("_", " ").title()

    func = job.get("func") or ""
    if not isinstance(func, str):
        func = str(func)
    args = job.get("args")
    if not isinstance(args, (list, tuple)) or not args:
        args = None

    if "run_scraper" in func and args:
        return f"Round {args[0]}"
    if "run_match_scraper" in func and args:
        return f"Match {args[0]}"
    if "injury" in func:
        return "Daily Injuries"
    return "General"


_SCHEDULER_HEALTH_STATES = {"healthy", "starting", "unhealthy"}


def _is_valid_scheduler_health(data) -> bool:
    """Validate the full scheduler health contract, not just the presence of `state`.

    A response that names a recognised state but carries a missing/wrong-typed
    field, or contradicts the state it reports (for example `state: healthy`
    while a required dependency is unreachable), is treated the same as an
    unparseable one: this must never let a malformed body render as healthy.
    """
    if not isinstance(data, dict):
        return False
    if data.get("state") not in _SCHEDULER_HEALTH_STATES:
        return False
    for field in ("scheduler_running", "database_accessible", "registry_accessible"):
        if not isinstance(data.get(field), bool):
            return False
    job_count = data.get("job_count")
    if not isinstance(job_count, int) or isinstance(job_count, bool) or job_count < 0:
        return False
    diagnostics = data.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(isinstance(d, str) for d in diagnostics):
        return False
    if not isinstance(data.get("version"), str):
        return False

    if data["state"] == "unhealthy":
        if data["database_accessible"] and data["registry_accessible"]:
            return False
    elif data["state"] == "healthy":
        if not (data["database_accessible"] and data["registry_accessible"] and data["scheduler_running"]):
            return False
    return True


def _fetch_scheduler_health() -> dict:
    """Fetch the stable scheduler health contract (Issue #178), independent of the jobs list.

    Any transport failure (connection error, timeout, unexpected HTTP status,
    unparseable body, or a body that fails the contract's field/invariant
    checks) is normalised to the same "unavailable" result, so the admin UI
    never has to distinguish a network failure from a malformed response.
    """
    try:
        response = httpx.get(SCHEDULER_HEALTH_URL, timeout=5)
    except Exception as e:
        log(f"❌ Failed to contact scheduler health endpoint: {e}", "ERROR")
        return {"available": False}
    if response.status_code not in (200, 503):
        log(f"❌ Unexpected scheduler health status code: {response.status_code}", "ERROR")
        return {"available": False}
    try:
        data = response.json()
    except ValueError:
        return {"available": False}
    if not _is_valid_scheduler_health(data):
        return {"available": False}
    return {"available": True, **data}


def _scheduler_health_display(health: dict) -> dict:
    """Map the stable scheduler health contract to an admin-facing display state.

    Keeps the template free of scheduler implementation detail: it only ever
    sees a label, a bootstrap-alert style, and a short human-readable detail.
    """
    if not health.get("available"):
        return {
            "label": "Unavailable",
            "style": "danger",
            "detail": "The admin interface could not reach the scheduler health endpoint.",
        }

    state = health.get("state")
    job_count = health.get("job_count")

    if state == "healthy":
        if job_count == 0:
            return {
                "label": "Healthy — no jobs registered",
                "style": "success",
                "detail": "The scheduler is running and reachable; its job registry is currently empty.",
            }
        return {
            "label": "Healthy",
            "style": "success",
            "detail": f"The scheduler is running with {job_count} registered job(s).",
        }

    if state == "starting":
        return {
            "label": "Starting / Not ready",
            "style": "warning",
            "detail": "The scheduler process is starting and is not yet accepting scheduled work.",
        }

    if state == "unhealthy":
        diagnostics = health.get("diagnostics") or []
        detail = "The scheduler reports a required dependency or runtime failure."
        if diagnostics:
            detail += " (" + ", ".join(str(code) for code in diagnostics) + ")"
        return {"label": "Unhealthy", "style": "danger", "detail": detail}

    return {
        "label": "Unavailable",
        "style": "danger",
        "detail": "The scheduler health response was not understood.",
    }


@app.get("/schedule", response_class=HTMLResponse)
def show_schedule(request: Request):
    scheduler_error = None
    try:
        response = httpx.get("http://afl-scheduler:8000/scheduler/jobs", timeout=5)
        response.raise_for_status()
        raw_jobs = response.json()
    except Exception as e:
        log(f"❌ Failed to contact scheduler: {e}", "ERROR")
        raw_jobs = []
        scheduler_error = "The scheduler service could not be reached. Job data may be temporarily unavailable."

    # Health is fetched and rendered independently of the jobs list above: a
    # jobs-request failure and scheduler health are different questions.
    scheduler_health_display = _scheduler_health_display(_fetch_scheduler_health())

    # Group persisted-only and in-memory jobs alike; nothing is filtered out.
    grouped = defaultdict(list)
    for job in raw_jobs:
        grouped[_schedule_group_label(job)].append(job)

    return templates.TemplateResponse(
        request=request,
        name="schedule_grouped.html",
        context={
            "grouped_jobs": dict(grouped),
            "scheduler_error": scheduler_error,
            "scheduler_health": scheduler_health_display,
        },
    )

@app.post("/scheduler/refresh", response_class=HTMLResponse)
def refresh_all_jobs(request: Request, _: None = Depends(require_csrf)):
    import httpx
    try:
        response = httpx.post("http://afl-scheduler:8000/scheduler/refresh", timeout=10)
        response.raise_for_status()
        return templates.TemplateResponse(
            request=request,
            name="message.html",
            context={"message": "✅ Schedule refresh successful!"},
        )
    except Exception as e:
        log(f"❌ Failed to refresh scheduler: {e}", "ERROR")
        return templates.TemplateResponse(
            request=request,
            name="message.html",
            context={"message": f"❌ Failed to refresh scheduler: {e}"},
        )


@app.post("/scheduler/manual/{kind}", response_class=HTMLResponse)
def trigger_manual_job(request: Request, kind: str, round_id: str = Form(None), match_id: str = Form(None), _: None = Depends(require_csrf)):
    if kind not in MANUAL_TRIGGER_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Unsupported manual scheduler trigger")
    payload = {}
    if kind in {"fixtures_round", "lineups_round"}:
        parsed, error = _parse_positive_int(round_id, "Round ID")
        if error:
            return _manual_message(request, f"❌ {error}", 422)
        if match_id and match_id.strip():
            return _manual_message(request, "❌ Limit each request to one round or one match.", 422)
        if not _identifier_exists("rounds", "round_id", parsed):
            return _manual_message(request, "❌ Unknown round identifier.", 422)
        payload["round_id"] = parsed
    elif kind in {"lineups_match", "player_stats_match"}:
        parsed, error = _parse_positive_int(match_id, "Match ID")
        if error:
            return _manual_message(request, f"❌ {error}", 422)
        if round_id and round_id.strip():
            return _manual_message(request, "❌ Limit each request to one round or one match.", 422)
        if not _identifier_exists("matches", "match_id", parsed):
            return _manual_message(request, "❌ Unknown match identifier.", 422)
        payload["match_id"] = parsed
    elif kind == "injuries":
        if (round_id and round_id.strip()) or (match_id and match_id.strip()):
            return _manual_message(request, "❌ Injury refresh does not accept a round or match identifier.", 422)
    try:
        data = _post_manual_trigger(kind, payload)
        return _manual_message(request, _format_trigger_response(data))
    except httpx.HTTPStatusError as exc:
        detail = "Scheduler rejected the manual trigger."
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:
            pass
        return _manual_message(request, f"❌ {detail}", exc.response.status_code)
    except Exception:
        log("❌ Scheduler unavailable for manual trigger", "ERROR")
        return _manual_message(request, "❌ Scheduler service is unavailable. No manual job was queued.", 503)

@app.get("/tables", response_class=HTMLResponse)
def show_tables(request: Request):
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse(request=request, name="tables.html", context={"tables": tables})


@app.get("/season-review", response_class=HTMLResponse)
def season_review(request: Request, season: str | None = Query(None)):
    """Render the shared season report through an explicitly read-only connection."""
    conn = get_read_only_db_connection()
    try:
        seasons = list_persisted_afl_seasons(conn)
        selected_year = None
        report = None
        error = None
        if season is not None:
            value = season.strip()
            if not value or not value.isdecimal():
                error = "Select a valid persisted AFL season."
            else:
                try:
                    selected_year = int(value)
                except ValueError:
                    error = "Select a valid persisted AFL season."
                if error is None and selected_year not in {item.year for item in seasons}:
                    error = "The selected AFL season is not persisted."
                elif error is None:
                    report = SeasonCompletenessReporter(
                        conn, database=get_db_path().name,
                    ).report(selected_year)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="season_review.html",
        context={"seasons": seasons, "selected_year": selected_year,
                 "report": report, "error": error},
        status_code=400 if error else 200,
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_report(request: Request, since: str | None = Query(None), until: str | None = Query(None),
                     by_lifecycle: bool = Query(False)):
    """Render the shared analytics report (Issue #205) through a read-only connection."""
    conn = get_read_only_db_connection()
    try:
        report = AnalyticsReporter(conn).report(
            since_date=since or None, until_date=until or None, group_by_lifecycle=by_lifecycle,
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request=request, name="analytics_report.html",
        context={"report": report, "since_date": since, "until_date": until, "by_lifecycle": by_lifecycle},
    )

# Show table of player data
@app.get("/table/{table_name}", response_class=HTMLResponse)
def view_table(
    request: Request,
    table_name: str,
    page: int = Query(1, ge=1),
    search: str = Query("", alias="q"),
    column: str = Query("", alias="col"),
    sort: str = Query("", alias="sort"),
    order: str = Query("asc", alias="order")  # asc or desc
):
    safe_table = escape(table_name)
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()

    try:
        # Column info
        cur.execute(f"PRAGMA table_info(`{safe_table}`)")
        columns_info = cur.fetchall()
        all_columns = [col[1] for col in columns_info]
        col_types = {col[1]: col[2].upper() for col in columns_info}

        # Validate search & sort columns
        selected_col = column if column in all_columns else all_columns[0] if all_columns else None
        sort_col = sort if sort in all_columns else None
        sort_order = "DESC" if order.lower() == "desc" else "ASC"

        # Row count
        if search and selected_col:
            if "CHAR" in col_types[selected_col] or "TEXT" in col_types[selected_col]:
                cur.execute(f"SELECT COUNT(*) FROM `{safe_table}` WHERE `{selected_col}` LIKE ?", (f"%{search}%",))
            else:
                cur.execute(f"SELECT COUNT(*) FROM `{safe_table}` WHERE `{selected_col}` = ?", (search,))
        else:
            cur.execute(f"SELECT COUNT(*) FROM `{safe_table}`")
        total_rows = cur.fetchone()[0]

        # Pagination
        page_size = 50
        offset = (page - 1) * page_size
        query = f"SELECT * FROM `{safe_table}`"
        params = []

        if search and selected_col:
            if "CHAR" in col_types[selected_col] or "TEXT" in col_types[selected_col]:
                query += f" WHERE `{selected_col}` LIKE ?"
                params.append(f"%{search}%")
            else:
                query += f" WHERE `{selected_col}` = ?"
                params.append(search)

        if sort_col:
            query += f" ORDER BY `{sort_col}` {sort_order}"

        query += " LIMIT ? OFFSET ?"
        params += [page_size, offset]

        cur.execute(query, params)
        rows = cur.fetchall()
        headers = [desc[0] for desc in cur.description]

    except sqlite3.OperationalError as e:
        log(f"⚠️ Error occurred: {e}", "ERROR")
        traceback.print_exc()
        conn.close()
        raise HTTPException(status_code=404, detail=str(e))

    conn.close()

    total_pages = (total_rows + page_size - 1) // page_size
    pagination_window = get_pagination_window(page, total_pages)

    return templates.TemplateResponse(
        request=request,
        name="table_view.html",
        context={
            "table": safe_table,
            "headers": headers,
            "rows": rows,
            "pagination_window": pagination_window,
            "total_pages": total_pages,
            "page": page,
            "search": search,
            "columns": all_columns,
            "selected_column": selected_col,
            "sort": sort_col,
            "order": sort_order.lower(),
        },
    )

def get_pagination_window(current, total, window=5):
    left = max(current - window, 1)
    right = min(current + window, total)
    return list(range(left, right + 1))

@app.get("/setup", response_class=HTMLResponse)
def show_setup(request: Request):
    return templates.TemplateResponse(request=request, name="setup.html", context={})

@app.get("/setup/clubs-diff", response_class=HTMLResponse)
def show_clubs_diff(request: Request):
    added, removed, changed = diff_clubs() or ([], [], [])
    return templates.TemplateResponse(
        request=request,
        name="clubs_diff.html",
        context={
            "added": added,
            "removed": removed,
            "changed": changed,
            "message": request.session.pop("message", None),
        },
    )

@app.get("/setup/api-keys", response_class=HTMLResponse)
def view_api_keys(request: Request):
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    create_api_keys_table(cur)
    conn.commit()
    cur.execute("SELECT id, label, key_prefix, created_at, is_active FROM api_keys ORDER BY created_at DESC")
    rows = cur.fetchall()
    one_time_key = request.session.pop("one_time_api_key", None)
    conn.close()

    return templates.TemplateResponse(
        request=request,
        name="api_keys.html",
        context={"api_keys": rows, "one_time_api_key": one_time_key},
    )

@app.get("/setup/api-keys/{key_id}", response_class=HTMLResponse)
def manage_key(request: Request, key_id: int):
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    create_api_keys_table(cur)
    conn.commit()
    cur.execute("SELECT id, label, key_prefix, created_at, is_active FROM api_keys WHERE id = ?", (key_id,))
    key = cur.fetchone()
    conn.close()

    if not key:
        raise HTTPException(status_code=404, detail="Key not found")

    return templates.TemplateResponse(
        request=request,
        name="api_key_manage.html",
        context={"key": key, "one_time_api_key": request.session.pop("one_time_api_key", None)},
    )

@app.post("/setup/api-keys/{key_id}/renew")
def renew_key(request: Request, key_id: int, _: None = Depends(require_csrf)):
    new_key = generate_api_key()
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    create_api_keys_table(cur)
    cur.execute(
        "UPDATE api_keys SET api_key = NULL, key_hash = ?, key_prefix = ? WHERE id = ?",
        (hash_api_key(new_key), api_key_prefix(new_key), key_id),
    )
    conn.commit()
    conn.close()
    request.session["one_time_api_key"] = new_key
    return RedirectResponse(f"/setup/api-keys/{key_id}", status_code=303)

@app.post("/setup/api-keys/{key_id}/toggle")
def toggle_key(request: Request, key_id: int, _: None = Depends(require_csrf)):
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM api_keys WHERE id = ?", (key_id,))
    current = cur.fetchone()
    if current:
        new_value = 0 if current[0] else 1
        cur.execute("UPDATE api_keys SET is_active = ? WHERE id = ?", (new_value, key_id))
        conn.commit()
    conn.close()
    return RedirectResponse(f"/setup/api-keys/{key_id}", status_code=303)

@app.post("/setup/api-keys/{key_id}/toggle-ajax")
def toggle_key_ajax(request: Request, key_id: int, _: None = Depends(require_csrf)):
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM api_keys WHERE id = ?", (key_id,))
    current = cur.fetchone()
    if current is not None:
        new_value = 0 if current[0] else 1
        cur.execute("UPDATE api_keys SET is_active = ? WHERE id = ?", (new_value, key_id))
        conn.commit()
        conn.close()
        return {"success": True, "new_status": new_value}
    conn.close()
    return {"success": False}

@app.post("/setup/api-keys/new")
def create_api_key(request: Request, label: str = Form(...), _: None = Depends(require_csrf)):
    new_key = generate_api_key()
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    create_api_keys_table(cur)
    cur.execute(
        "INSERT INTO api_keys (label, api_key, key_hash, key_prefix) VALUES (?, NULL, ?, ?)",
        (label, hash_api_key(new_key), api_key_prefix(new_key)),
    )
    conn.commit()
    conn.close()
    request.session["one_time_api_key"] = new_key
    return RedirectResponse("/setup/api-keys", status_code=303)

@app.post("/setup/api-keys/delete/{key_id}")
def delete_api_key(request: Request, key_id: int, _: None = Depends(require_csrf)):
    conn = sqlite3.connect(get_db_path())
    cur = conn.cursor()
    cur.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/setup/api-keys", status_code=303)

def _format_log_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}"
        size /= 1024
    return None  # pragma: no cover - unreachable, satisfies static analysis


def _format_log_age(modified_at: datetime | None) -> str | None:
    if modified_at is None:
        return None
    seconds = max(0, int((datetime.now(timezone.utc) - modified_at).total_seconds()))
    if seconds < 60:
        return "less than a minute ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _log_source_display(status: LogSourceStatus) -> dict:
    """Map a LogSourceStatus to an admin-facing display state.

    Keeps the template free of status-contract detail: it only ever sees a
    label, a bootstrap-alert/badge style, and a short human-readable detail,
    mirroring how `_scheduler_health_display` presents scheduler health.
    """
    size_label = _format_log_size(status.size_bytes)
    age_label = _format_log_age(status.modified_at)

    if status.status == STATUS_AVAILABLE:
        summary = f"updated {age_label}" if age_label else "updated recently"
        if size_label:
            summary = f"{summary} — {size_label}"
        return {"label": "Available", "style": "success", "detail": summary}

    if status.status == STATUS_NOT_CREATED:
        return {"label": "Configured, no log created yet", "style": "secondary", "detail": status.reason}

    if status.status == STATUS_DISABLED:
        return {"label": "Disabled", "style": "secondary", "detail": status.reason}

    assert status.status == STATUS_UNAVAILABLE, f"Unhandled log source status: {status.status!r}"
    return {"label": "Configured, expected log path unavailable", "style": "warning", "detail": status.reason}

@app.get("/logs", response_class=HTMLResponse)
def view_logs_raw(
    request: Request,
    log: str = Query("Player Stats"),
    q: str = Query("", alias="q"),
    lines: int = Query(200, ge=10, le=1000),
):
    try:
        statuses = get_log_source_statuses()
        sources = [{"status": s, "display": _log_source_display(s)} for s in statuses]
        by_display_name = {s.display_name: s for s in statuses}
        log_options = list(by_display_name.keys())

        base_context = {
            "log_options": log_options, "q": q, "lines": lines,
            "sources": sources, "selected_log": log,
        }

        selected = by_display_name.get(log)
        if selected is None:
            return templates.TemplateResponse(
                request=request,
                name="logs.html",
                context={**base_context, "logs": [], "log_error": "Unknown log selection.", "expected_file": None},
                status_code=400,
            )

        # A readable file (`size_bytes is not None`) is shown even for a
        # currently-disabled source: disabling a source stops new writes, it
        # must not hide a log that was already captured while it was enabled.
        if selected.size_bytes is None:
            display = _log_source_display(selected)
            return templates.TemplateResponse(
                request=request,
                name="logs.html",
                context={
                    **base_context, "logs": [],
                    "log_error": f"{display['label']}. {selected.reason}".strip(),
                    "expected_file": selected.resolved_path,
                },
            )

        log_path = LOG_SOURCES[selected.id].path
        LOCAL_TZ = timezone(timedelta(hours=8))  # AWST

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()

        filtered = [line for line in raw_lines if q.lower() in line.lower()]
        display_lines = filtered[-lines:]

        def convert_utc_line(line: str) -> str:
            try:
                ts_match = line.split("]")[0].strip("[").replace(" UTC", "")
                dt = datetime.strptime(ts_match, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                local_dt = dt.astimezone(LOCAL_TZ)
                converted = local_dt.strftime("%Y-%m-%d %H:%M:%S AWST")
                return line.replace(ts_match + " UTC", converted)
            except Exception:
                return line  # Fallback to original if parsing fails

        formatted_lines = [escape(convert_utc_line(l)) for l in display_lines]

        return templates.TemplateResponse(
            request=request,
            name="logs.html",
            context={
                **base_context,
                "logs": formatted_lines,
                "log_error": None,
                "expected_file": selected.resolved_path,
                "viewing_disabled_source": not selected.enabled,
            },
        )

    except Exception as e:
        return HTMLResponse(f"<h2>❌ Failed to load logs: {e}</h2>", status_code=500)

@app.post("/clubs-diff/import")
def do_import_clubs(request: Request, _: None = Depends(require_csrf)):
    from cli_runtime import import_clubs_to_db

    import_clubs_to_db()
    request.session["message"] = "✅ Clubs imported from JSON."
    return RedirectResponse("/setup/clubs-diff", status_code=303)

@app.post("/clubs-diff/export")
def do_export_clubs(request: Request, _: None = Depends(require_csrf)):
    export_clubs_from_db()
    request.session["message"] = "✅ Clubs exported to backup JSON."
    return RedirectResponse("/setup/clubs-diff", status_code=303)
