"""
Spokane Transit — Next Arrival Endpoint  (v3)
==============================================
Changes:
  - Tries BOTH alpha stop_id (SPRFARWF) and numeric stop_code (2968)
    when searching the feed, so it works regardless of which format
    Spokane Transit uses in their real-time feed
  - Added /debug?stop_id=2968 endpoint to inspect raw feed data
  - Added /peek endpoint to dump the first few raw entity objects
    so you can see the exact JSON structure of the live feed

Usage:
  GET /next-arrival?stop_id=2968          <- works with numeric OR alpha
  GET /debug?stop_id=2968                 <- shows what the feed has for this stop
  GET /peek                               <- dumps raw feed structure (troubleshooting)
  GET /health
"""

import requests
import pytz
from datetime import datetime, timezone
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse, JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GTFS_RT_FEED_URL = (
    "https://gtfsbridge.spokanetransit.com/realtime/"
    "GTFS-RealTime/TrapezeRealTimeFeed.json"
)

PACIFIC = pytz.timezone("America/Los_Angeles")
GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Bidirectional stop lookup — alpha <-> numeric
# ---------------------------------------------------------------------------
STOP_CODE_TO_ID = {
    "2849": "SPRSHEEF",  # Sprague @ Sherman
    "2968": "SPRFARWF",  # Sprague @ Farr (WinCo)
}
# Reverse map: alpha -> numeric
STOP_ID_TO_CODE = {v: k for k, v in STOP_CODE_TO_ID.items()}

ROUTE_NAMES = {
    "9":  "Route 9 Sprague",
    "12": "Route 12 Southside Medical",
}

app = FastAPI(title="Spokane Transit Next Arrival", version="3.0.0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_both_ids(raw: str):
    """
    Given either a numeric code ('2968') or alpha id ('SPRFARWF'),
    return a set containing BOTH forms so we can match either in the feed.
    """
    stripped = raw.strip().upper()
    candidates = {stripped}

    if stripped.isdigit():
        alpha = STOP_CODE_TO_ID.get(stripped)
        if alpha:
            candidates.add(alpha)
    else:
        numeric = STOP_ID_TO_CODE.get(stripped)
        if numeric:
            candidates.add(numeric)

    return candidates


def fetch_feed():
    """Fetch the GTFS-RT feed. Returns (feed_dict, error_string)."""
    try:
        resp = requests.get(GTFS_RT_FEED_URL, timeout=10)
        resp.raise_for_status()
        return resp.json(), None
    except requests.exceptions.ConnectionError:
        return None, "Error: could not connect to the transit feed"
    except requests.exceptions.Timeout:
        return None, "Error: transit feed timed out"
    except requests.exceptions.HTTPError as e:
        return None, f"Error: feed returned HTTP {e.response.status_code}"
    except ValueError:
        return None, "Error: feed returned invalid JSON"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def find_next_arrival(stop_id_raw: str) -> str:
    feed, err = fetch_feed()
    if err:
        return err

    candidates = get_both_ids(stop_id_raw)
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - GRACE_SECONDS

    best_ts = None
    best_route = ""

    for entity in feed.get("entity", []):
        trip_update = entity.get("trip_update")
        if not trip_update:
            continue

        route_id = trip_update.get("trip", {}).get("route_id", "")

        for stu in trip_update.get("stop_time_update", []):
            if stu.get("stop_id") not in candidates:
                continue

            time_event = stu.get("arrival") or stu.get("departure")
            if not time_event:
                continue

            arrival_ts = time_event.get("time")
            if arrival_ts is None:
                continue

            arrival_ts = float(arrival_ts)
            if arrival_ts < cutoff:
                continue

            if best_ts is None or arrival_ts < best_ts:
                best_ts = arrival_ts
                best_route = route_id

    if best_ts is None:
        return "No upcoming arrivals"

    arrival_dt = datetime.fromtimestamp(best_ts, tz=PACIFIC)
    time_str = arrival_dt.strftime("%-I:%M %p")
    route_name = ROUTE_NAMES.get(best_route, f"Route {best_route}" if best_route else "")

    return f"{time_str} — {route_name}" if route_name else time_str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/next-arrival", response_class=PlainTextResponse)
def next_arrival(stop_id: str = Query(..., min_length=1)):
    return find_next_arrival(stop_id)


@app.get("/debug")
def debug(stop_id: str = Query(..., min_length=1)):
    """
    Returns every stop_time_update entry from the live feed that matches
    this stop (trying both numeric and alpha forms). Use this to verify
    the feed is seeing your stop and to inspect the raw time values.
    """
    feed, err = fetch_feed()
    if err:
        return JSONResponse({"error": err})

    candidates = get_both_ids(stop_id)
    now_ts = datetime.now(timezone.utc).timestamp()
    matches = []

    for entity in feed.get("entity", []):
        trip_update = entity.get("trip_update")
        if not trip_update:
            continue

        route_id = trip_update.get("trip", {}).get("route_id", "")
        trip_id  = trip_update.get("trip", {}).get("trip_id", "")

        for stu in trip_update.get("stop_time_update", []):
            if stu.get("stop_id") not in candidates:
                continue

            arrival_ts   = (stu.get("arrival")   or {}).get("time")
            departure_ts = (stu.get("departure")  or {}).get("time")

            def fmt(ts):
                if ts is None:
                    return None
                dt = datetime.fromtimestamp(float(ts), tz=PACIFIC)
                return dt.strftime("%-I:%M %p") + f" (unix {ts})"

            matches.append({
                "route_id":    route_id,
                "trip_id":     trip_id,
                "feed_stop_id": stu.get("stop_id"),
                "arrival":     fmt(arrival_ts),
                "departure":   fmt(departure_ts),
                "in_past":     (float(arrival_ts) < now_ts) if arrival_ts else None,
            })

    matches.sort(key=lambda x: x.get("arrival") or "")

    return JSONResponse({
        "queried_stop":   stop_id,
        "candidates_tried": list(candidates),
        "now_pacific":    datetime.fromtimestamp(now_ts, tz=PACIFIC).strftime("%-I:%M %p"),
        "total_entities": len(feed.get("entity", [])),
        "matches_found":  len(matches),
        "matches":        matches,
    })


@app.get("/peek")
def peek():
    """
    Dumps the first 3 raw entities from the live feed so you can see
    the exact JSON structure Spokane Transit uses.
    """
    feed, err = fetch_feed()
    if err:
        return JSONResponse({"error": err})

    entities = feed.get("entity", [])
    return JSONResponse({
        "total_entities": len(entities),
        "sample": entities[:3],
    })


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", response_class=PlainTextResponse)
def root():
    return (
        "Spokane Transit API v3\n"
        "Endpoints:\n"
        "  /next-arrival?stop_id=2968\n"
        "  /debug?stop_id=2968\n"
        "  /peek\n"
        "  /health"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("spokane_transit_arrivals:app", host="0.0.0.0", port=8000, reload=True)
