# utils/http_utils.py

import requests
import time
import random
import config
from playwright.sync_api import sync_playwright
from utils.log import log

def is_blocked(response: requests.Response) -> bool:
    if response.status_code in [403, 429]:
        return True
    if "Access Denied" in response.text or "Are you a robot" in response.text:
        return True
    return False


def respectful_sleep(min_seconds=1.5, max_seconds=3.5):
    sleep_time = random.uniform(min_seconds, max_seconds)
    print(f"⏱ Sleeping for {sleep_time:.2f}s")
    time.sleep(sleep_time)


def scrape_with_backoff(
    url: str,
    max_retries: int = 3,
    min_sleep: float = 1.5,
    max_sleep: float = 3.5,
    timeout: int | None = None
) -> requests.Response | None:
    retries = 0
    timeout = timeout or config.SCRAPER_TIMEOUT

    while retries < max_retries:
        headers = config.get_scraper_headers()
        print(f"🌐 Requesting {url} with UA: {headers['User-Agent']}")
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)

            if is_blocked(resp):
                print(f"🚫 Blocked or throttled! Status: {resp.status_code}")
                respectful_sleep(min_sleep * 2, max_sleep * 2)
                retries += 1
                continue

            if resp.ok:
                return resp
            else:
                print(f"⚠️ Unexpected status: {resp.status_code}")
                retries += 1

        except requests.RequestException as e:
            print(f"❌ Request failed: {e}")
            retries += 1

        respectful_sleep(min_sleep, max_sleep)

    print(f"❗ Failed to retrieve {url} after {max_retries} attempts.")
    return None

def load_page_with_playwright(url: str, wait_time: float = 3.0) -> str | None:
    log(f"🌐 Launching browser for {url}", "INFO")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=10000)
            page.wait_for_timeout(wait_time * 1000)  # Wait for JavaScript to populate
            content = page.content()
            browser.close()
            log("✅ Page loaded with Playwright", "SUCCESS")
            return content
    except Exception as e:
        log(f"❌ Playwright failed: {e}", "ERROR")
        return None