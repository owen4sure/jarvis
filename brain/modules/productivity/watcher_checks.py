"""Per-type condition checks for `watcher_manager.py`.

`check(watcher)` returns `None` (nothing to report) or
`{"message": str | None, "updates": dict | None}`:
- `message` is sent to the user if not None.
- `updates` is merged into the watcher's stored dict (e.g. to remember
  "already fired" / "last seen alert id" state).
"""

import json
import os
import time
import urllib.request
from datetime import datetime

from modules.productivity import activity_tracker, emergency_contact

CRYPTO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

# stooq.com gives free, no-key delayed quotes as CSV. Symbol format e.g.
# "aapl.us", "2330.tw" - see https://stooq.com/q/ for lookup.
STOCK_QUOTE_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2c&h&e=csv"

# Free registration at https://opendata.cwa.gov.tw/ -> config/cwa.json:
#   {"api_key": "CWA-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
CWA_CONFIG_PATH = "/Users/USERNAME/Hermes_Brain/config/cwa.json"
CWA_EARTHQUAKE_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/E-A0015-001"
CWA_TYPHOON_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0034-005"

# OpenSky Network: free, no key, but anonymous accounts are limited to
# ~100 requests/day. Each check fetches the *entire* global state vector
# (one request), so flight watchers are only actually checked once every
# FLIGHT_CHECK_INTERVAL_SECONDS regardless of the 60s daemon loop, to stay
# well under that limit (96 calls/day at 15-minute intervals).
FLIGHT_STATES_URL = "https://opensky-network.org/api/states/all"
FLIGHT_CHECK_INTERVAL_SECONDS = 900


def check(watcher):
    if watcher["type"] == "crypto":
        return _check_crypto(watcher)
    if watcher["type"] == "stock":
        return _check_stock(watcher)
    if watcher["type"] == "earthquake":
        return _check_earthquake(watcher)
    if watcher["type"] == "typhoon":
        return _check_typhoon(watcher)
    if watcher["type"] == "flight":
        return _check_flight(watcher)
    if watcher["type"] == "late_night_checkin":
        return _check_late_night(watcher)
    return None


def _threshold_alert(label, value, watcher, unit):
    condition = watcher["condition"]
    threshold = watcher["price"]
    is_met = (value >= threshold) if condition == "above" else (value <= threshold)
    already_fired = watcher.get("fired", False)

    if is_met and not already_fired:
        message = (
            f"🚨 價格警示：{label} 現在 {value:,} {unit}，"
            f"已 {'超過' if condition == 'above' else '低於'} 你設定的 {threshold:,} {unit}"
        )
        return {"message": message, "updates": {"fired": True}}

    if not is_met and already_fired:
        return {"message": None, "updates": {"fired": False}}

    return None


def _check_crypto(watcher):
    coin_id = watcher["coin_id"]
    vs_currency = watcher.get("vs_currency", "usd")

    url = f"{CRYPTO_PRICE_URL}?ids={coin_id}&vs_currencies={vs_currency}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.load(resp)

    if coin_id not in data:
        return {"message": None, "updates": None}

    price = data[coin_id][vs_currency]
    return _threshold_alert(coin_id, price, watcher, vs_currency.upper())


def _check_stock(watcher):
    symbol = watcher["symbol"]
    url = STOCK_QUOTE_URL.format(symbol=symbol)
    with urllib.request.urlopen(url, timeout=8) as resp:
        lines = resp.read().decode("utf-8").splitlines()

    if len(lines) < 2:
        return None

    fields = lines[1].split(",")
    if len(fields) < 3 or fields[2] in ("N/D", ""):
        return None  # symbol not found / market closed with no data yet

    price = float(fields[2])
    return _threshold_alert(symbol, price, watcher, "")


def _load_cwa_key():
    if not os.path.exists(CWA_CONFIG_PATH):
        return None
    with open(CWA_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f).get("api_key")


