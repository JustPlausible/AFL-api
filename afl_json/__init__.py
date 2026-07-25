"""Contracts shared by AFL JSON collectors."""

from .contracts import (
    CFS_ERROR_AUTH,
    CFS_ERROR_NOT_PUBLISHED,
    ENDPOINTS,
    IDENTIFIER_TYPES,
    RETRYABLE_HTTP_STATUSES,
    SOURCE_PRIORITY,
    SUCCESS_HTTP_STATUSES,
    EndpointDefinition,
    get_endpoint,
)
from .client import (
    AflJsonAuthenticationError,
    AflJsonClient,
    AflJsonError,
    AflJsonHttpError,
    AflJsonInvalidResponse,
    AflJsonResourceUnavailable,
    AflJsonResponse,
    AflJsonTransportError,
    HttpPolicy,
    WMCTokenProvider,
)

__all__ = [
    "CFS_ERROR_AUTH",
    "CFS_ERROR_NOT_PUBLISHED",
    "ENDPOINTS",
    "IDENTIFIER_TYPES",
    "RETRYABLE_HTTP_STATUSES",
    "SOURCE_PRIORITY",
    "SUCCESS_HTTP_STATUSES",
    "EndpointDefinition",
    "get_endpoint",
    "AflJsonAuthenticationError",
    "AflJsonClient",
    "AflJsonError",
    "AflJsonHttpError",
    "AflJsonInvalidResponse",
    "AflJsonResourceUnavailable",
    "AflJsonResponse",
    "AflJsonTransportError",
    "HttpPolicy",
    "WMCTokenProvider",
]
