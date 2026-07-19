# Administrator CSRF protection

Browser-based administrator routes that change server state must require CSRF validation before performing side effects. Safe read-only `GET` and `HEAD` administrator pages do not require a CSRF token.

## Required pattern

- Render a token in every administrator mutation form with `{{ csrf_input(request) | safe }}`.
- JavaScript browser callers that perform a mutation must submit the same token in the form-encoded `csrf_token` field. Do not place CSRF tokens in URLs, query strings, logs, or documentation examples.
- Protect the handler with `_: None = Depends(require_csrf)` before any database update, file write, scheduler call, import, export, deletion, key change, refresh, scrape, or other state-changing operation.
- Keep read-only authenticated pages accessible without the CSRF dependency.

## Implementation notes

`admin_csrf.py` stores a high-entropy browser-context identifier in the signed session cookie and signs CSRF tokens with the application session secret through `itsdangerous.URLSafeTimedSerializer`. Validation checks the signature, token age, shape, and session-bound context before handlers perform side effects. Invalid, missing, malformed, or expired tokens return a controlled `403` response without exposing token values, credentials, stack traces, secrets, or implementation details.

## Testing new routes

Tests for new administrator mutation routes should verify that:

- the rendered form includes a hidden `csrf_token` field;
- a valid authenticated request with that token succeeds;
- missing, malformed, and invalid tokens are rejected;
- rejection occurs before the side effect;
- authenticated read-only pages remain accessible.