def _check_earthquake(watcher):
    api_key = _load_cwa_key()
    if not api_key:
        # Not configured - silently skip (no spam every cycle). The /watch
        # command tells the user how to set this up when they add it.
        return None

    min_magnitude = watcher.get("min_magnitude", 4.0)
    url = f"{CWA_EARTHQUAKE_URL}?Authorization={api_key}&limit=1"
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.load(resp)

    records = data.get("records", {}).get("Earthquake", [])
    if not records:
        return None

    latest = records[0]
    report_id = latest.get("EarthquakeNo")
    if report_id == watcher.get("last_report_id"):
        return None  # already alerted for this report

    info = latest.get("EarthquakeInfo", {})
    magnitude = info.get("EarthquakeMagnitude", {}).get("MagnitudeValue", 0)
    location = info.get("Epicenter", {}).get("Location", "未知地點")
    updates = {"last_report_id": report_id}

    if magnitude < min_magnitude:
        return {"message": None, "updates": updates}

    message = (
        f"🚨 地震速報：{location} 發生 {magnitude} 級地震 "
        f"(報告編號 {report_id})"
    )
    return {"message": message, "updates": updates}


def _check_typhoon(watcher):
    api_key = _load_cwa_key()
    if not api_key:
        # Same as earthquake: silently skip until config/cwa.json exists.
        return None

    url = f"{CWA_TYPHOON_URL}?Authorization={api_key}"
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.load(resp)

    typhoons = data.get("records", {}).get("tropicalCyclones", {}).get("tropicalCyclone", [])
    if not typhoons:
        # No active typhoon -> reset so the next one always alerts.
        if watcher.get("last_report_id"):
            return {"message": None, "updates": {"last_report_id": None}}
        return None

    active = typhoons[0]
    report_id = active.get("cwaTyphoonName") or active.get("typhoonName")
    if report_id == watcher.get("last_report_id"):
        return None

    name = active.get("cwaTyphoonName", active.get("typhoonName", "未知颱風"))
    message = f"🚨 颱風警報：{name} 已發布最新警報，請查看中央氣象署網站確認路徑與防颱準備。"
    return {"message": message, "updates": {"last_report_id": report_id}}


def _check_flight(watcher):
    now = time.time()
    last_checked = watcher.get("last_checked", 0)
    if now - last_checked < FLIGHT_CHECK_INTERVAL_SECONDS:
        return None

    callsign = watcher["callsign"].strip().upper()
    with urllib.request.urlopen(FLIGHT_STATES_URL, timeout=15) as resp:
        data = json.load(resp)

    updates = {"last_checked": now}
    for state in data.get("states") or []:
        state_callsign = (state[1] or "").strip().upper()
        if state_callsign != callsign:
            continue

        on_ground = state[8]
        prev_on_ground = watcher.get("on_ground")
        updates["on_ground"] = on_ground

        if prev_on_ground is not None and on_ground != prev_on_ground:
            status = "已降落" if on_ground else "已起飛"
            message = f"✈️ 航班 {watcher['callsign']} {status}"
            return {"message": message, "updates": updates}

        return {"message": None, "updates": updates}

    # Not currently in OpenSky's coverage (e.g. not yet departed, or
    # landed and out of range). Just record that we checked.
    return {"message": None, "updates": updates}


def _check_late_night(watcher):
    """If the user hasn't sent the Telegram bot a single message all day
    by `cutoff` (default 23:30), notify the registered emergency contact
    directly (not the user - they're the one not responding)."""
    cutoff = watcher.get("cutoff", "23:30")
    now = datetime.now()
    if now.strftime("%H:%M") < cutoff:
        return None

    today = now.strftime("%Y-%m-%d")
    if watcher.get("last_checked_date") == today:
        return None  # already handled today

    updates = {"last_checked_date": today}

    if activity_tracker.last_activity_date() == today:
        return {"message": None, "updates": updates}

    contact = emergency_contact.get_contact()
    if not contact:
        return {"message": None, "updates": updates}

    from modules.remote.telegram_handler import TelegramHandler

    name = contact.get("name") or "他/她"
    text = f"🌙 提醒：今天到 {cutoff} 為止都沒有收到任何訊息，請關心一下{name}。"
    try:
        TelegramHandler().send_message(contact["chat_id"], text)
    except Exception as e:
        print(f"⚠️ [WatcherChecks] 通知緊急聯絡人失敗: {e}")

    return {"message": None, "updates": updates}
