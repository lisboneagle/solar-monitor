#!/usr/bin/env python3
"""
fetch_deye_pv_strings.py
Fetches per-string PV data (PV1, PV2) from the Deye consumer web API
(www.deyecloud.com) and writes pv1_kw / pv2_kw into today's data files.

Auth: OAuth2 password flow against www.deyecloud.com using your Deye
      cloud email and password (stored as GitHub secrets).

Called as a step in fetch-deye-data.yml after the main fetch completes.
"""

import json, os, sys, time, datetime, requests

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL   = "https://www.deyecloud.com"
DEVICE_ID  = 10306777   # from HAR: device-s/device/10306777/stats/day
STATION_ID = 62118076
TOKEN_FILE = "data/.deye_web_token.json"   # cache token between runs

EMAIL    = os.environ["DEYE_WEB_EMAIL"]
PASSWORD = os.environ["DEYE_WEB_PASSWORD"]

HEADERS_BASE = {
    "Accept":     "application/json, text/plain, */*",
    "language":   "en",
    "platform":   "Web",
    "system":     "Deye",
    "User-Agent": "Mozilla/5.0 (compatible; solar-monitor-bot/1.0)",
}

# ── Auth ──────────────────────────────────────────────────────────────────────
def load_cached_token():
    try:
        with open(TOKEN_FILE) as f:
            t = json.load(f)
        # Check expiry with 5 min buffer
        if t.get("expires_at", 0) > time.time() + 300:
            print(f"  ✓ Using cached web token (expires in "
                  f"{int((t['expires_at']-time.time())/3600)}h)")
            return t["access_token"]
    except Exception:
        pass
    return None

def save_token(access_token, expires_in):
    os.makedirs("data", exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "access_token": access_token,
            "expires_at":   time.time() + expires_in,
        }, f)

