# admin.py
from fastapi import FastAPI, Request, HTTPException, Query, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from html import escape
import sqlite3
from pathlib import Path
from utils.log import log
import traceback
import secrets
import json
from db.import_to_db import import_clubs_to_db, export_clubs_from_db, diff_clubs

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.add_middleware(SessionMiddleware, secret_key="your-secret-key")  # use .env later

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    log("📥 index accessed", "INFO")
    return templates.TemplateResponse("index.html", {"request": request})

DB_PATH = Path("data/afl_players.db")

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
    search: str = Query("", alias="q")
):
    safe_table = escape(table_name)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    try:
        # Count total rows for pagination
        if search:
            cur.execute(f"SELECT COUNT(*) FROM `{safe_table}` WHERE full_name LIKE ?", (f"%{search}%",))
        else:
            cur.execute(f"SELECT COUNT(*) FROM `{safe_table}`")
        total_rows = cur.fetchone()[0]

        # Pagination setup
        page_size = 50
        offset = (page - 1) * page_size
        query = f"SELECT * FROM `{safe_table}`"
        params = []

        # Optional search
        if search:
            query += " WHERE full_name LIKE ?"
            params.append(f"%{search}%")

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

    return templates.TemplateResponse("table_view.html", {
        "request": request,
        "table": safe_table,
        "headers": headers,
        "rows": rows,
        "page": page,
        "total_pages": (total_rows + page_size - 1) // page_size,
        "search": search
    })

@app.get("/clubs-diff", response_class=HTMLResponse)
def show_clubs_diff(request: Request):
    added, removed, changed = diff_clubs()
    return templates.TemplateResponse("clubs_diff.html", {
        "request": request,
        "added": added,
        "removed": removed,
        "changed": changed,
        "message": request.session.pop("message", None)
    })

@app.get("/api-keys", response_class=HTMLResponse)
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

@app.get("/api-keys/{key_id}", response_class=HTMLResponse)
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

@app.post("/api-keys/{key_id}/renew")
def renew_key(key_id: int):
    new_key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE api_keys SET api_key = ? WHERE id = ?", (new_key, key_id))
    conn.commit()
    conn.close()
    return RedirectResponse(f"/api-keys/{key_id}", status_code=303)

@app.post("/api-keys/{key_id}/toggle")
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
    return RedirectResponse(f"/api-keys/{key_id}", status_code=303)

@app.post("/api-keys/new")
def create_api_key(label: str = Form(...)):
    new_key = secrets.token_urlsafe(32)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO api_keys (label, api_key) VALUES (?, ?)", (label, new_key))
    conn.commit()
    conn.close()
    return RedirectResponse("/api-keys", status_code=303)

@app.post("/api-keys/delete/{key_id}")
def delete_api_key(key_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/api-keys", status_code=303)

@app.post("/clubs-diff/import")
def do_import_clubs(request: Request):
    import_clubs_to_db()
    request.session["message"] = "✅ Clubs imported from JSON."
    return RedirectResponse("/clubs-diff", status_code=303)

@app.post("/clubs-diff/export")
def do_export_clubs(request: Request):
    export_clubs_from_db()
    request.session["message"] = "✅ Clubs exported to backup JSON."
    return RedirectResponse("/clubs-diff", status_code=303)