from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic

from scraper.afl_selectors import INJURY_SELECTORS
from utils.http_utils import get_scraper_http_client
from .models import InjurySourceDocument

INJURY_URL = "https://www.afl.com.au/matches/injury-list"


class InjuryAcquirer:
    """Plain-HTTP source adapter; it deliberately knows no parser or database.

    Acquisition decision (Issue #213): a paired live capture of the same
    finals-window page -- one plain HTTP response, one browser-rendered DOM
    (``docs/investigation/afl-json/samples/injuries/`` and
    ``docs/investigation/afl_injury_finals_evidence_capture_2026-08-24.md``)
    -- showed the plain HTTP response already contains the complete
    ``article__body`` contract: all 10 observed team blocks, correct club
    markers, every player/injury/estimated-return row, and ``Updated:``
    text, byte-for-byte identical to the rendered capture once the parser's
    two accepted table-sibling shapes (see ``scraper/injuries/parser.py``)
    are normalised. No JavaScript execution was required to obtain this
    content, so Playwright is no longer used here.
    """

    def __init__(self, http_client=None, *, timeout=None):
        self._http_client = http_client or get_scraper_http_client()
        self._timeout = timeout

    def acquire(self) -> InjurySourceDocument:
        started = monotonic()
        response = self._http_client.get(INJURY_URL, timeout=self._timeout)
        html = response.text
        if not html or INJURY_SELECTORS.ARTICLE_BODY.split(".")[-1] not in html:
            raise ValueError("Injury acquisition returned no required article content")
        return InjurySourceDocument(
            html=html,
            source_url=INJURY_URL,
            acquired_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=round((monotonic() - started) * 1000),
        )
