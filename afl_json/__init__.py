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
]
