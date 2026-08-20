"""Diagnostic evidence-capture framework.

Generic infrastructure for opt-in, read-only diagnostic investigations into
upstream (AFL public JSON / CFS) payload behaviour -- for example the
Issue #148 ``match_clock`` investigation into ``score.matchClock.periods``
semantics (see PR #175).

Diagnostic profiles are never consulted by production scheduler decisions
and never become source authority for the consumer API. See
``docs/diagnostics_framework.md`` for the full architecture and operator
guide, and ``diagnostics/framework.py`` for the profile contract.
"""
