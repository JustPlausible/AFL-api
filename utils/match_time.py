# utils/match_time.py
from datetime import datetime
import re
import pytz
from utils.log import log

def parse_match_time(date_str: str, time_str: str) -> str | None:
    """
    Converts full local date and time like "March 7 2025" and "4:40pmAWST"
    into an ISO UTC datetime string. Timezone must be included in time_str.
    """
    if not date_str or not time_str:
        return None

    # Extract timezone (must be last part of time_str)
    tz_match = re.search(r"(AWST|AEST|AEDT|ACST|ACDT|UTC)$", time_str.upper())
    timezone_str = tz_match.group(1) if tz_match else "AWST"

    tz_map = {
        "AWST": pytz.timezone("Australia/Perth"),
        "AEST": pytz.timezone("Australia/Brisbane"),
        "AEDT": pytz.timezone("Australia/Sydney"),
        "ACST": pytz.timezone("Australia/Darwin"),
        "ACDT": pytz.timezone("Australia/Adelaide"),
        "UTC": pytz.utc,
    }
    tz = tz_map.get(timezone_str, pytz.timezone("Australia/Perth"))

    # Clean time string: remove timezone suffix and spaces
    time_clean = re.sub(r"(AWST|AEST|AEDT|ACST|ACDT|UTC)", "", time_str.upper()).strip()

    try:
        # ✅ Clean ordinal suffixes in day part: "1st" → "1"
        date_str = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", date_str)

        naive_dt = datetime.strptime(f"{date_str} {time_clean}", "%B %d %Y %I:%M%p")
        local_dt = tz.localize(naive_dt)
        iso_utc = local_dt.astimezone(pytz.utc).isoformat()
        log(f"🕒 Parsed datetime: {iso_utc}", "DEBUG")
        return iso_utc
    except Exception as e:
        log(f"⚠️ Failed to parse datetime: '{date_str} {time_clean}' — {e}", "ERROR")
        return None
