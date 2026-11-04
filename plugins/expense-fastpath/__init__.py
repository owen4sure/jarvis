"""expense-fastpath plugin — instant bookkeeping for "item+amount" messages.

Intercepts pure "品項+金額" messages (like「午餐138」/「coffee 90」) in the
gateway's ``pre_gateway_dispatch`` hook, records them via the local
bookkeeping endpoint (8809/expense_auto), and replies in <1s — skipping the
~15s agent loop. Everything else falls through to normal dispatch untouched.

Why a plugin (not a patch in gateway/platforms/telegram.py):
  * ``pre_gateway_dispatch`` is the official interception point and survives
    ``hermes update`` — no local edits to tracked upstream source.
  * The earlier telegram.py override had three real bugs this design fixes:
      1. Auth bypass — it recorded BEFORE the pipeline's auth check, so any
         stranger could inject expenses. Here we gate on the gateway's own
         ``_is_user_authorized`` first; non-owners fall through to the normal
         auth/pairing flow and are never recorded.
      2. Photo drop — it matched a photo's caption and dropped the image.
         Here we bail out whenever ``event.media_urls`` is populated, so
         captioned media always reaches the vision pipeline.
      3. Clarify interception — it ate free-form answers to a pending
         clarify prompt. Here we skip the fast-path when a clarify entry is
         pending for the session.
  * Recording is decided BEFORE any reply, so a failed reply can never cause
    the agent loop to re-process and double-record (the old partial-success
    bug): if it recorded, we always skip; if it didn't, we always fall through.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request

logger = logging.getLogger(__name__)

_ENDPOINT = "http://127.0.0.1:8809/expense_auto"
_REMINDER_ENDPOINT = "http://127.0.0.1:8809/reminder"

# One regex does gating AND item/amount extraction (no second re-parse):
#   group 1 = item (non-greedy so trailing digits land in group 2)
#   group 2 = amount (2-6 digits)
#   optional trailing 元/塊
_PATTERN = re.compile(r"^([一-龥A-Za-z][一-龥A-Za-z0-9 ]{0,9}?)\s*(\d{2,6})\s*(?:元|塊)?$")

# Reminder/event fast-path: record deterministically when a message clearly
# schedules a FUTURE event, because flash-lite often replies "記下來了" without
# ever calling the tool. Trigger requires a concrete future-DAY word (not bare
# 今天/N點, which are ambiguous and over-match) OR an explicit 提醒. Questions and
# spending lines are excluded so statements like「明天幾點開會」(a question) and
#「明天大約要買東西」don't create bogus reminders. /reminder is the final gate.
_DAY_MARK = re.compile(
    r"明天|後天|大後天|下週[一二三四五六日天]?|下星期[一二三四五六日天]?|"
    r"下周[一二三四五六日天]?|本週[一二三四五六日天]|這週[一二三四五六日天]|"
    r"本周[一二三四五六日天]|星期[一二三四五六日天]|禮拜[一二三四五六日天]|"
    r"週[一二三四五六日天]|\d{1,2}\s*月\s*\d{1,2}")
_EVENT_INTENT = re.compile(
    r"比賽|球賽|會議|開會|約會|預約|有約|約了|約人|面試|聚餐|報告|繳費|生日|演唱會|"
    r"回診|看醫生|吃飯|見面|活動|典禮|婚禮|出差|考試|截止|報名|訂位|上課|演出|表演|"
    r"聚會|派對|健檢|體檢|婚宴|開幕|回台|返鄉")
_QUESTION = re.compile(r"[?？]|嗎|呢|幾點|幾號|幾月|什麼時候|何時|哪天|多久|是不是|如何")
_EXPENSE_HINT = re.compile(r"花了|花費|塊錢|\d+\s*元|\d+\s*塊")


def _looks_like_reminder(t: str) -> bool:
    """Conservative gate: schedules a future event? (day-word or 提醒) + event
    intent, not a question, not a spending line, not too long."""
    if not t or len(t) > 40 or _EXPENSE_HINT.search(t) or _QUESTION.search(t):
        return False
    if any(w in t for w in ("取消", "刪", "不用")):
        return False
    # 「提醒我/提醒你」才算明確排程指令(排除「開會要提醒大家」這種非排程句);
    # 或有具體未來日期詞。兩者之一 + 事件意圖(或本來就是「提醒我…」)才攔。
    remind_me = ("提醒我" in t) or ("提醒你" in t)
    has_trigger = remind_me or bool(_DAY_MARK.search(t))
    return has_trigger and bool(_EVENT_INTENT.search(t) or remind_me)


def _record(clean_message: str) -> bool:
    """POST to the bookkeeping endpoint. Returns True only on a recorded entry.

    Synchronous + short-timeout: it's a localhost call and we must know the
    outcome before deciding skip-vs-fallthrough. Any failure returns False so
    the message falls through to the normal agent loop (never silently lost).
    """
    try:
        req = urllib.request.Request(
            _ENDPOINT,
            data=json.dumps({"message": clean_message}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=2).read())
        return bool(resp.get("recorded"))
    except Exception as exc:
        logger.warning("expense-fastpath record failed (%s) — falling through", exc)
        return False


def _record_reminder(text: str):
    """POST to /reminder; the endpoint's own parser is the gate. Returns the
    human-readable confirmation string only when it parsed a real future time."""
    try:
        req = urllib.request.Request(
            _REMINDER_ENDPOINT,
            data=json.dumps({"time": text, "message": "", "channel": "both"}).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=2).read())
        if resp.get("ok") and (resp.get("time") or resp.get("repeat")):
            return resp.get("nice") or resp.get("text") or "好，記下了"
    except Exception as exc:
        logger.warning("reminder fast-path failed (%s) — falling through", exc)
    return None


async def _send_text(gateway, source, content: str) -> None:
    """Fire-and-forget reply via the platform adapter's own send()."""
    try:
        adapter = getattr(gateway, "adapters", {}).get(source.platform)
        if adapter is not None and source.chat_id:
            await adapter.send(chat_id=str(source.chat_id), content=content)
    except Exception as exc:
        logger.warning("fast-path reply send failed: %s", exc)


