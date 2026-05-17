"""
Spokane Transit — Next Arrival Endpoint  (v2)
==============================================
Fixes in this version:
  - Uses pytz instead of zoneinfo (works on any Python 3.7+ / Render runtime)
  - Accepts EITHER the numeric stop code (e.g. 2849) OR the alpha stop_id
    (e.g. SPRSHEEF) — whichever is printed on the bus stop sign
  - Returns route info alongside the time so you know which bus is coming
  - Cleaner error messages for easier debugging

Usage:
  GET /next-arrival?stop_id=SPRSHEEF      ← alpha stop_id
  GET /next-arrival?stop_id=2849          ← numeric stop code (same stop)
  GET /next-arrival?stop_id=2968          ← Sprague @ Farr / WinCo

Install:  pip install fastapi uvicorn requests pytz
Run:      uvicorn spokane_transit_arrivals:app --reload
"""

import requests
import pytz
from datetime import datetime, timezone
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GTFS_RT_FEED_URL = (
    "https://gtfsbridge.spokanetransit.com/realtime/"
    "GTFS-RealTime/TrapezeRealTimeFeed.json"
)

PACIFIC = pytz.timezone("America/Los_Angeles")

# How many seconds in the past we still consider "upcoming"
GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Numeric stop_code -> alpha stop_id lookup
# (from Spokane Transit GTFS stops.txt — add more as needed)
# ---------------------------------------------------------------------------
STOP_CODE_TO_ID = {
    # Code : stop_id         Stop name
    "2849": "SPRSHEEF",  # Sprague @ Sherman (inbound toward U-District)
    "2968": "SPRFARWF",  # Sprague @ Farr (outbound toward WinCo)
    # Add additional stops here in the same format
}

# ---------------------------------------------------------------------------
# Route ID -> friendly name (covers routes 9 and 12 which serve these stops)
# ---------------------------------------------------------------------------
ROUTE_NAMES = {
    "9":  "Route 9 Sprague",
    "12": "Route 12 Southside Medical",
}

app = FastAPI(title="Spokane Transit Next Arrival", version="2.0.0")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def resolve_stop_id(raw):
    """
    Accept either a numeric stop_code ('2849') or an alpha stop_id ('SPRSHEEF').
    Returns the alpha stop_id the GTFS-RT feed uses.
    """
    stripped = raw.strip()
    if stripped.isdigit():
        resolved = STOP_CODE_TO_ID.get(stripped)
        if resolved is None:
            return stripped  # Unknown numeric — pass through as-is
        return resolved
    return stripped.upper()


def find_next_arrival(stop_id_raw):
    """
    Returns the next arrival at stop_id as a Pacific-time string like
    "8:15 AM — Route 9 Sprague", or "No upcoming arrivals".
    """
    stop_id = resolve_stop_id(stop_id_raw)

    # 1. Fetch -----------------------------------------------------------------
    try:
        resp = requests.get(GTFS_RT_FEED_URL, timeout=10)
        resp.raise_for_status()
        feed = resp.json()
    except requests.exceptions.ConnectionError:
        return "Error: could not connect to the transit feed"
    except requests.exceptions.Timeout:
        return "Error: transit feed timed out"
    except requests.exceptions.HTTPError as e:
        return f"Error: feed returned HTTP {e.response.status_code}"
    except ValueError:
        return "Error: feed returned invalid JSON"

    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - GRACE_SECONDS

    # 2. Walk entities ---------------------------------------------------------
    best_ts = None
    best_route = ""

    for entity in feed.get("entity", []):
        trip_update = entity.get("trip_update")
        if not trip_update:
            continue

        route_id = trip_update.get("trip", {}).get("route_id", "")

        for stu in trip_update.get("stop_time_update", []):
            if stu.get("stop_id") != stop_id:
                continue

            # Prefer arrival; fall back to departure
            time_event = stu.get("arrival") or stu.get("departure")
            if not time_event:
                continue

            arrival_ts = time_event.get("time")
            if arrival_ts is None:
                continue

            arrival_ts = float(arrival_ts)
            if arrival_ts < cutoff:
                continue  # Already left

            if best_ts is None or arrival_ts < best_ts:
                best_ts = arrival_ts
                best_route = route_id

    # 3. Format ----------------------------------------------------------------
    if best_ts is None:
        return "No upcoming arrivals"

    arrival_dt = datetime.fromtimestamp(best_ts, tz=PACIFIC)
    time_str = arrival_dt.strftime("%-I:%M %p")  # e.g. "8:15 AM"
    route_name = ROUTE_NAMES.get(best_route, f"Route {best_route}" if best_route else "")

    if route_name:
        return f"{time_str} — {route_name}"
    return time_str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/next-arrival", response_class=PlainTextResponse)
def next_arrival(
    stop_id: str = Query(
        ...,
        description="Numeric stop code (2849) or alpha stop_id (SPRSHEEF)",
        min_length=1,
    )
):
    return find_next_arrival(stop_id)


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", response_class=PlainTextResponse)
def root():
    """Friendly root so Render health checks don't 404."""
    return "Spokane Transit API is running. Use /next-arrival?stop_id=2849"


# ---------------------------------------------------------------------------
# Local dev
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("spokane_transit_arrivals:app", host="0.0.0.0", port=8000, reload=True)
