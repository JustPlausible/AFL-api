from datetime import datetime, timezone
from pathlib import Path
import re
from utils.http_utils import get_scraper_http_client
from .models import MovementSourceDocument

LIVE_URL = "https://www.afl.com.au/news/retirements-and-delistings"
_WAYBACK = re.compile(r"https?://web\.archive\.org/web/(\d{14})(?:[a-z_]+)?/")

def archived_at_from_url(url: str) -> str | None:
    match = _WAYBACK.match(url)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat().replace('+00:00','Z')

class MovementAcquirer:
    def __init__(self, http_client=None, *, timeout=None):
        self.http_client = http_client or get_scraper_http_client()
        self.timeout = timeout

    def acquire_url(self, url: str = LIVE_URL, *, source_archived_at: str | None = None, observed_at: str | None = None):
        response = self.http_client.get(url, timeout=self.timeout)
        html = response.text
        if not html or "retirements-and-delistings" not in html.lower():
            raise ValueError("Player-movement source returned no required editorial content")
        return MovementSourceDocument(html, url, observed_at or datetime.now(timezone.utc).isoformat(), source_archived_at if source_archived_at is not None else archived_at_from_url(url))

    def acquire_file(self, path, *, source_url: str, source_archived_at: str | None = None, observed_at: str | None = None):
        html = Path(path).read_text(encoding="utf-8")
        return MovementSourceDocument(html, source_url, observed_at or datetime.now(timezone.utc).isoformat(), source_archived_at if source_archived_at is not None else archived_at_from_url(source_url))
