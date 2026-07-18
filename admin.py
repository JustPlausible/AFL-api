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
from pathlib import Path
from utils.log import log
import traceback
import json
from db.import_to_db import export_clubs_from_db, diff_clubs
import httpx
from collections import defaultdict

security = HTTPBasic()


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

app.add_middleware(SessionMiddleware, secret_key=session_secret)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    log("📥 index accessed", "INFO")
    return templates.TemplateResponse("index.html", {"request": request})

DB_PATH = Path("data/afl_players.db")

@app.get("/schedule", response_class=HTMLResponse)
def show_schedule(request: Request):
    try:
        response = httpx.get("http://afl-scheduler:8000/scheduler/jobs", timeout=5)
        response.raise_for_status()
        raw_jobs = response.json()
    except Exception as e:
        log(f"❌ Failed to contact scheduler: {e}", "ERROR")
        raw_jobs = []

    # Group by round_id if found in args
    grouped = defaultdict(list)

    for job in raw_jobs:
        # Assume round_id is first arg for run_scraper or run_match_scraper
        round_id = "General"
        if "run_scraper" in job["func"] and "args" in job:
            round_id = f"Round {job['args'][0]}"
        elif "run_match_scraper" in job["func"] and "args" in job:
            round_id = f"Match {job['args'][0]}"
        elif "injury" in job["func"]:
            round_id = "Daily Injuries"
        grouped[round_id].append(job)

    return templates.TemplateResponse("schedule_grouped.html", {
        "request": request,
        "grouped_jobs": dict(grouped)
    })

@app.get("/scheduler/refresh", response_class=HTMLResponse)
def refresh_all_jobs_get(request: Request):
    import httpx
    try:
        response = httpx.post("http://afl-scheduler:8000/scheduler/refresh", timeout=10)
        response.raise_for_status()
        return templates.TemplateResponse("message.html", {
            "request": request,
            "message": "✅ Schedule refresh successful!"
        })
    except Exception as e:
        log(f"❌ Failed to refresh scheduler: {e}", "ERROR")
        return templates.TemplateResponse("message.html", {
            "request": request,
            "message": f"❌ Failed to refresh scheduler: {e}"
        })

@app.get("/tables", response_class=HTMLResponse)
def show_tables(request: Request):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse("tables.html", {"request": request, "tables": tables})

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
    conn = sqlite3.connect(DB_PATH)
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

    return templates.TemplateResponse("table_view.html", {
        "request": request,
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
        "order": sort_order.lower()
    })

def get_pagination_window(current, total, window=5):
    left = max(current - window, 1)
    right = min(current + window, total)
    return list(range(left, right + 1))

@app.get("/setup", response_class=HTMLResponse)
def show_setup(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})

@app.get("/setup/clubs-diff", response_class=HTMLResponse)
def show_clubs_diff(request: Request):
    added, removed, changed = diff_clubs()
    return templates.TemplateResponse("clubs_diff.html", {
        "request": request,
        "added": added,
        "removed": removed,
        "changed": changed,
        "message": request.session.pop("message", None)
    })

@app.get("/setup/api-keys", response_class=HTMLResponse)
def view_api_keys(request: Request):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM api_keys ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()

    return templates.TemplateResponse("api_keys.html", {
        "request": request,
        "api_keys": rows
    })

@app.get("/setup/api-keys/{key_id}", response_class=HTMLResponse)
def manage_key(request: Request, key_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    key = cur.fetchone()
    conn.close()

    if not key:
        raise HTTPException(status_code=404, detail="Key not found")

    return templates.TemplateResponse("api_key_manage.html", {
        "request": request,
        "key": key
    })

@app.post("/setup/api-keys/{key_id}/renew")
def renew_key(key_id: int):
    new_key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE api_keys SET api_key = ? WHERE id = ?", (new_key, key_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/setup/api-keys/{key_id}", status_code=303)

@app.post("/setup/api-keys/{key_id}/toggle")
def toggle_key(key_id: int):
    conn = sqlite3.connect(DB_PATH)
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
def toggle_key_ajax(key_id: int):
    conn = sqlite3.connect(DB_PATH)
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
def create_api_key(label: str = Form(...)):
    new_key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO api_keys (label, api_key) VALUES (?, ?)", (label, new_key))
    conn.commit()
    conn.close()
    return RedirectResponse("/setup/api-keys", status_code=303)

@app.post("/setup/api-keys/delete/{key_id}")
def delete_api_key(key_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/setup/api-keys", status_code=303)

LOG_FILES = {
    "Player Stats": "scrape_afl_player_stats.log",
    "Injuries": "scrape_afl_injuries.log",
    "Lineups": "scrape_afl_lineups.log",
    "Matches": "scrape_afl_matches.log",
    "Scheduler Jobs": "scheduler_jobs.log",
}

@app.get("/logs", response_class=HTMLResponse)
def view_logs_raw(
    request: Request,
    log: str = Query("Player Stats"),
    q: str = Query("", alias="q"),
    lines: int = Query(200, ge=10, le=1000),
):
    try:
        file_name = LOG_FILES.get(log, None)
        if not file_name:
            return HTMLResponse(f"<h2>⚠️ Unknown log file: {log}</h2>", status_code=400)

        log_path = Path("logs") / file_name
        LOCAL_TZ = timezone(timedelta(hours=8))  # AWST

        if not log_path.exists():
            return HTMLResponse(f"<h2>⚠️ Log file not found: {file_name}</h2>", status_code=404)

        with open(log_path, "r", encoding="utf-8") as f:
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

        return templates.TemplateResponse("logs.html", {
            "request": request,
            "logs": formatted_lines,
            "selected_log": log,
            "log_options": LOG_FILES.keys(),
            "q": q,
            "lines": lines,
        })

    except Exception as e:
        return HTMLResponse(f"<h2>❌ Failed to load logs: {e}</h2>", status_code=500)

@app.post("/clubs-diff/import")
def do_import_clubs(request: Request):
    from cli import import_clubs_to_db

    import_clubs_to_db()
    request.session["message"] = "✅ Clubs imported from JSON."
    return RedirectResponse("/clubs-diff", status_code=303)

@app.post("/clubs-diff/export")
def do_export_clubs(request: Request):
    export_clubs_from_db()
    request.session["message"] = "✅ Clubs exported to backup JSON."
    return RedirectResponse("/clubs-diff", status_code=303)