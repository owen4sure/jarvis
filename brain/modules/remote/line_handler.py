"""LINE Messaging API client (webhook signature verification + push
message), stdlib only - same pattern as `telegram_handler.py`.

Config: config/line.json (gitignored, copy from config/line.json.example):
    {
        "channel_access_token": "...",
        "channel_secret": "...",
        "forward_to_telegram_chat_id": 123456789
    }

Get these from the LINE Developers console (Messaging API channel):
https://developers.line.biz/console/ -> create a Messaging API channel ->
"Messaging API" tab has the channel access token and channel secret.

This module only implements the client side (signature verification +
sending). The actual webhook HTTP endpoint is
`scripts/line_webhook_server.py` - LINE requires a public HTTPS URL to
deliver webhooks to, so that script needs to be reachable from the
internet (e.g. via a reverse proxy/tunnel) before LINE will call it.
"""

import base64
import hashlib
import hmac
import json
import os
import urllib.request

from .base_handler import BaseRemoteHandler

BASE_DIR = "/Users/USERNAME/Hermes_Brain"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "line.json")


class LineNotConfiguredError(Exception):
    pass


class LineHandler(BaseRemoteHandler):
    def __init__(self):
        if not os.path.exists(CONFIG_PATH):
            raise LineNotConfiguredError(
                "尚未設定 LINE 帳號。請複製 config/line.json.example 為 "
                "config/line.json 並填入 LINE Developers console "
                "(Messaging API channel) 的 channel_access_token 與 "
                "channel_secret，以及要轉發重要訊息的 Telegram chat_id"
            )
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.channel_access_token = cfg["channel_access_token"]
        self.channel_secret = cfg["channel_secret"]
        self.forward_to_telegram_chat_id = cfg.get("forward_to_telegram_chat_id")

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """`signature` is the `X-Line-Signature` header value (base64 HMAC-SHA256
        of the raw request body using the channel secret)."""
        digest = hmac.new(self.channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    def parse_events(self, body: bytes):
        """Returns a list of {"user_id", "text", "reply_token"} for each
        text message event in a LINE webhook payload."""
        data = json.loads(body.decode("utf-8"))
        events = []
        for event in data.get("events", []):
            if event.get("type") != "message":
                continue
            message = event.get("message", {})
            if message.get("type") != "text":
                continue
            events.append({
                "user_id": event.get("source", {}).get("userId"),
                "text": message.get("text", ""),
                "reply_token": event.get("replyToken"),
            })
        return events

    def send_message(self, chat_id, text):
        """Push a message to a LINE user (`chat_id` = LINE userId)."""
        url = "https://api.line.me/v2/bot/message/push"
        body = json.dumps({
            "to": chat_id,
            "messages": [{"type": "text", "text": text}],
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.channel_access_token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception as e:
            print(f"⚠️ [LineHandler] send_message failed: {e}")

    def reply_message(self, reply_token, text):
        """Reply to a specific event using its `replyToken` (free, doesn't
        count against the monthly push message quota)."""
        url = "https://api.line.me/v2/bot/message/reply"
        body = json.dumps({
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.channel_access_token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except Exception as e:
            print(f"⚠️ [LineHandler] reply_message failed: {e}")
