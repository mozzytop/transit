"""
Spokane Transit — Arrival Board  (v5)
======================================
Output format per stop:

  Route #9 // 2:19 PM // 2:20 PM       ← scheduled // real-time (if different)
  Route #9 // 2:49 PM // On Time        ← on-time (within 60s of schedule)
  Route #9 // 3:19 PM                   ← no real-time data yet

  Route #12 // Sprague @ Sherman // 2:25 PM // 2:27 PM
  Route #12 // Sprague @ Sherman // 3:05 PM
  Route #12 // Sprague @ Sherman // 3:45 PM // On Time

Works with any stop — use the number on the bus stop sign:
  GET /next-arrival?stop_id=2849
  GET /next-arrival?stop_id=2968
  GET /next-arrival?stop_id=SPRSHEEF   ← alpha stop_id also accepted

NEW — JSON endpoint for Apple Shortcuts:
  GET /next-arrival-json?stop_id=2968
  GET /next-arrival-json?stop_id=2968&dest_stop_id=2849
  GET /search-stops?q=sprague

Install:  pip install fastapi uvicorn requests pytz
Run:      uvicorn spokane_transit_arrivals:app --reload
"""

import io
import csv
import zipfile
import threading
import logging
from collections import defaultdict
from datetime import datetime, timedelta, date, time as dtime, timezone

import pytz
import requests
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sta")

# ---------------------------------------------------------------------------
GTFS_ZIP_URL   = "https://www.spokanetransit.com/gtfs"
GTFS_RT_URL    = (
    "https://gtfsbridge.spokanetransit.com/realtime/"
    "GTFS-RealTime/TrapezeRealTimeFeed.json"
)
PACIFIC        = pytz.timezone("America/Los_Angeles")
NUM_ARRIVALS   = 3      # next N arrivals shown per route
GRACE_SECONDS  = 60     # seconds in the past still counted as "upcoming"
ON_TIME_WINDOW = 60     # delay ≤ this many seconds = "On Time"

# Hardcoded favorite stops — use the name or code in any endpoint
PRESET_STOPS = {
    "farr":      {"stop_code": "2968", "name": "Sprague @ Farr (Winco)"},
    "sherman":   {"stop_code": "2849", "name": "Sprague @ Sherman"},
    "appleway":  {"stop_code": "2884", "name": "Appleway @ Farr (Winco)"},
}

# ---------------------------------------------------------------------------
_lock = threading.Lock()
_gtfs: dict = {
    "loaded_date": None,          # date object; rebuilt when date changes
    "stops_by_code": {},          # "2849"    → {stop_id, stop_name}
    "stops_by_id":   {},          # "SPRSHEEF"→ {stop_code, stop_name}
    "trips":         {},          # trip_id   → {route_id, service_id}
    "routes":        {},          # route_id  → short_name  e.g. "9"
    "schedule":      {},          # stop_id   → [(unix_ts, trip_id), ...]
}