def get_token():
    cached = load_cached_token()
    if cached:
        return cached

    print("  Authenticating with Deye web API...")
    # OAuth2 password grant — same client_id seen in JWT from HAR
    resp = requests.post(
        f"{BASE_URL}/uc-s/oauth/token",
        headers={**HEADERS_BASE, "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "password",
            "client_id":  "test",
            "username":   f"0_{EMAIL}_2",   # format seen in JWT: 0_email_2
            "password":   PASSWORD,
            "scope":      "all",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Auth failed: {resp.status_code} {resp.text[:200]}")

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {data}")

    expires_in = data.get("expires_in", 3600 * 24 * 7)
    save_token(token, expires_in)
    print(f"  ✓ Web token obtained (expires in {expires_in//3600}h)")
    return token

# ── Fetch per-string data ─────────────────────────────────────────────────────
def fetch_pv_strings(token, date_str):
    """
    Fetch DP1/DP2 (DC Power PV1/PV2) from stats/day endpoint.
    date_str: "YYYY/MM/DD"
    Returns list of {time_unix, pv1_w, pv2_w} dicts.
    """
    url = f"{BASE_URL}/device-s/device/{DEVICE_ID}/stats/day"
    resp = requests.get(
        url,
        headers={
            **HEADERS_BASE,
            "Authorization": f"bearer {token}",
            "Referer": f"{BASE_URL}/station/device?id={STATION_ID}",
        },
        params={"day": date_str, "lan": "en"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"stats/day failed: {resp.status_code} {resp.text[:200]}")

    items = resp.json()
    if not isinstance(items, list):
        raise RuntimeError(f"Unexpected response format: {type(items)}")

    # Find DP1 and DP2 series
    dp1 = dp2 = None
    for item in items:
        sn = item.get("storageName", "")
        if sn == "DP1":
            dp1 = {d["collectionTime"]: float(d["value"])
                   for d in item.get("detailList", []) if d.get("value") not in (None, "--")}
        elif sn == "DP2":
            dp2 = {d["collectionTime"]: float(d["value"])
                   for d in item.get("detailList", []) if d.get("value") not in (None, "--")}

    if dp1 is None or dp2 is None:
        raise RuntimeError(f"DP1/DP2 not found in response. "
                           f"Available: {[i.get('storageName') for i in items[:10]]}")

    # Merge by timestamp
    all_times = sorted(set(dp1) | set(dp2))
    results = []
    for t in all_times:
        results.append({
            "time_unix": t,
            "pv1_w": dp1.get(t, 0.0),
            "pv2_w": dp2.get(t, 0.0),
        })

    print(f"  ✓ Fetched {len(results)} per-string readings "
          f"(PV1 max: {max((r['pv1_w'] for r in results), default=0):.0f}W, "
          f"PV2 max: {max((r['pv2_w'] for r in results), default=0):.0f}W)")
    return results

# ── Merge into existing JSON ──────────────────────────────────────────────────
def ts_to_sast(unix_ts):
    """Convert Unix timestamp to SAST datetime string (UTC+2)."""
    return datetime.datetime.utcfromtimestamp(unix_ts + 7200).strftime("%Y-%m-%d %H:%M:%S")

def merge_pv_strings(pv_data, json_path):
    """
    Merge pv1_kw / pv2_kw into rows of an existing data JSON file.
    Matches by closest timestamp within 3 minutes.
    """
    if not os.path.exists(json_path):
        print(f"  ⚠ {json_path} not found — skipping merge")
        return

    with open(json_path) as f:
        data = json.load(f)
    rows = data.get("rows", data) if isinstance(data, dict) else data

    if not rows:
        return

    # Build lookup: SAST time string → (pv1_w, pv2_w)
    pv_lookup = {}
    for p in pv_data:
        ts_str = ts_to_sast(p["time_unix"])
        pv_lookup[ts_str] = (p["pv1_w"], p["pv2_w"])

    matched = 0
    for row in rows:
        row_time = row.get("time", "")
        # Try exact match first, then ±1 minute
        best = None
        for delta in range(0, 181, 60):   # 0, 1, 2, 3 minutes
            for sign in ([0] if delta == 0 else [1, -1]):
                try:
                    dt = datetime.datetime.strptime(row_time, "%Y-%m-%d %H:%M:%S")
                    candidate = (dt + datetime.timedelta(seconds=sign * delta)
                                 ).strftime("%Y-%m-%d %H:%M:%S")
                    if candidate in pv_lookup:
                        best = pv_lookup[candidate]
                        break
                except Exception:
                    pass
            if best:
                break

        if best:
            row["pv1_kw"] = round(best[0] / 1000, 3)
            row["pv2_kw"] = round(best[1] / 1000, 3)
            matched += 1
        else:
            # Fallback: 50/50 split
            pv_total = row.get("pv_kw", 0)
            row["pv1_kw"] = round(pv_total / 2, 3)
            row["pv2_kw"] = round(pv_total / 2, 3)

    print(f"  ✓ Merged pv1/pv2 into {json_path}: "
          f"{matched}/{len(rows)} rows matched exactly")

    # Write back
    if isinstance(data, dict):
        data["rows"] = rows
        out = data
    else:
        out = rows

    with open(json_path, "w") as f:
        json.dump(out, f, separators=(",", ":"))

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # SAST today
    now_sast = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    date_str  = now_sast.strftime("%Y/%m/%d")   # e.g. "2026/05/22"
    date_key  = now_sast.strftime("%Y-%m-%d")   # e.g. "2026-05-22"

    print(f"\n  Fetching per-string PV data for {date_key} from Deye web API...")

    try:
        token   = get_token()
        pv_data = fetch_pv_strings(token, date_str)

        # Merge into both latest.json and today's archive
        merge_pv_strings(pv_data, "data/latest.json")
        merge_pv_strings(pv_data, f"data/{date_key}.json")

    except Exception as e:
        print(f"  ⚠ Per-string PV fetch failed (non-critical): {e}")
        print(f"    PV1/PV2 will fall back to 50/50 split in latest.json")
        # Ensure pv1_kw/pv2_kw exist even on failure
        try:
            with open("data/latest.json") as f:
                data = json.load(f)
            rows = data.get("rows", data) if isinstance(data, dict) else data
            for row in rows:
                if "pv1_kw" not in row:
                    row["pv1_kw"] = round(row.get("pv_kw", 0) / 2, 3)
                    row["pv2_kw"] = round(row.get("pv_kw", 0) / 2, 3)
            if isinstance(data, dict):
                data["rows"] = rows
            with open("data/latest.json", "w") as f:
                json.dump(data if isinstance(data, dict) else rows, f, separators=(",", ":"))
        except Exception:
            pass

if __name__ == "__main__":
    main()
