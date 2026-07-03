"""Telegram Bot API client (long polling, stdlib only - no extra deps).

Config: config/telegram.json (gitignored, never commit)
{
    "bot_token": "...",
    "allowed_user_ids": [123456789],
    "poll_timeout": 30
}

`allowed_user_ids` is the authorization whitelist. Until a Telegram
user's numeric id is listed here, the bot will not run any commands
for them - it only replies with their id so they can add themselves
and restart the bot (see scripts/telegram_bot.py).
"""

import json
import os
import urllib.parse
import urllib.request

from .base_handler import BaseRemoteHandler

BASE_DIR = "/Users/USERNAME/Hermes_Brain"
CONFIG_PATH = os.path.join(BASE_DIR, "config", "telegram.json")


class TelegramHandler(BaseRemoteHandler):
    def __init__(self):
        self._reload_config()
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    def _reload_config(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.bot_token = cfg["bot_token"]
        self.allowed_user_ids = set(cfg.get("allowed_user_ids", []))
        self.poll_timeout = cfg.get("poll_timeout", 30)

    def is_authorized(self, user_id) -> bool:
        # Re-read config so adding an id to the whitelist takes effect
        # without restarting the bot.
        self._reload_config()
        return user_id in self.allowed_user_ids

    def get_updates(self, offset=None):
        params = {"timeout": self.poll_timeout}
        if offset is not None:
            params["offset"] = offset
        url = f"{self.api_base}/getUpdates?{urllib.parse.urlencode(params)}"
        # 讀取逾時只比長輪詢多 5 秒：連線卡死時更快放棄重連（不會讓你的訊息枯等 40 秒）
        with urllib.request.urlopen(url, timeout=self.poll_timeout + 5) as resp:
            data = json.load(resp)
        if not data.get("ok"):
            return []
        return data.get("result", [])

    def send_message(self, chat_id, text, parse_mode=None):
        url = f"{self.api_base}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:  # 例如 "Markdown"；不帶就維持純文字（向後相容）
            payload["parse_mode"] = parse_mode
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                r = json.load(resp)
            return bool(r.get("ok", True))
        except Exception as e:
            print(f"⚠️ [TelegramHandler] send_message failed: {e}")
            return False

    def send_chat_action(self, chat_id, action="typing"):
        """顯示「輸入中…」狀態，讓使用者立刻知道有在處理（瞬間、無延遲感）。"""
        try:
            body = json.dumps({"chat_id": chat_id, "action": action}).encode("utf-8")
            req = urllib.request.Request(f"{self.api_base}/sendChatAction", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).read()
            return True
        except Exception:
            return False

    def send_message_get_id(self, chat_id, text):
        """送訊息並回傳 message_id（給串流逐步編輯用）。失敗回 None。"""
        try:
            body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
            req = urllib.request.Request(f"{self.api_base}/sendMessage", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                r = json.load(resp)
            return (r.get("result") or {}).get("message_id")
        except Exception as e:
            print(f"⚠️ [TelegramHandler] send_message_get_id failed: {e}")
            return None

    def edit_message(self, chat_id, message_id, text):
        """編輯既有訊息（串流：逐步把累積的文字填進同一則訊息）。"""
        try:
            body = json.dumps({"chat_id": chat_id, "message_id": message_id,
                               "text": text}).encode("utf-8")
            req = urllib.request.Request(f"{self.api_base}/editMessageText", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception:
            return False  # 內容沒變或被限流會報錯，吞掉即可（下一次編輯會補上）

    def download_file(self, file_id, dest_path):
        """Download a Telegram-hosted file (voice/audio/document) to `dest_path`."""
        url = f"{self.api_base}/getFile?{urllib.parse.urlencode({'file_id': file_id})}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        if not data.get("ok"):
            raise RuntimeError(f"getFile failed: {data}")
        file_path = data["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        with urllib.request.urlopen(file_url, timeout=120) as resp, open(dest_path, "wb") as f:
            f.write(resp.read())
        return dest_path

    def send_document(self, chat_id, file_path, caption=None):
        """Send a local file as a document (e.g. a generated report)."""
        boundary = "----HermesBoundary"
        parts = [f"--{boundary}\r\n".encode()]
        parts.append(
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'.encode()
        )
        if caption:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(
                f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode()
            )
        filename = os.path.basename(file_path)
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        with open(file_path, "rb") as f:
            parts.append(f.read())
        parts.append(f"\r\n--{boundary}--\r\n".encode())

        body = b"".join(parts)
        url = f"{self.api_base}/sendDocument"
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                json.load(resp)
        except Exception as e:
            print(f"⚠️ [TelegramHandler] send_document failed: {e}")
