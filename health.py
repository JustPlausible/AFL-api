import sqlite3

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from db import connection

router = APIRouter()


def _status_response(state: str, status_code: int = status.HTTP_200_OK) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"status": state})


@router.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    return _status_response("ok")


@router.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    conn = None
    try:
        conn = connection.get_db_connection()
        conn.execute("SELECT 1").fetchone()
    except (FileNotFoundError, sqlite3.Error):
        return _status_response("unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    finally:
        if conn is not None:
            conn.close()

    return _status_response("ok")
