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

# One regex does gating AND item/amount extraction (no second re-parse):
#   group 1 = item (non-greedy so trailing digits land in group 2)
#   group 2 = amount (2-6 digits)
#   optional trailing 元/塊
_PATTERN = re.compile(r"^([一-龥A-Za-z][一-龥A-Za-z0-9 ]{0,9}?)\s*(\d{2,6})\s*(?:元|塊)?$")
_QUESTION = re.compile(r"[?？嗎]")


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
        resp = json.loads(urllib.request.urlopen(req, timeout=3).read())
        return bool(resp.get("recorded"))
    except Exception as exc:
        logger.warning("expense-fastpath record failed (%s) — falling through", exc)
        return False


async def _send_confirmation(gateway, source, item: str, amount: str) -> None:
    """Fire-and-forget confirmation via the platform adapter's own send()
    (gets markdown/chunking/thread handling; not a raw bot call)."""
    try:
        adapter = getattr(gateway, "adapters", {}).get(source.platform)
        if adapter is not None and source.chat_id:
            await adapter.send(chat_id=str(source.chat_id),
                               content=f"好，{item} {amount} 元記下了。")
    except Exception as exc:
        logger.warning("expense-fastpath confirmation send failed: %s", exc)


def _on_pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_kwargs):
    """Return {"action": "skip"} when we've handled an expense; else None
    (normal dispatch). Every gate below fails safe to None."""
    if event is None or gateway is None or getattr(event, "internal", False):
        return None

    text = (getattr(event, "text", "") or "").strip()
    # Text-only: captioned photos/documents carry media_urls — never intercept
    # them (that would drop the image).
    if not text or len(text) > 16 or "\n" in text or getattr(event, "media_urls", None):
        return None
    if _QUESTION.search(text):
        return None

    match = _PATTERN.match(text)
    if not match:
        return None
    item, amount = match.group(1).strip(), match.group(2)

    source = getattr(event, "source", None)
    if source is None:
        return None

    # SECURITY GATE — only the authorized owner may fast-path a record.
    # Non-owners fall through to the normal auth/pairing flow (and are NOT
    # recorded). If we can't verify, we don't fast-path.
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

    # Record synchronously so skip-vs-fallthrough reflects the real outcome.
    if not _record(f"{item} {amount}"):
        return None  # not recorded → let the agent handle it, nothing lost

    # Recorded → confirm asynchronously (reply failure can't cause a re-record).
    try:
        asyncio.get_running_loop().create_task(
            _send_confirmation(gateway, source, item, amount))
    except RuntimeError:
        pass  # no running loop → skip reply, still short-circuit the pipeline

    return {"action": "skip", "reason": "expense-fastpath"}


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
