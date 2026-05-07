"""
fetch_deye_data.py
------------------
Fetches today's 5-minute interval data from the Deye Cloud API
and writes it to data/latest.json in the format expected by the dashboard.

Environment variables required (set as GitHub Secrets):
  DEYE_APP_ID       - Your DeyeCloud AppId
  DEYE_APP_SECRET   - Your DeyeCloud AppSecret
  DEYE_EMAIL        - Your DeyeCloud account email
  DEYE_PASSWORD     - Your DeyeCloud account password (plain text)
  DEYE_STATION_ID   - Your station/plant ID (leave blank to auto-discover)
  DEYE_REGION       - Optional: eu1 (default), us1 etc.
"""

import os
import json
import hashlib
import requests
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
REGION      = os.environ.get("DEYE_REGION", "eu1")
BASE_URL    = f"https://{REGION}-developer.deyecloud.com/v1.0"
APP_ID      = os.environ["DEYE_APP_ID"]
APP_SECRET  = os.environ["DEYE_APP_SECRET"]
EMAIL       = os.environ["DEYE_EMAIL"]
PASSWORD    = os.environ["DEYE_PASSWORD"]
STATION_ID  = os.environ.get("DEYE_STATION_ID", "")
OUTPUT_PATH = "data/latest.json"

# ── Helpers ───────────────────────────────────────────────────────────────────
def sha256(s):
    h = hashlib.sha256()
    h.update(s.encode("utf-8"))
    return h.hexdigest()

def is_success(data):
    code = str(data.get("code", ""))
    return data.get("success", False) or code in ("0", "200", "1000000")

def post(path, payload, token=None, query_params=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"bearer {token}"
    url = f"{BASE_URL}/{path}"
    if query_params:
        url += "?" + "&".join(f"{k}={v}" for k, v in query_params.items())
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(f"    → /{path} code: {data.get('code')} success: {data.get('success')}")
    if not is_success(data):
        raise RuntimeError(f"API error on /{path}: {data}")
    return data

# ── Step 1: Authenticate ──────────────────────────────────────────────────────
# Per official sample code:
# - appId goes as a URL query parameter
# - appSecret, email, password, companyId go in the request body
# - password must be SHA256 hashed
print(f"Authenticating with Deye Cloud API ({BASE_URL})...")
auth_resp = post(
    "account/token",
    query_params={"appId": APP_ID},
    payload={
        "appSecret":  APP_SECRET,
        "email":      EMAIL,
        "password":   sha256(PASSWORD),
        "companyId":  "0",
    }
)

# accessToken may be at root level or nested under "data"
token_data = auth_resp.get("data") or auth_resp
raw_token = token_data["accessToken"]
token = raw_token.replace("Bearer ", "").replace("bearer ", "").strip()
print(f"  ✓ Token obtained")

# ── Step 2: Get station ID if not supplied ────────────────────────────────────
station_id = STATION_ID.strip()
if not station_id:
    print("  DEYE_STATION_ID not set — fetching station list...")
    stations = post("station/list", {"page": 1, "size": 10}, token=token)
    items = (stations.get("stationList") or
             stations.get("data", {}).get("list") or
             stations.get("data", {}).get("stationList") or
             stations.get("list") or [])
    if not items:
        raise RuntimeError(f"No stations found. Full response: {json.dumps(stations)[:500]}")
    station_id = str(items[0].get("id") or items[0].get("stationId"))
    print(f"  ✓ Available stations: {[str(s.get('id') or s.get('stationId')) for s in items]}")
    print(f"  ✓ Using station: {station_id}  ← add this as DEYE_STATION_ID secret")

# ── Step 3: Target date (SAST = UTC+2) ───────────────────────────────────────
now_local   = datetime.now(timezone.utc) + timedelta(hours=2)
target_date = now_local.strftime("%Y-%m-%d")
print(f"  Fetching data for {target_date} (station {station_id})...")

# ── Step 4: Fetch 5-minute history ───────────────────────────────────────────
history_resp = None
attempts = [
    ("station/history", {
        "stationId": station_id,
        "startTime": f"{target_date} 00:00:00",
        "endTime":   f"{target_date} 23:59:59",
        "timeType":  1,
    }),
    ("station/day", {
        "stationId": station_id,
        "time": target_date,
    }),
    ("device/history", {
        "stationId": station_id,
        "startTime": f"{target_date} 00:00:00",
        "endTime":   f"{target_date} 23:59:59",
        "timeType":  1,
    }),
]

for endpoint, payload in attempts:
    try:
        print(f"  Trying /{endpoint}...")
        history_resp = post(endpoint, payload, token=token)
        print(f"  ✓ Success with /{endpoint}")
        break
    except Exception as e:
        print(f"  ✗ Failed: {e}")

if not history_resp:
    raise RuntimeError("All history endpoints failed.")

data_block  = history_resp.get("data", {})
raw_records = (data_block.get("list") or data_block.get("infos") or
               data_block.get("records") or
               (data_block if isinstance(data_block, list) else []))
print(f"  ✓ Received {len(raw_records)} records")

if len(raw_records) == 0:
    print(f"  ⚠ Raw response: {json.dumps(history_resp)[:800]}")

# ── Step 5: Normalise field names ─────────────────────────────────────────────
FIELD_MAP = {
    "generationPower":  "production_kw",
    "productionPower":  "production_kw",
    "consumptionPower": "consumption_kw",
    "purchasePower":    "grid_kw",
    "wirePower":        "grid_kw",
    "gridPower":        "grid_kw",
    "batteryPower":     "battery_kw",
    "SOC":              "soc_pct",
    "soc":              "soc_pct",
    "batterySoc":       "soc_pct",
    "pvPower":          "pv_kw",
    "generatorPower":   "generator_kw",
}

def normalise(r, time_str):
    out = {"time": time_str, "production_kw": 0.0, "consumption_kw": 0.0,
           "grid_kw": 0.0, "battery_kw": 0.0, "soc_pct": 0.0,
           "pv_kw": 0.0, "generator_kw": 0, "grid_inverter_kw": 0}
    for k, dk in FIELD_MAP.items():
        if k in r and r[k] is not None:
            try:
                val = float(r[k])
                if dk != "soc_pct" and abs(val) > 200:
                    val = round(val / 1000, 3)
                out[dk] = val
            except (ValueError, TypeError):
                pass
    return out

rows = []
for rec in raw_records:
    t = (rec.get("time") or rec.get("collectTime") or
         rec.get("dateTime") or rec.get("date") or "")
    rows.append(normalise(rec, str(t).replace("T", " ")[:19]))

rows.sort(key=lambda r: r["time"])
print(f"  ✓ Normalised {len(rows)} rows")
if rows:
    print(f"  Sample: {json.dumps(rows[0])}")

# ── Step 6: Write output ──────────────────────────────────────────────────────
os.makedirs("data", exist_ok=True)
output = {"date": target_date, "station": station_id, "rows": rows}

with open(OUTPUT_PATH, "w") as f:
    json.dump(output, f, separators=(",", ":"))
print(f"  ✓ {OUTPUT_PATH} written ({len(rows)} rows, {os.path.getsize(OUTPUT_PATH):,} bytes)")

with open(f"data/{target_date}.json", "w") as f:
    json.dump(output, f, separators=(",", ":"))
print(f"  ✓ Archive: data/{target_date}.json")
