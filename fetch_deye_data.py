"""
fetch_deye_data.py
------------------
Fetches yesterday's 5-minute interval data from the Deye Cloud API
and writes it to data/latest.json in the format expected by the dashboard.

Environment variables required (set as GitHub Secrets):
  DEYE_APP_ID       - Your DeyeCloud AppId
  DEYE_APP_SECRET   - Your DeyeCloud AppSecret
  DEYE_EMAIL        - Your DeyeCloud account email
  DEYE_PASSWORD     - Your DeyeCloud account password
  DEYE_STATION_ID   - Your station/plant ID (find it via the /station/list endpoint)
"""

import os
import json
import hashlib
import requests
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://eu1-developer.deyecloud.com/v1.0"
APP_ID      = os.environ["DEYE_APP_ID"]
APP_SECRET  = os.environ["DEYE_APP_SECRET"]
EMAIL       = os.environ["DEYE_EMAIL"]
PASSWORD    = os.environ["DEYE_PASSWORD"]
STATION_ID  = os.environ["DEYE_STATION_ID"]

# Output path (relative to repo root — GitHub Actions runs from repo root)
OUTPUT_PATH = "data/latest.json"

# ── Helpers ───────────────────────────────────────────────────────────────────
def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()

def post(path: str, payload: dict, token: str = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(f"{BASE_URL}/{path}", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") not in (0, "0", 200, "200"):
        raise RuntimeError(f"API error on /{path}: {data}")
    return data

# ── Step 1: Authenticate ──────────────────────────────────────────────────────
print("Authenticating with Deye Cloud API...")
auth_resp = post("token", {
    "appId":     APP_ID,
    "appSecret": APP_SECRET,
    "email":     EMAIL,
    "password":  md5(PASSWORD),   # Deye expects MD5-hashed password
})
token = auth_resp["data"]["accessToken"]
print("  ✓ Token obtained")

# ── Step 2: Get station ID if not supplied ────────────────────────────────────
station_id = STATION_ID
if not station_id:
    print("  DEYE_STATION_ID not set — fetching station list...")
    stations = post("station/list", {"page": 1, "size": 10}, token=token)
    station_id = str(stations["data"]["list"][0]["id"])
    print(f"  ✓ Using first station: {station_id}")

# ── Step 3: Fetch yesterday's date ───────────────────────────────────────────
# Run at 23:30 SAST (UTC+2) via cron — we want today's full data.
# To be safe we fetch "today" in UTC+2. GitHub Actions runs in UTC so we add 2h.
now_local = datetime.now(timezone.utc) + timedelta(hours=2)  # SAST offset
target_date = now_local.strftime("%Y-%m-%d")
print(f"  Fetching data for {target_date}...")

# ── Step 4: Fetch 5-minute interval history ───────────────────────────────────
# Endpoint: station/history  (returns time-series power data)
history_resp = post("station/history", {
    "stationId": station_id,
    "startTime": f"{target_date} 00:00:00",
    "endTime":   f"{target_date} 23:59:59",
    "timeType":  1,   # 1 = 5-minute intervals
}, token=token)

raw_records = history_resp["data"]["list"]
print(f"  ✓ Received {len(raw_records)} records")

# ── Step 5: Normalise field names ─────────────────────────────────────────────
# Deye API field names → dashboard field names
# These are the known Deye Cloud API response keys. If your inverter returns
# different keys the script will still work — unmapped fields default to 0.
FIELD_MAP = {
    # Deye key          dashboard key
    "productionPower":  "production_kw",
    "consumptionPower": "consumption_kw",
    "gridPower":        "gridOrMeterPower",   # resolved below
    "purchasePower":    "grid_kw",            # grid import (positive = import)
    "wirePower":        "grid_kw",            # alternative key some firmware uses
    "batteryPower":     "battery_kw",
    "SOC":              "soc_pct",
    "soc":              "soc_pct",
    "pvPower":          "pv_kw",
    "generationPower":  "pv_kw",              # alternative
    "generatorPower":   "generator_kw",
}

def normalise_record(r: dict, time_str: str) -> dict:
    """Map a raw Deye API record to the dashboard schema."""
    out = {
        "time":             time_str,
        "production_kw":    0.0,
        "consumption_kw":   0.0,
        "grid_kw":          0.0,
        "battery_kw":       0.0,
        "soc_pct":          0.0,
        "pv_kw":            0.0,
        "generator_kw":     0,
        "grid_inverter_kw": 0,
    }
    for raw_key, dash_key in FIELD_MAP.items():
        if raw_key in r and r[raw_key] is not None:
            try:
                val = float(r[raw_key])
                # Convert W → kW if values look like watts (> 100 for reasonable solar)
                if dash_key in ("production_kw","consumption_kw","grid_kw",
                                "battery_kw","pv_kw","generator_kw") and abs(val) > 200:
                    val = round(val / 1000, 3)
                out[dash_key] = val
            except (ValueError, TypeError):
                pass

    # Some firmware reports gridOrMeterPower rather than purchasePower.
    # Positive = import from grid, negative = export to grid.
    if "gridOrMeterPower" in out:
        out["grid_kw"] = out.pop("gridOrMeterPower", 0.0)

    # Battery: Deye convention — negative = charging, positive = discharging
    # (same as the dashboard convention, so no inversion needed)
    return out

# Build normalised list
rows = []
for rec in raw_records:
    # Time field names vary: "time", "collectTime", "dateTime"
    time_val = rec.get("time") or rec.get("collectTime") or rec.get("dateTime") or ""
    # Ensure format is "YYYY-MM-DD HH:MM:SS"
    time_str = str(time_val).replace("T", " ")[:19]
    rows.append(normalise_record(rec, time_str))

# Sort chronologically
rows.sort(key=lambda r: r["time"])

print(f"  ✓ Normalised {len(rows)} rows")

# ── Step 6: Write output ──────────────────────────────────────────────────────
os.makedirs("data", exist_ok=True)

output = {
    "date":    target_date,
    "station": station_id,
    "rows":    rows,
}

with open(OUTPUT_PATH, "w") as f:
    json.dump(output, f, separators=(",", ":"))

print(f"  ✓ Written to {OUTPUT_PATH}  ({len(rows)} rows, {os.path.getsize(OUTPUT_PATH):,} bytes)")

# Also write a dated archive copy so you keep history
archive_path = f"data/{target_date}.json"
with open(archive_path, "w") as f:
    json.dump(output, f, separators=(",", ":"))
print(f"  ✓ Archive copy written to {archive_path}")
