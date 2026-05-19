"""
Spokane Transit — Arrival Board  (v5)
======================================
New in v5
---------
  • Hardcoded stops (2968 / 2849 / 2884) with display names
  • /next-arrival-json  → structured JSON for Apple Shortcuts bus-picker
      menu_items list   → ready-made strings for "Choose from List"
      departure_unix    → exact departure timestamp
      minutes_until     → countdown already computed server-side
      stop_lat / lon    → for "Get Travel Time" in Shortcuts
  • /schedule           → full day timetable for any stop + any date
      &format=text (default)  plain-text grouped by route
      &format=json            machine-readable array
  • /stops              → list hard-coded stops as JSON

Plain-text output (unchanged from v4):
  Route #9 // 2:19 PM // 2:20 PM       ← scheduled // real-time (if different)
  Route #9 // 2:49 PM // On Time
  Route #9 // 3:19 PM

  Route #12 // Sprague @ Sherman // 2:25 PM // 2:27 PM

Menu item format (for /next-arrival-json → menu_items):
  "Route #9 // 2:19 PM // On Time // 14 min"
  "Route #9 // 2:49 PM // +2 min // 44 min"
  "Route #9 // 3:19 PM // Scheduled // 74 min"
  Split on " // " → [route, scheduled_time, status, minutes_until]

Install:  pip install fastapi uvicorn requests pytz
Run:      uvicorn spokane_transit_arrivals:app --reload
"""

import io
import csv
import zipfile
import threading
import logging
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta, date, time as dtime, timezone

import pytz
import requests
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sta")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GTFS_ZIP_URL   = "https://www.spokanetransit.com/gtfs"
GTFS_RT_URL    = (
    "https://gtfsbridge.spokanetransit.com/realtime/"
    "GTFS-RealTime/TrapezeRealTimeFeed.json"
)
PACIFIC        = pytz.timezone("America/Los_Angeles")
NUM_ARRIVALS   = 3       # next N arrivals shown per route in /next-arrival
GRACE_SECONDS  = 60      # seconds in the past still counted as "upcoming"
ON_TIME_WINDOW = 60      # delay ≤ this many seconds = "On Time"

# Hard-coded stops  stop_code → display name
HARDCODED_STOPS: OrderedDict = OrderedDict([
    ("2968", "Sprague @ Farr (Winco)"),
    ("2849", "Sprague @ Sherman"),
    ("2884", "Appleway @ Farr (Winco)"),
])

# ---------------------------------------------------------------------------
# GTFS zip cache  (raw bytes, re-downloaded once per day)
# ---------------------------------------------------------------------------
_zip_lock  = threading.Lock()
_zip_cache: dict = {"bytes": None, "fetched_date": None}


def _ensure_zip():
    """
    Return (ZipFile, None) built from cached bytes, or (None, error_str).
    Re-downloads the zip once per Pacific day.
    Falls back to a stale zip rather than returning an error when possible.
    """
    today = datetime.now(PACIFIC).date()
    with _zip_lock:
        if _zip_cache["fetched_date"] != today or _zip_cache["bytes"] is None:
            log.info("Downloading GTFS zip from %s …", GTFS_ZIP_URL)
            try:
                resp = requests.get(GTFS_ZIP_URL, timeout=60)
                resp.raise_for_status()
                _zip_cache["bytes"]        = resp.content
                _zip_cache["fetched_date"] = today
                log.info("GTFS zip downloaded (%d bytes).", len(resp.content))
            except Exception as exc:
                log.exception("GTFS zip download failed.")
                if _zip_cache["bytes"] is None:
                    return None, f"Schedule data unavailable: {exc}"
                log.warning("Using stale GTFS zip from %s.", _zip_cache["fetched_date"])
        raw = _zip_cache["bytes"]
    try:
        return zipfile.ZipFile(io.BytesIO(raw)), None
    except Exception as exc:
        return None, f"Corrupt GTFS zip: {exc}"


