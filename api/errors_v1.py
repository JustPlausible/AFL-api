"""Small, reusable application-error contract for ``/api/v1`` routes."""

from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ApplicationError(BaseModel):
    code: str
    message: str


class ApplicationErrorResponse(BaseModel):
    error: ApplicationError


def application_error(status_code: int, code: str, message: str) -> JSONResponse:
    """Return a safe v1 application error without FastAPI's ``detail`` wrapper."""
    payload = ApplicationErrorResponse(error=ApplicationError(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump())