async def _send_confirmation(gateway, source, item: str, amount: str) -> None:
    await _send_text(gateway, source, f"好，{item} {amount} 元記下了。")


def _on_pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_kwargs):
    """Return {"action": "skip"} when we've handled an expense; else None
    (normal dispatch). Every gate below fails safe to None."""
    if event is None or gateway is None or getattr(event, "internal", False):
        return None

    text = (getattr(event, "text", "") or "").strip()
    # Text-only: captioned photos/documents carry media_urls — never intercept
    # them (that would drop the image). Questions never fast-path.
    if not text or "\n" in text or getattr(event, "media_urls", None):
        return None
    if _QUESTION.search(text):
        return None

    source = getattr(event, "source", None)
    if source is None:
        return None

    # SECURITY GATE — only the authorized owner may fast-path. Non-owners fall
    # through to the normal auth/pairing flow. If we can't verify, don't fast-path.
    authorized = getattr(gateway, "_is_user_authorized", None)
    if not callable(authorized):
        return None
    try:
        if not authorized(source):
            return None
    except Exception:
        return None

    # Don't eat a free-form answer the agent is waiting on (clarify/approval).
    try:
        from tools import clarify_gateway
        session_key = gateway._session_key_for_source(source)
        if clarify_gateway.get_pending_for_session(session_key) is not None:
            return None
    except Exception:
        pass  # clarify unavailable → proceed; pending clarify is a rare edge

    def _skip_after(coro):
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            pass  # no running loop → skip reply, still short-circuit the pipeline

    # ── Path 1: expense (short "item + amount") ──────────────────────────────
    m = _PATTERN.match(text) if len(text) <= 16 else None
    if m:
        item, amount = m.group(1).strip(), m.group(2)
        if _record(f"{item} {amount}"):   # sync → skip/fallthrough reflects reality
            _skip_after(_send_confirmation(gateway, source, item, amount))
            return {"action": "skip", "reason": "expense-fastpath"}
        return None

    # ── Path 2: reminder / event (conservative gate; /reminder is the final gate) ──
    if _looks_like_reminder(text):
        nice = _record_reminder(text)
        if nice:
            _skip_after(_send_text(gateway, source, nice))
            return {"action": "skip", "reason": "reminder-fastpath"}

    return None  # nothing matched → normal agent dispatch


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
