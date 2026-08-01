from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from typing import Callable

from scraper.afl_selectors import INJURY_SELECTORS
from .models import InjurySourceDocument

INJURY_URL = "https://www.afl.com.au/matches/injury-list"


class InjuryAcquirer:
    """Playwright-only source adapter; it deliberately knows no parser or database."""

    def __init__(self, playwright_factory: Callable | None = None, *,
                 navigation_timeout_ms: int = 60_000, content_timeout_ms: int = 15_000):
        self._playwright_factory = playwright_factory
        self.navigation_timeout_ms = navigation_timeout_ms
        self.content_timeout_ms = content_timeout_ms

    def acquire(self) -> InjurySourceDocument:
        # Keep Playwright import and browser types out of every downstream stage.
        if self._playwright_factory is None:
            from playwright.sync_api import sync_playwright
            factory = sync_playwright
        else:
            factory = self._playwright_factory
        started = monotonic()
        with factory() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(INJURY_URL, timeout=self.navigation_timeout_ms)
                page.wait_for_selector(
                    INJURY_SELECTORS.ARTICLE_BODY, timeout=self.content_timeout_ms
                )
                html = page.content()
                if not html or INJURY_SELECTORS.ARTICLE_BODY.split(".")[-1] not in html:
                    raise ValueError("Injury acquisition returned no required article content")
            finally:
                browser.close()
        return InjurySourceDocument(
            html=html,
            source_url=INJURY_URL,
            acquired_at=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=round((monotonic() - started) * 1000),
        )
