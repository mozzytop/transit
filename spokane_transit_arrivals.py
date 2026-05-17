"""
Spokane Transit — Next Arrival Endpoint
========================================
Returns a plain-text arrival time for a given stop_id so an Apple Shortcut
can call it with a simple GET request and read the result directly.

Usage:
  GET /next-arrival?stop_id=SPRFREEF
  → "08:15 AM"   (or "No upcoming arrivals")

Deployment: FastAPI on Render / PythonAnywhere / AWS Lambda (via Mangum).
Install:     pip install fastapi uvicorn requests pytz
Run locally: uvicorn spokane_transit_arrivals:app --reload
"""

import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo          # stdlib in Python 3.9+
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse

# ---------------------------------------------------------------------------
# Config — swap in the real feed URL if it ever changes
# ---------------------------------------------------------------------------
GTFS_RT_FEED_URL = "https://gtfsbridge.spokanetransit.com/realtime/GTFS-RealTime/TrapezeRealTimeFeed.json"
PACIFIC = ZoneInfo("America/Los_Angeles")

# How many seconds in the past we still consider "upcoming"
# (handles tiny clock skew between the feed server and now)
GRACE_SECONDS = 30

app = FastAPI(title="Spokane Transit Next Arrival", version="1.0.0")


# ---------------------------------------------------------------------------
# Core logic — pure function so it's easy to unit-test independently
# ---------------------------------------------------------------------------
def find_next_arrival(stop_id: str) -> str:
    """
    Fetch the GTFS-RT feed and return the soonest arrival time at *stop_id*
    as a Pacific-Time 12-hour string, e.g. "08:15 AM".
    Returns "No upcoming arrivals" when nothing is found.
    """
    # 1. Fetch -----------------------------------------------------------------
    try:
        resp = requests.get(GTFS_RT_FEED_URL, timeout=10)
        resp.raise_for_status()
        feed = resp.json()
    except requests.RequestException as exc:
        # Surface a clean message rather than a raw traceback
        return f"Feed unavailable: {exc}"
    except ValueError:
        return "Feed returned invalid JSON"

    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - GRACE_SECONDS          # ignore arrivals older than this

    # 2. Walk the entity list --------------------------------------------------
    earliest_ts: float | None = None

    for entity in feed.get("entity", []):
        trip_update = entity.get("trip_update")
        if not trip_update:
            continue

        for stu in trip_update.get("stop_time_update", []):
            if stu.get("stop_id") != stop_id:
                continue

            # Prefer arrival; fall back to departure (some feeds omit arrival
            # at the first stop of a trip or at loop terminals)
            time_event = stu.get("arrival") or stu.get("departure")
            if not time_event:
                continue

            arrival_ts = time_event.get("time")
            if arrival_ts is None:
                continue

            arrival_ts = float(arrival_ts)

            # Skip buses that have already left (with grace period)
            if arrival_ts < cutoff:
                continue

            if earliest_ts is None or arrival_ts < earliest_ts:
                earliest_ts = arrival_ts

    # 3. Format ----------------------------------------------------------------
    if earliest_ts is None:
        return "No upcoming arrivals"

    arrival_dt = datetime.fromtimestamp(earliest_ts, tz=PACIFIC)
    return arrival_dt.strftime("%-I:%M %p")   # e.g. "8:15 AM"  (no leading zero)


# ---------------------------------------------------------------------------
# FastAPI route
# ---------------------------------------------------------------------------
@app.get("/next-arrival", response_class=PlainTextResponse)
def next_arrival(
    stop_id: str = Query(
        ...,                                   # required
        description="GTFS stop_id, e.g. SPRFREEF",
        min_length=1,
    )
) -> str:
    """
    Returns the next arrival time at the requested stop as plain text.
    Apple Shortcuts can call this directly and use the response as a variable.
    """
    return find_next_arrival(stop_id)


# ---------------------------------------------------------------------------
# Health-check — useful for Render / UptimeRobot keep-alive pings
# ---------------------------------------------------------------------------
@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


# ---------------------------------------------------------------------------
# Local dev entry-point: `python spokane_transit_arrivals.py`
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("spokane_transit_arrivals:app", host="0.0.0.0", port=8000, reload=True)
