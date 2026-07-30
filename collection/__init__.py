"""Application-level operational collection orchestration."""

from .source_policy import (
    CollectionOutcome,
    OperationalDomain,
    SOURCE_POLICY,
    collect_operational,
    policy_for,
)

__all__ = [
    "CollectionOutcome", "OperationalDomain", "SOURCE_POLICY",
    "collect_operational", "policy_for",
]
