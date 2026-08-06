#!/usr/bin/env python3
"""
Tesla Inventory Monitor
------------------------
Polls Tesla's public inventory-search API for new/used vehicles matching a
model + trim + drivetrain filter within a radius of a ZIP code, and sends an
SMS (via Twilio) the first time a matching VIN shows up.

Config is read from a .env file (see .env.example) or environment variables.

Usage:
    python3 tesla_monitor.py            # run one check
    python3 tesla_monitor.py --debug    # run one check and dump raw matches

Intended to be run on a schedule via cron (see README.md).
"""

import os
import sys
import json
import time
import argparse
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

import requests

# --------------------------------------------------------------------------
# Config loading (.env file, simple parser so we don't require python-dotenv)
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / ".env"
STATE_PATH = SCRIPT_DIR / "seen_vehicles.json"
LOG_PATH = SCRIPT_DIR / "monitor.log"


def load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_env_file(ENV_PATH)

CONFIG = {
    "ZIP": os.environ.get("ZIP", "94402"),
    "RANGE_MILES": int(os.environ.get("RANGE_MILES", "200")),
    "MODEL": os.environ.get("MODEL", "y"),  # s, 3, x, y
    "CONDITION": os.environ.get("CONDITION", "new"),  # new, used
    "TRIM_KEYWORDS": [
        s.strip().lower()
        for s in os.environ.get("TRIM_KEYWORDS", "performance").split(",")
        if s.strip()
    ],
    # If REQUIRE_RWD=true, a vehicle must look like RWD (and NOT AWD) to match.
    # Default false because Performance is AWD-only today -- flip this on
    # once Tesla ships the RWD Performance trim, or set ALERT_ON_ANY_DRIVETRAIN.
    "REQUIRE_RWD": os.environ.get("REQUIRE_RWD", "false").lower() == "true",
    "TWILIO_ACCOUNT_SID": os.environ.get("TWILIO_ACCOUNT_SID", ""),
    "TWILIO_AUTH_TOKEN": os.environ.get("TWILIO_AUTH_TOKEN", ""),
    "TWILIO_FROM_NUMBER": os.environ.get("TWILIO_FROM_NUMBER", ""),
    "TWILIO_TO_NUMBER": os.environ.get("TWILIO_TO_NUMBER", ""),
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# --------------------------------------------------------------------------
# ZIP -> lat/lng geocoding (free, no API key needed)
# --------------------------------------------------------------------------

def geocode_zip(zip_code: str):
    """Look up lat/lng for a US ZIP using the free Zippopotam.us API."""
    url = f"https://api.zippopotam.us/us/{zip_code}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    place = data["places"][0]
    return float(place["latitude"]), float(place["longitude"])


# --------------------------------------------------------------------------
# Tesla inventory API
# --------------------------------------------------------------------------

def fetch_inventory(zip_code: str, range_miles: int, model: str, condition: str,
                     lat: float, lng: float):
    """Query Tesla's public inventory-results endpoint.

    This is the same undocumented endpoint tesla.com/inventory/ uses in the
    browser. No authentication required. We deliberately do NOT filter by
    TRIM option code server-side (Tesla's internal trim codes shift between
    model years/refreshes); instead we pull all matching-model results in
    range and filter client-side on the human-readable trim text, which is
    more robust to Tesla changing option codes under the hood.
    """
    query = {
        "query": {
            "model": model,
            "condition": condition,
            "options": {},
            "arrangeby": "Price",
            "order": "asc",
            "market": "US",
            "language": "en",
            "super_region": "north america",
            "lng": lng,
            "lat": lat,
            "zip": zip_code,
            "range": range_miles,
            "region": "",
        },
        "offset": 0,
        "count": 100,
        "outsideOffset": 0,
        "outsideSearch": False,
    }

    url = "https://www.tesla.com/inventory/api/v4/inventory-results?query=" + \
        urllib.parse.quote(json.dumps(query))

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def vehicle_matches(vehicle: dict, trim_keywords, require_rwd: bool) -> bool:
    """Text-match trim/drivetrain against the whole vehicle record.

    We dump the record to a lowercase string and search it rather than
    relying on a specific field name, since Tesla's inventory JSON schema
    has changed shape multiple times over the years.
    """
    blob = json.dumps(vehicle).lower()

    if trim_keywords and not any(kw in blob for kw in trim_keywords):
        return False

    if require_rwd:
        # Must look like RWD and NOT all-wheel drive.
        has_rwd = "rwd" in blob or "rear-wheel drive" in blob
        has_awd = "awd" in blob or "all-wheel drive" in blob or "all wheel drive" in blob
        if not has_rwd or has_awd:
            return False

    return True


def describe_vehicle(vehicle: dict) -> str:
    trim = vehicle.get("TrimName") or vehicle.get("Trim") or "Model Y"
    price = vehicle.get("Price") or vehicle.get("InventoryPrice") or "?"
    vin = vehicle.get("VIN", "?")
    city = vehicle.get("metadata", {}).get("cpo", {}).get("city") if isinstance(vehicle.get("metadata"), dict) else None
    city = city or vehicle.get("City") or vehicle.get("location", {}).get("city") if isinstance(vehicle.get("location"), dict) else city
    year = vehicle.get("Year", "")
    link = f"https://www.tesla.com/my/order/{vin}" if vin != "?" else "https://www.tesla.com/inventory/new/my"
    parts = [f"{year} {trim}".strip(), f"${price}", f"VIN {vin}"]
    if city:
        parts.append(str(city))
    parts.append(link)
    return " | ".join(str(p) for p in parts)


# --------------------------------------------------------------------------
# State (dedupe so we only alert once per VIN)
# --------------------------------------------------------------------------

def load_seen():
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    STATE_PATH.write_text(json.dumps(sorted(seen)))


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def send_sms(body: str):
    sid = CONFIG["TWILIO_ACCOUNT_SID"]
    token = CONFIG["TWILIO_AUTH_TOKEN"]
    from_number = CONFIG["TWILIO_FROM_NUMBER"]
    to_number = CONFIG["TWILIO_TO_NUMBER"]

    if not all([sid, token, from_number, to_number]):
        log("SMS not sent: Twilio credentials are not fully configured in .env")
        return False

    try:
        from twilio.rest import Client
    except ImportError:
        log("SMS not sent: 'twilio' package not installed. Run: pip install twilio")
        return False

    client = Client(sid, token)
    # SMS bodies over ~1600 chars get split/rejected; keep it tight.
    body = body[:1500]
    msg = client.messages.create(body=body, from_=from_number, to=to_number)
    log(f"SMS sent (sid={msg.sid})")
    return True


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(debug: bool = False):
    zip_code = CONFIG["ZIP"]
    try:
        lat, lng = geocode_zip(zip_code)
    except Exception as e:
        log(f"ERROR: could not geocode ZIP {zip_code}: {e}")
        return

    try:
        data = fetch_inventory(
            zip_code=zip_code,
            range_miles=CONFIG["RANGE_MILES"],
            model=CONFIG["MODEL"],
            condition=CONFIG["CONDITION"],
            lat=lat,
            lng=lng,
        )
    except Exception as e:
        log(f"ERROR: inventory fetch failed: {e}")
        return

    results = data.get("results", [])
    total = data.get("total_matches_found", len(results))
    log(f"Fetched {len(results)} of {total} total '{CONFIG['MODEL']}' results "
        f"within {CONFIG['RANGE_MILES']}mi of {zip_code}")

    if debug:
        Path(SCRIPT_DIR / "last_raw_response.json").write_text(json.dumps(data, indent=2))
        log("Debug: wrote full raw response to last_raw_response.json")

    matches = [
        v for v in results
        if vehicle_matches(v, CONFIG["TRIM_KEYWORDS"], CONFIG["REQUIRE_RWD"])
    ]
    log(f"{len(matches)} match trim filter {CONFIG['TRIM_KEYWORDS']} "
        f"(require_rwd={CONFIG['REQUIRE_RWD']})")

    seen = load_seen()
    new_matches = [m for m in matches if m.get("VIN", "") not in seen]

    if not new_matches:
        log("No new matching vehicles.")
        return

    log(f"{len(new_matches)} NEW matching vehicle(s) found:")
    for v in new_matches:
        desc = describe_vehicle(v)
        log(f"  -> {desc}")

    if len(new_matches) == 1:
        body = f"Tesla alert: {describe_vehicle(new_matches[0])}"
    else:
        body = f"Tesla alert: {len(new_matches)} new matches near {zip_code}.\n" + \
            "\n".join(describe_vehicle(v) for v in new_matches[:5])
        if len(new_matches) > 5:
            body += f"\n(+{len(new_matches) - 5} more)"

    send_sms(body)

    for v in new_matches:
        seen.add(v.get("VIN", ""))
    save_seen(seen)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="dump raw API response for inspection")
    args = parser.parse_args()
    run(debug=args.debug)
