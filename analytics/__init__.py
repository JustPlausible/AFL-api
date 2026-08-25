"""Modular analytics/telemetry framework (Issue #205).

Historical/domain analytics over the AFL-api value chain -- upstream
AFL/CFS polling behaviour and consumer ``/api/v1`` usage -- answering
questions such as "how often did this resource actually change?" and
"which collected resources are being requested?". See
``docs/analytics_framework.md`` for the full architecture and the boundary
between this framework, the diagnostics evidence-capture framework
(``diagnostics/``), and normal operational logging.

Public entry points a collector or API route needs:

* :func:`analytics.record.record_upstream_poll` -- one call per logical
  upstream poll.
* :func:`analytics.record.record_consumer_request` -- one call per
  ``/api/v1`` request (wired centrally as middleware; routes do not need to
  call this themselves).

Everything else in this package (storage, rollup, reporting) is
implementation detail or operator tooling, not something a collector needs
to import directly.
"""
