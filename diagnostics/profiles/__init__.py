"""Checked-in diagnostic profiles.

Importing this module registers every approved, built-in diagnostic profile
with the framework registry (``diagnostics.framework``). Being registered
here only makes a profile *eligible* for scheduling -- it still has to be
named in ``AFL_DIAGNOSTIC_PROFILES`` (and ``AFL_DIAGNOSTICS_ENABLED=true``)
to actually run. This is the whole extension point: a new investigation adds
one module here and one import/`register_profile` call below, never a
config-driven or dynamically loaded profile.
"""
from diagnostics.framework import register_profile
from diagnostics.profiles.match_clock import MatchClockProfile

register_profile(MatchClockProfile())

__all__ = ["MatchClockProfile"]
