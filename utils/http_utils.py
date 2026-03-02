# utils/http_utils.py

import requests
import time
import random
import config
import os
import errno
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
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

def load_page_with_playwright(url: str, wait_time: float = 3.0, screenshot_on_error: bool = True) -> str | None:
    log(f"🌐 Launching Playwright browser for: {url}", "INFO")

    screenshot_path = os.path.join("logs", "playwright_error.png")
    os.makedirs("logs", exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800}
            )
            page = context.new_page()

            response = page.goto(url, timeout=10000)
            if not response:
                log(f"❌ No response returned from {url}", "ERROR")
                if screenshot_on_error:
                    page.screenshot(path=screenshot_path)
                    log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
                return None

            if response.status != 200:
                log(f"❌ Received HTTP {response.status} from {url}", "ERROR")
                if screenshot_on_error:
                    page.screenshot(path=screenshot_path)
                    log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
                return None

            page.wait_for_timeout(wait_time * 1000)  # Let JS populate
            content = page.content()
            log("✅ Page loaded successfully via Playwright", "SUCCESS")

            browser.close()
            return content

    except PlaywrightTimeoutError:
        log(f"⏰ Timeout while trying to load: {url}", "ERROR")
        if screenshot_on_error:
            try:
                page.screenshot(path=screenshot_path)
                log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
            except Exception:
                pass

    except Exception as e:
        log(f"❌ Exception occurred during page load: {e}", "ERROR")
        if screenshot_on_error:
            try:
                page.screenshot(path=screenshot_path)
                log(f"📸 Screenshot saved to {screenshot_path}", "WARN")
            except Exception:
                pass

    except OSError as e:
        if e.errno == errno.EAGAIN:
            log("⚠️ Resource temporarily unavailable — retrying after short wait", "WARN")
            time.sleep(5)
            return load_page_with_playwright(url, wait_time, screenshot_on_error)
        else:
            log(f"❌ OSError during page load: {e}", "ERROR")

    return None
