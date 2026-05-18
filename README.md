[shortcut_guide.md](https://github.com/user-attachments/files/27950504/shortcut_guide.md)
# Spokane Transit — Apple Shortcuts Rebuild Guide (v5)

**Base URL:** `https://transit-schedule.onrender.com`

---

## What's New in v5

| Feature | How it works |
|---|---|
| **Bus Picker** | `/next-arrival-json` returns a `menu_items` list — plug straight into "Choose from List" |
| **Departure countdown** | Every menu item includes minutes until the bus (e.g. `14 min`), calculated at fetch time |
| **Stop coordinates** | JSON includes `stop_lat` / `stop_lon` so you can feed "Get Travel Time" |
| **Full day schedule** | `/schedule?stop_id=X&date=YYYY-MM-DD` — all trips for a stop on any date |
| **Hard-coded stops** | 2968, 2849, 2884 built into the server; manual entry still works |

---

## Menu Item Format (Bus Picker)

Every string in `menu_items` uses `//` as a delimiter with **exactly 4 parts**:

```
Route #9 // 2:19 PM // On Time // 14 min
Route #9 // 2:49 PM // +2min   // 44 min
Route #12 // 3:05 PM // Scheduled // 60 min
```

Split on ` // ` → index 0 = route, index 1 = scheduled time, index 2 = status, index 3 = countdown.

---

## Shortcut A — Quick Arrival Check (Plain Text)

> Tap a stop, see the next 3 buses per route as plain text.

### Steps

1. **Menu** — "Choose from Menu"
   - Sprague @ Farr (Winco)
   - Sprague @ Sherman
   - Appleway @ Farr (Winco)
   - Enter stop manually

2. **Set Variable `stop_code`**
   - Sprague @ Farr → `2968`
   - Sprague @ Sherman → `2849`
   - Appleway @ Farr → `2884`
   - Manual → "Ask for Input" (text, prompt: "Enter stop number")

3. **Get Contents of URL**
   ```
   https://transit-schedule.onrender.com/next-arrival?stop_id=[stop_code]
   ```

4. **Show Result** (Quick Look or Show Notification)

---

## Shortcut B — Bus Picker + Departure Countdown

> See all upcoming buses as a list, tap one, and get a clear departure summary
> with optional ETA to your destination.

### Steps

1. **Choose stop** (same menu as Shortcut A → set `stop_code`)

2. **Get Contents of URL**
   ```
   https://transit-schedule.onrender.com/next-arrival-json?stop_id=[stop_code]
   ```
   - Method: GET
   - *(No headers needed)*

3. **Get Dictionary from Input** ← pass the URL contents in

4. **Get Value for Key `menu_items`** from Dictionary
   → This gives you a **List**

5. **Choose from List** ← pass the List in
   > Each item looks like: `Route #9 // 2:19 PM // On Time // 14 min`
   > The user taps their bus.

6. **Split Text** ← pass the chosen item, split by ` // `
   > Now you have a list of 4 text items

7. **Set Variable `route_label`** = Get Item 1 from Split Text
8. **Set Variable `sched_time`**  = Get Item 2 from Split Text
9. **Set Variable `rt_status`**   = Get Item 3 from Split Text
10. **Set Variable `mins_until`** = Get Item 4 from Split Text

11. **Show Alert / Notification:**
    ```
    [route_label] departs [sched_time]
    Status: [rt_status]
    Time until bus: [mins_until]
    ```

---

### Optional Add-on: ETA to Destination

After step 10, add these steps to calculate when you'll arrive:

11. **Get Travel Time**
    - From: Current Location
    - To: *(your destination address)*
    - Mode: Transit *(or Walking if destination is near the stop)*
    → Set Variable `travel_duration` (Shortcuts returns this in seconds)

12. **Calculate** `travel_duration / 60`
    → Set Variable `ride_minutes`

13. **Show Alert:**
    ```
    Board [route_label] at [sched_time] ([mins_until] away)
    Travel time: ~[ride_minutes] min
    Estimated arrival: [sched_time] + [ride_minutes] min
    ```

> **Tip:** To get precise walk time to the stop, add a second "Get Travel Time"
> from Current Location to the stop's address (e.g. "Sprague Ave & Farr Rd,
> Spokane Valley") using Walking mode before step 11. Subtract that from
> `mins_until` to know your latest departure time from home.

---

## Shortcut C — Full Day Schedule

> Pick a stop and a date; see every scheduled trip for that day, grouped by route.

### Steps

1. **Choose stop** (same menu → `stop_code`)

2. **Choose date** — "Choose from Menu"
   - Today
   - Tomorrow
   - Pick a date

   **If Today:**
   - Format Date: Current Date → Custom format `yyyy-MM-dd`
   - Set Variable `date_str`

   **If Tomorrow:**
   - Adjust Date: Current Date, Add 1 Day
   - Format Date → `yyyy-MM-dd`
   - Set Variable `date_str`

   **If Pick a date:**
   - Ask for Input — Input Type: **Date**  ← shows native date picker
   - Format Date (the result) → `yyyy-MM-dd`
   - Set Variable `date_str`

3. **Get Contents of URL**
   ```
   https://transit-schedule.onrender.com/schedule?stop_id=[stop_code]&date=[date_str]
   ```

4. **Show Result** (Quick Look)

---

## Shortcut D — Day Schedule as JSON (for advanced use)

If you want to process the schedule in Shortcuts rather than just display it:

1. Same as Shortcut C steps 1–3, but append `&format=json` to the URL.

2. **Get Dictionary from Input**

3. **Get Value for Key `total_trips`** → show count
4. **Get Value for Key `arrivals`** → List of Dictionaries

Each arrival dict has:
```
route_short      "9"
scheduled_time   "6:15 AM"
scheduled_unix   1747616100
trip_id          "T-1234"
```

---

## Reference: All Endpoints

| Endpoint | Returns | Use for |
|---|---|---|
| `/next-arrival?stop_id=X` | Plain text | Quick display |
| `/next-arrival-json?stop_id=X` | JSON | Bus picker, ETA calc |
| `/schedule?stop_id=X&date=YYYY-MM-DD` | Plain text | Day view |
| `/schedule?stop_id=X&date=YYYY-MM-DD&format=json` | JSON | Advanced processing |
| `/stops` | JSON | Auto-build stop menus |
| `/health` | Plain text | Debugging |

## Hard-Coded Stop Codes

| Stop | Code |
|---|---|
| Sprague @ Farr (Winco) | **2968** |
| Sprague @ Sherman | **2849** |
| Appleway @ Farr (Winco) | **2884** |
| Any other stop | Use the number on the sign |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No upcoming arrivals" | You're past the last bus for the day, or it's a Sunday/holiday with reduced service |
| Empty menu_items | Same — try `/schedule` to see if any buses run today |
| Schedule for a future date shows nothing | Date may be outside GTFS coverage (~90 days); check `/health` |
| Server cold-starts slow | Render.com free tier spins down after 15 min of inactivity — first request takes ~30s |
| Stop code not found | Verify the code on the physical sign; alpha stop IDs (e.g. SPRSHEEF) also work |