# ---------------------------------------------------------------------------
# In-memory GTFS cache  (today's schedule + lookup tables)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_gtfs: dict = {
    "loaded_date":  None,          # date; rebuilt when date changes
    "stops_by_code": {},           # "2849"     → {stop_id, stop_name, lat, lon}
    "stops_by_id":   {},           # "SPRSHEEF" → {stop_code, stop_name, lat, lon}
    "trips":         {},           # trip_id    → {route_id, service_id}
    "routes":        {},           # route_id   → short_name
    "schedule":      {},           # stop_id    → [(unix_ts, trip_id), …]  ALL today
}


# ---------------------------------------------------------------------------
# GTFS static helpers
# ---------------------------------------------------------------------------

def _hms_to_secs(t: str) -> int:
    """'14:19:00' or '25:30:00' → seconds-from-midnight (handles >24h)."""
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


def _get_active_services(zf: zipfile.ZipFile, names: set,
                          date_str: str, day_name: str) -> set:
    """Return set of active service_ids for the given date/day_name."""
    active: set = set()
    if "calendar.txt" in names:
        with zf.open("calendar.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                if (row.get(day_name, "0") == "1"
                        and row["start_date"] <= date_str <= row["end_date"]):
                    active.add(row["service_id"].strip())
    if "calendar_dates.txt" in names:
        with zf.open("calendar_dates.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                if row["date"] == date_str:
                    sid = row["service_id"].strip()
                    if row["exception_type"] == "1":
                        active.add(sid)
                    elif row["exception_type"] == "2":
                        active.discard(sid)
    return active


def _load_gtfs() -> None:
    """Download (or use cached) GTFS zip and rebuild today's in-memory schedule."""
    today     = datetime.now(PACIFIC).date()
    today_str = today.strftime("%Y%m%d")
    day_name  = today.strftime("%A").lower()

    zf, err = _ensure_zip()
    if err:
        raise RuntimeError(err)
    names = set(zf.namelist())

    # -- stops (include lat/lon for Shortcuts "Get Travel Time") ---------------
    stops_by_code: dict = {}
    stops_by_id:   dict = {}
    with zf.open("stops.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid  = row["stop_id"].strip()
            code = row.get("stop_code", "").strip()
            name = row.get("stop_name", "").strip()
            lat  = _safe_float(row.get("stop_lat", ""))
            lon  = _safe_float(row.get("stop_lon", ""))
            entry = {"stop_code": code, "stop_name": name, "lat": lat, "lon": lon}
            stops_by_id[sid] = entry
            if code:
                stops_by_code[code] = {**entry, "stop_id": sid}

    # -- routes ----------------------------------------------------------------
    routes: dict = {}
    with zf.open("routes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            rid   = row["route_id"].strip()
            short = row.get("route_short_name", "").strip() or rid
            routes[rid] = short

    # -- trips -----------------------------------------------------------------
    trips: dict = {}
    with zf.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            trips[row["trip_id"].strip()] = {
                "route_id":   row["route_id"].strip(),
                "service_id": row["service_id"].strip(),
            }

    # -- active services today -------------------------------------------------
    active       = _get_active_services(zf, names, today_str, day_name)
    active_trips = {tid for tid, t in trips.items() if t["service_id"] in active}
    log.info("Active service IDs today: %d  |  Active trips: %d",
             len(active), len(active_trips))

    # -- stop_times  (only active trips, ALL times — no cutoff here) -----------
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
            secs = _hms_to_secs(raw)
            unix = _secs_to_unix(secs, today)
            schedule[sid].append((unix, tid))

    for sid in schedule:
        schedule[sid].sort()

    log.info("Schedule loaded: %d stops.", len(schedule))

    with _lock:
        _gtfs["loaded_date"]   = today
        _gtfs["stops_by_code"] = stops_by_code
        _gtfs["stops_by_id"]   = stops_by_id
        _gtfs["trips"]         = trips
        _gtfs["routes"]        = routes
        _gtfs["schedule"]      = dict(schedule)


def _safe_float(s: str):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _ensure_gtfs() -> str | None:
    """Reload today's schedule if stale. Returns error string or None."""
    with _lock:
        loaded_date = _gtfs["loaded_date"]
    today = datetime.now(PACIFIC).date()
    if loaded_date != today:
        try:
            _load_gtfs()
        except Exception as exc:
            log.exception("GTFS load failed.")
            return f"Schedule data unavailable: {exc}"
    return None


# ---------------------------------------------------------------------------
# Full-day schedule for arbitrary date  (used by /schedule endpoint)
# ---------------------------------------------------------------------------

def _get_full_day_schedule(stop_id: str, target_date: date) -> tuple:
    """
    Returns (arrivals_list, error_or_None).
    Each arrival dict: {route_short, scheduled_unix, scheduled_time, trip_id}
    Sorted by scheduled_unix.
    Reuses _gtfs routes/trips lookup tables when available.
    """
    today = datetime.now(PACIFIC).date()

    # Fast path: today → use already-parsed schedule
    if target_date == today:
        with _lock:
            if _gtfs["loaded_date"] == today:
                routes = _gtfs["routes"]
                trips  = _gtfs["trips"]
                raw    = _gtfs["schedule"].get(stop_id, [])
                arrivals = []
                for unix_ts, trip_id in raw:
                    route_id = trips.get(trip_id, {}).get("route_id", "?")
                    arrivals.append({
                        "route_short":    routes.get(route_id, route_id),
                        "scheduled_unix": unix_ts,
                        "scheduled_time": _fmt(unix_ts),
                        "trip_id":        trip_id,
                    })
                return arrivals, None

    # Slow path: different date → parse from zip
    zf, err = _ensure_zip()
    if err:
        return [], err

    names    = set(zf.namelist())
    date_str = target_date.strftime("%Y%m%d")
    day_name = target_date.strftime("%A").lower()

    # Reuse cached routes/trips if available
    with _lock:
        cached_routes = dict(_gtfs["routes"]) if _gtfs["loaded_date"] else None
        cached_trips  = dict(_gtfs["trips"])  if _gtfs["loaded_date"] else None

    if cached_routes is None:
        cached_routes = {}
        with zf.open("routes.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                cached_routes[row["route_id"].strip()] = (
                    row.get("route_short_name", "").strip() or row["route_id"].strip()
                )

    if cached_trips is None:
        cached_trips = {}
        with zf.open("trips.txt") as f:
            for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                cached_trips[row["trip_id"].strip()] = {
                    "route_id":   row["route_id"].strip(),
                    "service_id": row["service_id"].strip(),
                }

    active       = _get_active_services(zf, names, date_str, day_name)
    active_trips = {tid for tid, t in cached_trips.items() if t["service_id"] in active}

    arrivals: list = []
    with zf.open("stop_times.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            tid = row["trip_id"].strip()
            if tid not in active_trips:
                continue
            if row["stop_id"].strip() != stop_id:
                continue
            raw = (row.get("arrival_time") or row.get("departure_time") or "").strip()
            if not raw:
                continue
            secs    = _hms_to_secs(raw)
            unix_ts = _secs_to_unix(secs, target_date)
            route_id = cached_trips[tid]["route_id"]
            arrivals.append({
                "route_short":    cached_routes.get(route_id, route_id),
                "scheduled_unix": unix_ts,
                "scheduled_time": _fmt(unix_ts),
                "trip_id":        tid,
            })

    arrivals.sort(key=lambda x: x["scheduled_unix"])
    return arrivals, None


# ---------------------------------------------------------------------------
# Real-time feed
# ---------------------------------------------------------------------------

def _fetch_rt() -> tuple:
    try:
        r = requests.get(GTFS_RT_URL, timeout=10)
        r.raise_for_status()
        return r.json(), None
    except Exception as exc:
        return {}, str(exc)


def _build_rt_index(feed: dict) -> dict:
    """Returns {trip_id: {stop_id: {rt_time, delay}}}."""
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
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=PACIFIC).strftime("%-I:%M %p")


def _route_sort_key(rid: str, routes: dict):
    short = routes.get(rid, rid)
    try:
        return (0, int(short), short)
    except ValueError:
        return (1, 0, short)


# ---------------------------------------------------------------------------
# Plain-text arrivals  (unchanged behaviour from v4)
# ---------------------------------------------------------------------------

def _build_lines(stop_id: str, stop_name: str, rt_idx: dict, now_ts: float) -> list:
    cutoff   = now_ts - GRACE_SECONDS
    schedule = _gtfs["schedule"]
    trips    = _gtfs["trips"]
    routes   = _gtfs["routes"]

    by_route: dict = defaultdict(list)
    for unix_ts, trip_id in schedule.get(stop_id, []):
        if unix_ts < cutoff:
            continue
        route_id = trips.get(trip_id, {}).get("route_id", "?")
        if len(by_route[route_id]) < NUM_ARRIVALS:
            by_route[route_id].append((unix_ts, trip_id))

    if not by_route:
        return ["No upcoming arrivals"]

    lines: list = []
    for idx, route_id in enumerate(
            sorted(by_route, key=lambda r: _route_sort_key(r, routes))):
        short             = routes.get(route_id, route_id)
        include_stop_name = (idx > 0)
        if lines:
            lines.append("")

        for sched_ts, trip_id in by_route[route_id]:
            sched_str = _fmt(sched_ts)
            rt_info   = rt_idx.get(trip_id, {}).get(stop_id)
            if rt_info:
                delay  = rt_info.get("delay") or 0
                rt_str = "On Time" if abs(delay) <= ON_TIME_WINDOW else _fmt(rt_info["rt_time"])
            else:
                rt_str = None

            parts = [f"Route #{short}"]
            if include_stop_name:
                parts.append(stop_name)
            parts.append(sched_str)
            if rt_str:
                parts.append(rt_str)
            lines.append(" // ".join(parts))

    return lines


# ---------------------------------------------------------------------------
# JSON arrivals  (for /next-arrival-json — Apple Shortcuts bus-picker)
# ---------------------------------------------------------------------------

def _build_arrivals_json(stop_id: str, stop_name: str,
                          rt_idx: dict, now_ts: float,
                          stop_lat, stop_lon) -> dict:
    """
    Returns dict ready for JSONResponse.
    Key fields callers need:
      menu_items  : list of strings, one per upcoming bus, for Shortcuts
                    "Choose from List".  Split on ' // ' → 4 parts:
                    [route_label, scheduled_time, status, minutes_until]
      arrivals    : full detail dicts (departure_unix, etc.)
    """
    cutoff   = now_ts - GRACE_SECONDS
    schedule = _gtfs["schedule"]
    trips    = _gtfs["trips"]
    routes   = _gtfs["routes"]

    by_route: dict = defaultdict(list)
    for unix_ts, trip_id in schedule.get(stop_id, []):
        if unix_ts < cutoff:
            continue
        route_id = trips.get(trip_id, {}).get("route_id", "?")
        if len(by_route[route_id]) < NUM_ARRIVALS:
            by_route[route_id].append((unix_ts, trip_id))

    if not by_route:
        return {
            "stop_name":  stop_name,
            "stop_id":    stop_id,
            "stop_lat":   stop_lat,
            "stop_lon":   stop_lon,
            "arrivals":   [],
            "menu_items": [],
            "error":      "No upcoming arrivals",
        }

    arrivals:   list = []
    menu_items: list = []

    for route_id in sorted(by_route,
                            key=lambda r: _route_sort_key(r, routes)):
        short = routes.get(route_id, route_id)

        for sched_ts, trip_id in by_route[route_id]:
            sched_str = _fmt(sched_ts)

            # Real-time
            rt_info    = rt_idx.get(trip_id, {}).get(stop_id)
            delay_secs = 0
            rt_unix    = sched_ts
            on_time    = None
            status     = "Scheduled"   # shown in menu_item column 3

            if rt_info:
                rt_unix    = rt_info["rt_time"]
                delay_secs = int(rt_info.get("delay") or 0)
                on_time    = abs(delay_secs) <= ON_TIME_WINDOW
                if on_time:
                    status = "On Time"
                else:
                    sign   = "+" if delay_secs > 0 else ""
                    status = f"{sign}{round(delay_secs / 60)}min"

            depart_unix   = rt_unix
            minutes_until = max(0, round((depart_unix - now_ts) / 60))

            # Menu string — split on " // " gives exactly 4 parts:
            #   0: "Route #9"
            #   1: "2:19 PM"     (scheduled)
            #   2: "On Time"     (or "+2min" / "Scheduled")
            #   3: "14 min"
            menu_item = (
                f"Route #{short}|{sched_str}|{status}|{minutes_until} min|{trip_id}"
            )

            entry = {
                "route":          short,
                "scheduled_time": sched_str,
                "scheduled_unix": int(sched_ts),
                "realtime_time":  _fmt(rt_unix) if rt_info else None,
                "realtime_unix":  int(rt_unix)  if rt_info else None,
                "departure_unix": int(depart_unix),
                "minutes_until":  minutes_until,
                "delay_seconds":  delay_secs    if rt_info else None,
                "delay_minutes":  round(delay_secs / 60, 1) if rt_info else None,
                "on_time":        on_time,
                "status":         status,
                "trip_id":        trip_id,
                "menu_item":      menu_item,
            }
            arrivals.append(entry)
            menu_items.append(menu_item)

    return {
        "stop_name":  stop_name,
        "stop_id":    stop_id,
        "stop_lat":   stop_lat,
        "stop_lon":   stop_lon,
        "arrivals":   arrivals,
        "menu_items": menu_items,
    }


# ---------------------------------------------------------------------------
# Stop resolver
# ---------------------------------------------------------------------------

def _resolve(raw: str) -> tuple:
    """(stop_id, stop_name, lat, lon) or (None, error_msg, None, None)."""
    s = raw.strip()
    if s.isdigit():
        info = _gtfs["stops_by_code"].get(s)
        if not info:
            return None, f"Stop code {s} not found in schedule data", None, None
        return info["stop_id"], info["stop_name"], info.get("lat"), info.get("lon")
    s = s.upper()
    info = _gtfs["stops_by_id"].get(s)
    if not info:
        return None, f"Stop ID '{s}' not found in schedule data", None, None
    return s, info["stop_name"], info.get("lat"), info.get("lon")


# ---------------------------------------------------------------------------
# High-level helpers used by endpoints
# ---------------------------------------------------------------------------

def get_arrivals(stop_input: str) -> str:
    err = _ensure_gtfs()
    if err:
        return err
    stop_id, result, _, _ = _resolve(stop_input)
    if stop_id is None:
        return result
    feed, _  = _fetch_rt()
    rt_idx   = _build_rt_index(feed) if feed else {}
    now_ts   = datetime.now(timezone.utc).timestamp()
    lines    = _build_lines(stop_id, result, rt_idx, now_ts)
    return "\n".join(lines)


def get_arrivals_json(stop_input: str) -> dict:
    err = _ensure_gtfs()
    if err:
        return {"error": err}
    stop_id, stop_name, lat, lon = _resolve(stop_input)
    if stop_id is None:
        return {"error": stop_name}
    feed, _  = _fetch_rt()
    rt_idx   = _build_rt_index(feed) if feed else {}
    now_ts   = datetime.now(timezone.utc).timestamp()
    return _build_arrivals_json(stop_id, stop_name, rt_idx, now_ts, lat, lon)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Spokane Transit Arrival Board", version="5.0.0")


@app.on_event("startup")
def startup_event():
    """Pre-load GTFS in background so the first request isn't slow."""
    threading.Thread(target=_load_gtfs, daemon=True).start()


@app.get("/", response_class=PlainTextResponse)
def root():
    stops_list = "\n".join(
        f"  {name} — stop {code}" for code, name in HARDCODED_STOPS.items()
    )
    return (
        "Spokane Transit Arrival Board v5\n"
        "─────────────────────────────────\n"
        "\n"
        "Hard-coded stops:\n"
        f"{stops_list}\n"
        "\n"
        "Endpoints:\n"
        "  GET /next-arrival?stop_id=2968\n"
        "  GET /next-arrival-json?stop_id=2968      ← JSON for Shortcuts\n"
        "  GET /eta-routes                           ← ETA route menu list\n"\
        "  GET /eta-plan?from_stop=2968&to_stop=2849&trip_id=XXXX\n"\
        "  GET /schedule?stop_id=2968&date=2025-05-19\n"
        "  GET /schedule?stop_id=2968&date=2025-05-19&format=json\n"
        "  GET /stops                                ← list hard-coded stops\n"
        "  GET /health\n"
    )


@app.get("/stops")
def stops_list():
    """Return hard-coded stops as JSON (useful for Shortcuts menus)."""
    return JSONResponse({
        "hardcoded_stops": [
            {"stop_code": code, "name": name}
            for code, name in HARDCODED_STOPS.items()
        ]
    })


@app.get("/next-arrival", response_class=PlainTextResponse)
def next_arrival(
    stop_id: str = Query(
        ...,
        description="Stop code (e.g. 2849) or alpha stop ID (e.g. SPRSHEEF)",
        min_length=1,
    )
):
    """Plain-text upcoming arrivals — same format as v4."""
    return get_arrivals(stop_id)


@app.get("/next-arrival-json")
def next_arrival_json(
    stop_id: str = Query(
        ...,
        description="Stop code or alpha stop ID",
        min_length=1,
    )
):
    """
    JSON arrivals designed for Apple Shortcuts.

    Key response fields
    -------------------
    menu_items   list[str]   Ready for Shortcuts "Choose from List".
                             Each string splits into 4 parts on " // ":
                             [route_label, scheduled_time, status, minutes_until]
    arrivals     list[dict]  Full detail — departure_unix, on_time, etc.
    stop_lat     float       Use with "Get Travel Time" in Shortcuts
    stop_lon     float
    """
    return JSONResponse(get_arrivals_json(stop_id))


@app.get("/schedule")
def schedule(
    stop_id: str = Query(
        ...,
        description="Stop code (e.g. 2968) or alpha stop ID",
        min_length=1,
    ),
    date: str = Query(
        None,
        description="Date in YYYY-MM-DD format (default: today Pacific time)",
    ),
    format: str = Query(
        "text",
        description="Output format: 'text' (default) or 'json'",
    ),
):
    """
    Full day timetable for a stop on a given date.
    Default date is today (Pacific time).
    Dates must fall within the GTFS feed's active range (typically 30–90 days).
    """
    # Resolve date
    if date is None:
        target_date = datetime.now(PACIFIC).date()
    else:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            msg = "Invalid date — use YYYY-MM-DD (e.g. 2025-05-19)."
            return (JSONResponse({"error": msg}, status_code=400)
                    if format == "json" else PlainTextResponse(msg, status_code=400))

    # Ensure lookup tables are loaded (needed for _resolve)
    err = _ensure_gtfs()
    if err:
        return (JSONResponse({"error": err}, status_code=503)
                if format == "json" else PlainTextResponse(err, status_code=503))

    stop_id_resolved, stop_name, _, _ = _resolve(stop_id)
    if stop_id_resolved is None:
        return (JSONResponse({"error": stop_name}, status_code=404)
                if format == "json" else PlainTextResponse(stop_name, status_code=404))

    arrivals, err = _get_full_day_schedule(stop_id_resolved, target_date)
    if err:
        return (JSONResponse({"error": err}, status_code=503)
                if format == "json" else PlainTextResponse(err, status_code=503))

    date_label = target_date.strftime("%A, %B %-d %Y")

    # ── JSON response ──────────────────────────────────────────────────────
    if format == "json":
        return JSONResponse({
            "stop_name":   stop_name,
            "stop_code":   stop_id,
            "date":        target_date.isoformat(),
            "date_label":  date_label,
            "total_trips": len(arrivals),
            "arrivals":    arrivals,
        })

    # ── Plain-text response ────────────────────────────────────────────────
    if not arrivals:
        return PlainTextResponse(
            f"{stop_name}\n{date_label}\n\nNo service scheduled on this date."
        )

    # Group by route, preserve schedule order within each route
    by_route: dict = defaultdict(list)
    for a in arrivals:
        by_route[a["route_short"]].append(a["scheduled_time"])

    def _rs(r):
        try:
            return (0, int(r))
        except ValueError:
            return (1, r)

    lines = [
        stop_name,
        date_label,
        f"({len(arrivals)} trips scheduled)",
        "",
    ]
    for route_short in sorted(by_route, key=_rs):
        times = by_route[route_short]
        lines.append(f"Route #{route_short}  ({len(times)} trips):")
        for i in range(0, len(times), 4):
            lines.append("  " + "   ".join(times[i:i + 4]))
        lines.append("")

    return PlainTextResponse("\n".join(lines))


# ---------------------------------------------------------------------------
# Hard-coded ETA routes  (for Shortcuts ETA shortcut)
# ---------------------------------------------------------------------------
ETA_ROUTES = [
    {
        "id":           "farr_to_sherman",
        "label":        "Sprague @ Farr → Sprague @ Sherman",
        "from_code":    "2968",
        "to_code":      "2849",
        "from_name":    "Sprague @ Farr (Winco)",
        "to_name":      "Sprague @ Sherman",
        "dest_address": "Elson S. Floyd College of Medicine, 412 E Spokane Falls Blvd, Spokane, WA",
    },
    {
        "id":           "sherman_to_appleway",
        "label":        "Sprague @ Sherman → Appleway @ Farr",
        "from_code":    "2849",
        "to_code":      "2884",
        "from_name":    "Sprague @ Sherman",
        "to_name":      "Appleway @ Farr (Winco)",
        "dest_address": "9717 E Mn Lane, Spokane Valley, WA",
    },
]


@app.get("/eta-routes")
def eta_routes():
    """Return the hard-coded ETA route options for the Shortcuts menu."""
    return JSONResponse({
        "routes": [{"id": r["id"], "label": r["label"]} for r in ETA_ROUTES]
    })


@app.get("/eta-plan")
def eta_plan(
    from_stop: str = Query(..., description="Departure stop code, e.g. 2968"),
    to_stop:   str = Query(..., description="Arrival stop code, e.g. 2849"),
    trip_id:   str = Query(..., description="Trip ID selected by user from /next-arrival-json"),
):
    """
    Given a chosen trip, return the scheduled ride time between two stops
    plus both stops' coordinates so Shortcuts can calculate walk + ride ETA.

    Response fields
    ---------------
    ride_minutes      int     Scheduled minutes between from_stop and to_stop
                              on this trip (null if not found)
    from_lat/lon      float   Departure stop coordinates
    to_lat/lon        float   Arrival stop coordinates
    from_name         str
    to_name           str
    depart_time       str     Scheduled departure from from_stop  e.g. "9:39 PM"
    arrive_time       str     Scheduled arrival   at   to_stop    e.g. "9:47 PM"
    """
    err = _ensure_gtfs()
    if err:
        return JSONResponse({"error": err}, status_code=503)

    # Resolve both stops
    from_id, from_name, from_lat, from_lon = _resolve(from_stop)
    if from_id is None:
        return JSONResponse({"error": from_name}, status_code=404)

    to_id, to_name, to_lat, to_lon = _resolve(to_stop)
    if to_id is None:
        return JSONResponse({"error": to_name}, status_code=404)

    # Find scheduled times for this trip at both stops
    schedule = _gtfs["schedule"]
    from_ts = None
    to_ts   = None

    for unix_ts, tid in schedule.get(from_id, []):
        if tid == trip_id:
            from_ts = unix_ts
            break

    for unix_ts, tid in schedule.get(to_id, []):
        if tid == trip_id:
            to_ts = unix_ts
            break

    ride_minutes = None
    arrive_time  = None
    depart_time  = _fmt(from_ts) if from_ts else None

    if from_ts is not None and to_ts is not None:
        ride_minutes = max(1, round((to_ts - from_ts) / 60))
        arrive_time  = _fmt(to_ts)

    return JSONResponse({
        "trip_id":      trip_id,
        "from_name":    from_name,
        "to_name":      to_name,
        "from_lat":     from_lat,
        "from_lon":     from_lon,
        "to_lat":       to_lat,
        "to_lon":       to_lon,
        "depart_time":  depart_time,
        "arrive_time":  arrive_time,
        "ride_minutes": ride_minutes,
    })


@app.get("/health", response_class=PlainTextResponse)
def health():
    with _lock:
        d = _gtfs["loaded_date"]
    with _zip_lock:
        z = _zip_cache["fetched_date"]
    sched_msg = f"schedule loaded for {d}" if d else "schedule loading…"
    zip_msg   = f"zip cached for {z}"      if z else "zip not yet downloaded"
    return f"ok — {sched_msg} | {zip_msg}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("spokane_transit_arrivals:app", host="0.0.0.0", port=8000, reload=True)
