"""Calendar sync - "家人行程關注" wish-list item.

Supports TWO backends (tried in order):

━━━ Option A: ICS URL (Recommended - zero credentials, immediate) ━━━
Any calendar app can share a read-only subscribe URL (.ics format):
  - Google Calendar: Settings → [Calendar name] → "Secret address in
    iCal format" → copy the webcal:// link
  - Apple Calendar: Calendar → Share Calendar → Public Calendar → copy
  - Any CalDAV service: look for "Calendar Subscription URL"

Create config/calendar_ics.json (already gitignored):
  {"url": "webcal://calendar.google.com/calendar/ical/xxx/private-xxx/basic.ics"}
  (replace "webcal://" with "https://" — both work)

━━━ Option B: Google Calendar OAuth (more powerful but needs setup) ━━━
1. Google Cloud Console → create project → enable Calendar API →
   create OAuth client ID (Desktop app) → download JSON as
   config/google_credentials.json (see config/google_credentials.json.example).
2. Run: ./.venv/bin/python -m scripts.google_calendar_oauth_setup
   (one-time browser login → saves config/google_token.json, auto-refreshes).
3. Allows reading *all* calendars on the account (including shared ones).

If neither is configured, upcoming_events() raises CalendarNotConfiguredError.
family_schedule_check() silently returns None in that case (no nag).
"""

import datetime
import json
import os
import re
import urllib.request

BASE_DIR = "/Users/USERNAME/Hermes_Brain"
CREDENTIALS_PATH = os.path.join(BASE_DIR, "config", "google_credentials.json")
TOKEN_PATH = os.path.join(BASE_DIR, "config", "google_token.json")
ICS_CONFIG_PATH = os.path.join(BASE_DIR, "config", "calendar_ics.json")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class CalendarNotConfiguredError(Exception):
    pass


# ── ICS backend ───────────────────────────────────────────────────────────────

def _fetch_ics(url: str) -> str:
    url = url.replace("webcal://", "https://", 1)
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_ics(ics_text: str, days: int = 1) -> list[dict]:
    """Minimal stdlib ICS parser — extracts DTSTART + SUMMARY from VEVENTs
    that fall within the next `days` days. No external dependencies."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now + datetime.timedelta(days=days)

    # Include events starting from the beginning of today (UTC) through cutoff,
    # so events already in progress or started earlier today are still shown.
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    events = []
    in_event = False
    current: dict = {}

    for raw_line in ics_text.splitlines():
        line = raw_line.strip()
        if line == "BEGIN:VEVENT":
            in_event = True
            current = {}
        elif line == "END:VEVENT":
            in_event = False
            if "summary" in current and "start" in current:
                events.append(current)
            current = {}
        elif in_event:
            if ":" not in line:
                continue
            key_raw, _, value = line.partition(":")
            key = key_raw.split(";")[0].upper()
            if key == "SUMMARY":
                current["summary"] = value
            elif key == "DTSTART":
                dt = _parse_ics_datetime(value)
                if dt and today_start <= dt <= cutoff:
                    current["start"] = dt.strftime("%Y-%m-%dT%H:%M")

    events.sort(key=lambda e: e.get("start", ""))
    return events


def _parse_ics_datetime(value: str):
    """Parse DTSTART value: YYYYMMDDTHHMMSSZ or YYYYMMDD (all-day)."""
    value = value.strip()
    try:
        if "T" in value:
            base = re.sub(r"[^0-9T]", "", value)
            dt = datetime.datetime.strptime(base[:15], "%Y%m%dT%H%M%S")
            if value.endswith("Z"):
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        else:
            base = re.sub(r"[^0-9]", "", value)[:8]
            return datetime.datetime.strptime(base, "%Y%m%d").replace(
                tzinfo=datetime.timezone.utc
            )
    except Exception:
        return None


def _ics_upcoming_events(days: int = 1) -> list[dict]:
    if not os.path.exists(ICS_CONFIG_PATH):
        raise CalendarNotConfiguredError(
            "尚未設定行事曆。最簡單的方法：\n"
            "1. 在 Google 日曆 → 設定 → [行事曆名稱] → 找到「以 iCal 格式提供的秘密位址」\n"
            "2. 複製 webcal:// 連結（或 https:// 版本）\n"
            "3. 建立 config/calendar_ics.json：{\"url\": \"https://...\"}\n"
            "（無需 Google Cloud 帳號或 OAuth，複製 URL 即可使用）"
        )
    with open(ICS_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    url = cfg.get("url", "")
    if not url:
        raise CalendarNotConfiguredError("config/calendar_ics.json 缺少 url 欄位")
    ics_text = _fetch_ics(url)
    return _parse_ics(ics_text, days=days)


# ── Google Calendar OAuth backend ─────────────────────────────────────────────

def _load_oauth_credentials():
    if not os.path.exists(TOKEN_PATH):
        raise CalendarNotConfiguredError(
            "尚未設定 Google Calendar OAuth。請先到 Google Cloud Console 建立 OAuth "
            "用戶端（啟用 Calendar API，類型選 Desktop app），下載 JSON 存成 "
            "config/google_credentials.json，再執行 "
            "`./.venv/bin/python -m scripts.google_calendar_oauth_setup`"
        )
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return creds


def _oauth_upcoming_events(days: int = 1, max_results: int = 10) -> list[dict]:
    from googleapiclient.discovery import build
    creds = _load_oauth_credentials()
    service = build("calendar", "v3", credentials=creds)
    now = datetime.datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + datetime.timedelta(days=days)).isoformat() + "Z"
    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = []
    for event in result.get("items", []):
        start = event["start"].get("dateTime", event["start"].get("date"))
        events.append({"summary": event.get("summary", "(無標題)"), "start": start})
    return events


# ── Unified API ───────────────────────────────────────────────────────────────

def upcoming_events(days: int = 1) -> list[dict]:
    """Return upcoming events using ICS URL if configured, else Google OAuth.
    Raises CalendarNotConfiguredError if neither is set up."""
    if os.path.exists(ICS_CONFIG_PATH):
        return _ics_upcoming_events(days)
    return _oauth_upcoming_events(days)


def schedule_digest(days: int = 1) -> str:
    """Traditional Chinese digest of upcoming events (used by daily_content_skills)."""
    try:
        events = upcoming_events(days=days)
    except CalendarNotConfiguredError as e:
        return f"⚠️ {e}"
    except Exception as e:
        return f"⚠️ 讀取行事曆失敗: {e}"
    if not events:
        return f"📅 接下來 {days} 天沒有任何行程。"
    lines = [f"📅 接下來 {days} 天的行程："]
    for e in events:
        lines.append(f"- {e['start']}: {e['summary']}")
    return "\n".join(lines)
