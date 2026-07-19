"""CSRF helpers for browser-based administrator mutation forms."""

from __future__ import annotations

import secrets
from html import escape

from fastapi import Form, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

CSRF_FIELD_NAME = "csrf_token"
CSRF_SESSION_KEY = "admin_csrf_context"
CSRF_MAX_AGE_SECONDS = 60 * 60 * 4


def _serializer(request: Request) -> URLSafeTimedSerializer:
    secret = request.app.state.csrf_secret
    return URLSafeTimedSerializer(secret_key=secret, salt="admin-csrf-v1")


def _context_id(request: Request) -> str:
    context_id = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(context_id, str) or len(context_id) < 32:
        context_id = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = context_id
    return context_id


def generate_csrf_token(request: Request) -> str:
    """Create a signed CSRF token bound to this authenticated admin browser context."""
    return _serializer(request).dumps({"ctx": _context_id(request)})


def csrf_input(request: Request) -> str:
    token = escape(generate_csrf_token(request), quote=True)
    return f'<input type="hidden" name="{CSRF_FIELD_NAME}" value="{token}">'


def validate_csrf_token(request: Request, token: str) -> None:
    if not token or not isinstance(token, str) or len(token) > 4096:
        raise_csrf_error()
    try:
        data = _serializer(request).loads(token, max_age=CSRF_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        raise_csrf_error()
    expected_context = request.session.get(CSRF_SESSION_KEY)
    submitted_context = data.get("ctx") if isinstance(data, dict) else None
    if not isinstance(expected_context, str) or not isinstance(submitted_context, str):
        raise_csrf_error()
    if not secrets.compare_digest(submitted_context, expected_context):
        raise_csrf_error()


def raise_csrf_error() -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="CSRF validation failed. Please reload the admin page and submit the form again.",
    )


def require_csrf(request: Request, csrf_token: str = Form("")) -> None:
    validate_csrf_token(request, csrf_token)
