# expense-fastpath

A tiny hermes-agent plugin that records pure **"item + amount"** messages
(like `午餐138` / `coffee 90`) in **under a second** — skipping the full agent
loop — and replies with a confirmation. Everything else falls through to normal
dispatch untouched.

## Why a plugin (and not a patch)

An earlier version of this lived as a `handle_message` override edited directly
into hermes-agent's Telegram adapter. That was the wrong altitude and shipped
three real bugs. This plugin uses the official **`pre_gateway_dispatch`** hook
instead, which:

- **survives `hermes update`** — no edits to tracked upstream source, and
- lets us reuse the gateway's own primitives to close the bugs:

| Bug in the old override | How the plugin fixes it |
|---|---|
| **Auth bypass** — recorded *before* the pipeline's auth check, so any stranger who DM'd the bot could inject expenses. | Gate on the gateway's own `_is_user_authorized(source)` first; non-owners fall through to the normal auth/pairing flow and are never recorded. |
| **Photo drop** — matched a photo's caption and silently dropped the image. | Bail out whenever `event.media_urls` is populated, so captioned media always reaches the vision pipeline. |
| **Clarify interception** — ate free-form answers to a pending clarify prompt. | Skip the fast-path when a clarify entry is pending for the session. |
| **Partial-success double-record** — a failed reply let the agent re-process and record a second time. | Decide skip-vs-fallthrough purely on the record outcome; the reply is fire-and-forget and can never trigger a re-record. |

## How it works

`pre_gateway_dispatch` fires once per inbound message, **before** auth and agent
dispatch. The callback:

1. Fails safe to `None` (normal dispatch) on every gate: internal event,
   empty/too-long/multiline text, any attached media, a question mark, a
   non-matching regex, an unauthorized sender, or a pending clarify.
2. Records synchronously via the local bookkeeping endpoint
   (`127.0.0.1:8809/expense_auto`) so it knows the real outcome.
3. If recorded → schedules an async confirmation via the platform adapter's own
   `send()` and returns `{"action": "skip"}` to short-circuit the pipeline.
4. If **not** recorded → returns `None` so the agent handles it (nothing lost).

## Install

Copy this directory to `~/.hermes/plugins/expense-fastpath/` and enable it in
`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - expense-fastpath
```

Then restart the gateway. It works on any gateway platform (Telegram, etc.) —
the owner-only gate and the confirmation both go through the active adapter.

## Requirements

- A running bookkeeping endpoint exposing `POST /expense_auto` that accepts
  `{"message": "<item> <amount>"}` and returns `{"recorded": true|false}`
  (this repo's `brain/scripts/hermes_memory_endpoint.py` provides one).
