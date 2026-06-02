DOMAIN = "apsystems_openapi"
DEFAULT_BASE_URL = "https://api.apsystemsema.com:9282"
PLATFORMS = ["sensor", "button"]

# NOTE: config_flow.py / __init__.py actually default scan_interval to 1800s
# (30 min).  The 60-min value below is the *recommended* safe value to stay
# under the 1000 calls/month quota year-round (see budget review below).
DEFAULT_SCAN_INTERVAL = 3600  # seconds (hourly energy)

# Summary (lifetime/today/month/year) is fetched once per day near
# the end of solar hours.  "today" is already derived from the hourly
# series; the summary provides ground-truth lifetime/month/year.

# Inverter energy (minutely power/voltage/temperature) is fetched once
# per day at 12:30 when panels should be active and producing.

# ── Monthly API budget review (6 inverters, 1 ECU) ─────────────────────────
# The coordinator runs every `scan_interval` seconds and makes 1 "hourly"
# system-energy call per cycle *during solar hours* (≈ 30 min after sunrise
# until sunset).  The solar window length L varies seasonally.
#
# Per day, with cycle interval I seconds and L solar hours:
#   Hourly system energy:  (3600 / I) × L            (every cycle in daylight)
#   Sunrise re-trigger:    1
#   Daily summary:         1
#   Inverter energy:       6   (once/day, one call per inverter at 12:30)
#   Batch power (per ECU): 1   (once/day at 23:00)
# Per-day total ≈ (3600/I)·L + 9   →   Monthly ≈ 30·((3600/I)·L + 9)
#
#   I = 1800s (30 min — the ACTUAL default):
#       L=8  (winter)  ≈ 720      L=11 (shoulder) ≈ 930
#       L=14 (summer)  ≈ 1110     L=15 (peak)     ≈ 1170   ← EXCEEDS 1000
#
#   I = 3600s (60 min — recommended safe value):
#       L=8 ≈ 510   L=11 ≈ 600   L=14 ≈ 690   L=15 ≈ 720   ← safe year-round
#
# Conclusion: at the default 30-min interval, long summer days push usage
# OVER the 1000 calls/month quota.  Raise the scan interval to 3600s to stay
# safe.  The integration now logs an error and raises a Home Assistant
# persistent notification whenever the API reports the limit was exceeded.

# ── API rate-limit / access-limit response codes ───────────────────────────
# APsystems returns HTTP 200 with one of these codes in the JSON body when the
# account's monthly quota or the server request-rate limit is exceeded.
# See API_REFERENCE.md Annex 1.
API_LIMIT_CODES = {2005, 7001, 7002, 7003}
API_LIMIT_CODE_MESSAGES = {
    2005: "monthly account access limit exceeded",
    7001: "server access limit exceeded",
    7002: "too many requests, please retry later",
    7003: "the system is busy, please retry later",
}

# ── Automatic scan-interval calculation ────────────────────────────────────
# Bounds for the coordinator update interval (seconds).
MIN_SCAN_INTERVAL = 1800   # 30 min — API floor enforced by config_flow
MAX_SCAN_INTERVAL = 7200   # 2 hours
SCAN_INTERVAL_STEP = 300   # round recommendations up to a clean 5-min step

# Monthly call quota and the fraction of it we actually budget for (head-room
# for manual refreshes, restarts, the inverter-list button, etc.).
MONTHLY_API_QUOTA = 1000
API_QUOTA_SAFETY = 0.85
# Worst-case days in a month — sizing against this keeps every month under quota.
DAYS_PER_MONTH = 31

import math as _math


def max_daylight_hours(latitude: float) -> float:
    """Longest possible day length (hours) for a latitude.

    Uses the summer-solstice solar declination (±23.44°). This is the
    worst-case daylight window, so sizing the interval against it keeps the
    busiest month of the year under quota.
    """
    decl = _math.radians(23.44)
    lat = _math.radians(latitude)
    # Hour angle at sunrise/sunset: cos(H) = -tan(lat)·tan(decl)
    cos_h = -_math.tan(lat) * _math.tan(decl)
    cos_h = max(-1.0, min(1.0, cos_h))  # clamp for polar day/night
    half_day_deg = _math.degrees(_math.acos(cos_h))
    return 2.0 * half_day_deg / 15.0  # 15° of rotation per hour


def estimate_monthly_calls(
    interval_s: int,
    latitude: float,
    num_inverters: int,
    num_ecus: int = 1,
    sunrise_offset_min: int = 30,
) -> int:
    """Estimate worst-case monthly API calls for a given scan interval.

    Daily calls = (3600/interval)·window  (hourly system energy, every cycle
    during the solar window) + 1 sunrise re-trigger + 1 daily summary +
    num_inverters (per-inverter energy) + num_ecus (batch power).
    """
    window = max(0.0, max_daylight_hours(latitude) - sunrise_offset_min / 60.0)
    hourly_per_day = (3600.0 / interval_s) * window
    daily_fixed = 2 + num_inverters + num_ecus
    return round(DAYS_PER_MONTH * (hourly_per_day + daily_fixed))


def recommended_scan_interval(
    latitude: float,
    num_inverters: int,
    num_ecus: int = 1,
    sunrise_offset_min: int = 30,
    quota: int = MONTHLY_API_QUOTA,
    safety: float = API_QUOTA_SAFETY,
) -> int:
    """Smallest scan interval (seconds) that keeps the busiest month under quota.

    Solves DAYS·((3600/I)·window + daily_fixed) ≤ quota·safety for I, then
    rounds up to SCAN_INTERVAL_STEP and clamps to [MIN, MAX]_SCAN_INTERVAL.
    """
    window = max(0.0, max_daylight_hours(latitude) - sunrise_offset_min / 60.0)
    daily_fixed = 2 + num_inverters + num_ecus
    budget_per_day = (quota * safety) / DAYS_PER_MONTH
    remaining = budget_per_day - daily_fixed
    if remaining <= 0 or window <= 0:
        # Fixed daily calls alone already blow the budget (or polar night) —
        # poll as slowly as allowed.
        return MAX_SCAN_INTERVAL
    interval = window * 3600.0 / remaining
    interval = _math.ceil(interval / SCAN_INTERVAL_STEP) * SCAN_INTERVAL_STEP
    return max(MIN_SCAN_INTERVAL, min(MAX_SCAN_INTERVAL, int(interval)))