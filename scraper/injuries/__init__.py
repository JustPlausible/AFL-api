"""Explicit boundaries for the rendered-HTML injury collector."""

from .acquisition import InjuryAcquirer
from .orchestration import collect_injuries

__all__ = ["InjuryAcquirer", "collect_injuries"]