def _hms_to_secs(t: str) -> int:
    """'14:19:00' or '25:30:00' → seconds from midnight (handles >24h)."""
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _secs_to_unix(secs: int, service_date: date) -> float:
    """Seconds-from-midnight + Pacific service date → Unix timestamp."""
    extra = secs // 86400
    rem   = secs %  86400
    naive = datetime.combine(
        service_date + timedelta(days=extra),
        dtime(rem // 3600, (rem % 3600) // 60, rem % 60),
    )
    return PACIFIC.localize(naive).timestamp()


def _load_gtfs() -> None:
    """Download STA GTFS zip and rebuild the in-memory cache."""
    today     = datetime.now(PACIFIC).date()
    today_str = today.strftime("%Y%m%d")
    day_name  = today.strftime("%A").lower()   # "monday" etc.

    log.info("Downloading GTFS from %s …", GTFS_ZIP_URL)
    resp = requests.get(GTFS_ZIP_URL, timeout=60)
    resp.raise_for_status()
    zf   = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    log.info("GTFS zip downloaded. Files: %s", sorted(names))

    # -- stops ----------------------------------------------------------------
    stops_by_code: dict = {}
    stops_by_id:   dict = {}
    with zf.open("stops.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid  = row["stop_id"].strip()
            code = row.get("stop_code", "").strip()
            name = row.get("stop_name", "").strip()
            stops_by_id[sid] = {"stop_code": code, "stop_name": name}
            if code:
                stops_by_code[code] = {"stop_id": sid, "stop_name": name}

    # -- routes ---------------------------------------------------------------
    routes: dict = {}
    with zf.open("routes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            rid   = row["route_id"].strip()
            short = row.get("route_short_name", "").strip() or rid
            routes[rid] = short

    # -- trips ----------------------------------------------------------------
    trips: dict = {}
    with zf.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            trips[row["trip_id"].strip()] = {
                "route_id":   row["route_id"].strip(),
                "service_id": row["service_id"].strip(),
            }

    # -- active services today ------------------------------------------------
    active: set = set()
    if "calendar.txt" in names:
        with zf.open("calendar.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                if (row.get(day_name, "0") == "1"
                        and row["start_date"] <= today_str <= row["end_date"]):
                    active.add(row["service_id"].strip())

    if "calendar_dates.txt" in names:
        with zf.open("calendar_dates.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                if row["date"] == today_str:
                    sid = row["service_id"].strip()
                    if row["exception_type"] == "1":
                        active.add(sid)
                    elif row["exception_type"] == "2":
                        active.discard(sid)

    log.info("Active service IDs today: %s", active)
    active_trips = {tid for tid, t in trips.items() if t["service_id"] in active}

    # -- stop_times (only active trips) ---------------------------------------
    schedule: dict = defaultdict(list)
    with zf.open("stop_times.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            tid = row["trip_id"].strip()
            if tid not in active_trips:
                continue
            sid = row["stop_id"].strip()
            raw = (row.get("arrival_time") or row.get("departure_time") or "").strip()
            if not raw:
                continue
            secs  = _hms_to_secs(raw)
            unix  = _secs_to_unix(secs, today)
            schedule[sid].append((unix, tid))

    for sid in schedule:
        schedule[sid].sort()

    log.info("Schedule loaded: %d stops, %d active trips.", len(schedule), len(active_trips))

    with _lock:
        _gtfs["loaded_date"]   = today
        _gtfs["stops_by_code"] = stops_by_code
        _gtfs["stops_by_id"]   = stops_by_id
        _gtfs["trips"]         = trips
        _gtfs["routes"]        = routes
        _gtfs["schedule"]      = dict(schedule)


def _ensure_gtfs() -> str | None:
    """Reload GTFS if stale (new day). Returns error string or None."""
    with _lock:
        loaded_date = _gtfs["loaded_date"]
    today = datetime.now(PACIFIC).date()
    if loaded_date != today:
        try:
            _load_gtfs()
        except Exception as exc:
            log.exception("GTFS load failed")
            return f"Schedule data unavailable: {exc}"
    return None

# ---------------------------------------------------------------------------
def _fetch_rt() -> tuple[dict, str | None]:
    try:
        r = requests.get(GTFS_RT_URL, timeout=10)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        return {}, str(exc)


def _build_rt_index(feed: dict) -> dict:
    """
    Returns {trip_id: {stop_id: {"rt_time": float, "delay": int|None}}}
    """
    idx: dict = {}
    for entity in feed.get("entity", []):
        tu = entity.get("trip_update")
        if not tu:
            continue
        trip_id = tu.get("trip", {}).get("trip_id", "")
        if not trip_id:
            continue
        for stu in tu.get("stop_time_update", []):
            sid = stu.get("stop_id", "")
            evt = stu.get("arrival") or stu.get("departure") or {}
            rt  = evt.get("time")
            if rt is None:
                continue
            idx.setdefault(trip_id, {})[sid] = {
                "rt_time": float(rt),
                "delay":   evt.get("delay"),
            }
    return idx

# ---------------------------------------------------------------------------
def _fmt(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=PACIFIC).strftime("%-I:%M %p")


def _build_lines(stop_id: str, stop_name: str, rt_idx: dict, now_ts: float) -> list[str]:
    cutoff   = now_ts - GRACE_SECONDS
    schedule = _gtfs["schedule"]
    trips    = _gtfs["trips"]
    routes   = _gtfs["routes"]

    # Gather upcoming arrivals, group by route (capped at NUM_ARRIVALS each)
    by_route: dict = defaultdict(list)
    for unix_ts, trip_id in schedule.get(stop_id, []):
        if unix_ts < cutoff:
            continue
        route_id = trips.get(trip_id, {}).get("route_id", "?")
        if len(by_route[route_id]) < NUM_ARRIVALS:
            by_route[route_id].append((unix_ts, trip_id))

    if not by_route:
        return ["No upcoming arrivals"]

    # Sort routes numerically where possible, alpha otherwise
    def _route_sort_key(rid):
        short = routes.get(rid, rid)
        try:
            return (0, int(short), short)
        except ValueError:
            return (1, 0, short)

    lines: list[str] = []
    for route_idx, route_id in enumerate(sorted(by_route, key=_route_sort_key)):
        short   = routes.get(route_id, route_id)
        include_stop_name = (route_idx > 0)   # second+ routes show stop name

        if lines:
            lines.append("")   # blank line between route groups

        for sched_ts, trip_id in by_route[route_id]:
            sched_str = _fmt(sched_ts)

            # Real-time lookup
            rt_info = rt_idx.get(trip_id, {}).get(stop_id)
            if rt_info:
                rt_ts  = rt_info["rt_time"]
                delay  = rt_info.get("delay") or 0
                rt_str = "On Time" if abs(delay) <= ON_TIME_WINDOW else _fmt(rt_ts)
            else:
                rt_str = None

            # Assemble line
            parts = [f"Route #{short}"]
            if include_stop_name:
                parts.append(stop_name)
            parts.append(sched_str)
            if rt_str:
                parts.append(rt_str)

            lines.append(" // ".join(parts))

    return lines


# ---------------------------------------------------------------------------
# NEW: Structured arrival data for JSON endpoint
# ---------------------------------------------------------------------------
def _get_arrivals_data(
    stop_id: str,
    stop_name: str,
    rt_idx: dict,
    now_ts: float,
    dest_stop_id: str | None = None,
    dest_stop_name: str | None = None,
) -> list[dict]:
    """
    Returns structured arrival data as a list of dicts.
    If dest_stop_id is provided, each arrival includes when
    that same trip reaches the destination stop (from GTFS schedule).
    """
    cutoff   = now_ts - GRACE_SECONDS
    schedule = _gtfs["schedule"]
    trips    = _gtfs["trips"]
    routes   = _gtfs["routes"]

    # Build a quick lookup for the destination stop's schedule:
    # {trip_id: unix_ts} for the destination
    dest_by_trip: dict = {}
    if dest_stop_id:
        for dest_ts, dest_tid in schedule.get(dest_stop_id, []):
            dest_by_trip[dest_tid] = dest_ts

    # Count arrivals per route to cap at NUM_ARRIVALS
    route_counts: dict = defaultdict(int)
    arrivals: list[dict] = []

    for unix_ts, trip_id in schedule.get(stop_id, []):
        if unix_ts < cutoff:
            continue

        route_id    = trips.get(trip_id, {}).get("route_id", "?")
        route_short = routes.get(route_id, route_id)

        if route_counts[route_id] >= NUM_ARRIVALS:
            continue
        route_counts[route_id] += 1

        # --- Real-time data for boarding stop ---
        rt_info = rt_idx.get(trip_id, {}).get(stop_id)
        if rt_info:
            rt_ts   = rt_info["rt_time"]
            delay   = rt_info.get("delay") or 0
            if abs(delay) <= ON_TIME_WINDOW:
                status  = "on_time"
            else:
                status  = "delayed"
            realtime_str = _fmt(rt_ts)
            effective_ts = rt_ts  # use real-time for minutes_away
        else:
            status       = "no_data"
            realtime_str = None
            effective_ts = unix_ts

        minutes_away = max(0, round((effective_ts - now_ts) / 60))

        arrival: dict = {
            "route":          route_short,
            "trip_id":        trip_id,
            "scheduled":      _fmt(unix_ts),
            "scheduled_unix": unix_ts,
            "minutes_away":   minutes_away,
            "status":         status,
        }
        if realtime_str:
            arrival["realtime"] = realtime_str

        # --- Destination lookup (same trip, different stop) ---
        if dest_stop_id and trip_id in dest_by_trip:
            dest_sched_ts = dest_by_trip[trip_id]
            ride_minutes  = max(0, round((dest_sched_ts - unix_ts) / 60))

            arrival["dest_stop_name"]   = dest_stop_name or dest_stop_id
            arrival["dest_arrival"]     = _fmt(dest_sched_ts)
            arrival["dest_arrival_unix"] = dest_sched_ts
            arrival["ride_minutes"]     = ride_minutes

            # Real-time for destination stop too
            dest_rt = rt_idx.get(trip_id, {}).get(dest_stop_id)
            if dest_rt:
                arrival["dest_realtime"]     = _fmt(dest_rt["rt_time"])
                arrival["dest_arrival_unix"] = dest_rt["rt_time"]
                ride_minutes_rt = max(0, round((dest_rt["rt_time"] - effective_ts) / 60))
                arrival["ride_minutes"] = ride_minutes_rt

            # Total minutes from now until arriving at destination
            dest_effective = arrival["dest_arrival_unix"]
            arrival["total_trip_minutes"] = max(0, round((dest_effective - now_ts) / 60))

        # --- Build label for Shortcuts "Choose from List" ---
        status_tag = {"on_time": "[On Time]", "delayed": "[Delayed]", "no_data": ""}[status]
        display_time = realtime_str or arrival["scheduled"]
        label = f"#{route_short} // {display_time} ({minutes_away} min) {status_tag}"
        if "ride_minutes" in arrival:
            label += f" // {arrival['ride_minutes']} min ride"
        arrival["label"] = label.strip()

        arrivals.append(arrival)

    return arrivals


# ---------------------------------------------------------------------------
def _resolve(raw: str) -> tuple[str | None, str]:
    """(stop_id, stop_name) or (None, error_msg).
    Accepts: preset name ('farr'), numeric stop code ('2968'),
    or alpha stop ID ('SPRSHEEF').
    """
    s = raw.strip()

    # Check preset names first (case-insensitive)
    preset = PRESET_STOPS.get(s.lower())
    if preset:
        s = preset["stop_code"]

    if s.isdigit():
        info = _gtfs["stops_by_code"].get(s)
        if not info:
            return None, f"Stop code {s} not found in schedule data"
        return info["stop_id"], info["stop_name"]
    s = s.upper()
    info = _gtfs["stops_by_id"].get(s)
    if not info:
        return None, f"Stop ID '{s}' not found in schedule data"
    return s, info["stop_name"]

# ---------------------------------------------------------------------------
def get_arrivals(stop_input: str) -> str:
    err = _ensure_gtfs()
    if err:
        return err

    stop_id, result = _resolve(stop_input)
    if stop_id is None:
        return result   # error message
    stop_name = result

    feed, _ = _fetch_rt()
    rt_idx   = _build_rt_index(feed) if feed else {}
    now_ts   = datetime.now(timezone.utc).timestamp()

    lines = _build_lines(stop_id, stop_name, rt_idx, now_ts)
    return "\n".join(lines)

# ---------------------------------------------------------------------------
app = FastAPI(title="Spokane Transit Arrival Board", version="5.0.0")


@app.on_event("startup")
def startup_event():
    """Pre-load GTFS in background so first request isn't slow."""
    t = threading.Thread(target=_load_gtfs, daemon=True)
    t.start()


@app.get("/next-arrival", response_class=PlainTextResponse)
def next_arrival(
    stop_id: str = Query(
        ...,
        description="Bus stop number (e.g. 2849) or alpha stop ID (e.g. SPRSHEEF)",
        min_length=1,
    )
):
    return get_arrivals(stop_id)


# ---------------------------------------------------------------------------
# NEW: JSON endpoint — returns structured data for Apple Shortcuts
# ---------------------------------------------------------------------------
@app.get("/next-arrival-json", response_class=JSONResponse)
def next_arrival_json(
    stop_id: str = Query(
        ...,
        description="Bus stop number (e.g. 2968) or alpha stop ID",
        min_length=1,
    ),
    dest_stop_id: str = Query(
        default=None,
        description="Optional destination stop to get ride time & arrival ETA",
    ),
):
    """
    Returns structured JSON with upcoming arrivals.
    If dest_stop_id is provided, each arrival includes the time
    that same trip arrives at the destination (actual GTFS data,
    not a generic estimate).

    Apple Shortcuts usage:
      1. GET /next-arrival-json?stop_id=2968&dest_stop_id=2849
      2. Get Dictionary Value → "arrivals"
      3. Choose from List (shows "label" field for each)
      4. Get Dictionary Value from chosen item → minutes_away, ride_minutes, etc.
    """
    err = _ensure_gtfs()
    if err:
        return JSONResponse({"error": err}, status_code=503)

    # Resolve boarding stop
    board_id, board_result = _resolve(stop_id)
    if board_id is None:
        return JSONResponse({"error": board_result}, status_code=404)
    board_name = board_result

    # Resolve destination stop (optional)
    dest_id   = None
    dest_name = None
    if dest_stop_id:
        dest_id, dest_result = _resolve(dest_stop_id)
        if dest_id is None:
            return JSONResponse({"error": dest_result}, status_code=404)
        dest_name = dest_result

    feed, _  = _fetch_rt()
    rt_idx   = _build_rt_index(feed) if feed else {}
    now_ts   = datetime.now(timezone.utc).timestamp()
    now_str  = datetime.now(PACIFIC).strftime("%-I:%M %p")

    arrivals = _get_arrivals_data(
        board_id, board_name, rt_idx, now_ts,
        dest_stop_id=dest_id, dest_stop_name=dest_name,
    )

    response = {
        "stop_name":  board_name,
        "stop_id":    stop_id,
        "queried_at": now_str,
        "arrivals":   arrivals,
    }
    if dest_name:
        response["dest_stop_name"] = dest_name
        response["dest_stop_id"]   = dest_stop_id

    if not arrivals:
        response["message"] = "No upcoming arrivals"

    return JSONResponse(response, headers={"Cache-Control": "public, max-age=30"})


# ---------------------------------------------------------------------------
# NEW: Stop search — find stop codes by name
# ---------------------------------------------------------------------------
@app.get("/search-stops", response_class=JSONResponse)
def search_stops(
    q: str = Query(
        ...,
        description="Search term (e.g. 'sprague', 'sherman', 'plaza')",
        min_length=2,
    ),
):
    """Search stops by name. Returns matching stops with their codes."""
    err = _ensure_gtfs()
    if err:
        return JSONResponse({"error": err}, status_code=503)

    query = q.strip().lower()
    results = []
    for code, info in _gtfs["stops_by_code"].items():
        if query in info["stop_name"].lower():
            results.append({
                "stop_code": code,
                "stop_id":   info["stop_id"],
                "stop_name": info["stop_name"],
            })

    # Also search by stop_id for stops without codes
    for sid, info in _gtfs["stops_by_id"].items():
        if query in info["stop_name"].lower() and info["stop_code"] == "":
            results.append({
                "stop_id":   sid,
                "stop_name": info["stop_name"],
            })

    results.sort(key=lambda x: x.get("stop_name", ""))
    return {"query": q, "count": len(results), "stops": results[:25]}


# ---------------------------------------------------------------------------
# Preset stops list
# ---------------------------------------------------------------------------
@app.get("/stops", response_class=JSONResponse)
def list_stops():
    """Returns the hardcoded preset stops. Use any stop_code or preset
    name in the stop_id / dest_stop_id parameters of other endpoints."""
    return {
        "presets": [
            {
                "name": key,
                "stop_code": val["stop_code"],
                "label": val["name"],
            }
            for key, val in PRESET_STOPS.items()
        ],
        "note": "You can also pass any numeric stop code or alpha stop ID.",
    }


@app.get("/health", response_class=PlainTextResponse)
def health():
    with _lock:
        d = _gtfs["loaded_date"]
    return f"ok — schedule loaded for {d}" if d else "ok — schedule loading..."


@app.get("/", response_class=PlainTextResponse)
def root():
    return (
        "Spokane Transit Arrival Board v5\n"
        "─────────────────────────────────\n"
        "\n"
        "PRESET STOPS:\n"
        "  farr      -> 2968  Sprague @ Farr (Winco)\n"
        "  sherman   -> 2849  Sprague @ Sherman\n"
        "  appleway  -> 2884  Appleway @ Farr (Winco)\n"
        "\n"
        "PLAIN TEXT:\n"
        "  GET /next-arrival?stop_id=2968\n"
        "  GET /next-arrival?stop_id=farr        (preset name works too)\n"
        "\n"
        "JSON (for Shortcuts):\n"
        "  GET /next-arrival-json?stop_id=farr\n"
        "  GET /next-arrival-json?stop_id=farr&dest_stop_id=sherman\n"
        "  GET /next-arrival-json?stop_id=2968&dest_stop_id=2849\n"
        "\n"
        "STOPS:\n"
        "  GET /stops                            (list presets)\n"
        "  GET /search-stops?q=sprague            (search all stops)\n"
        "\n"
        "GET /health"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("spokane_transit_arrivals:app", host="0.0.0.0", port=8000, reload=True)
