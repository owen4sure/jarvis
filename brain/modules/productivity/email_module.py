"""Email inbox digest, from the Stack-chan "資訊過濾" wish list.

Connects to a mailbox over IMAP (read-only), pulls the most recent
unread messages, and asks Gemini to produce a short Traditional
Chinese digest - "Gmail/LINE/Telegram 重要訊息播報" for email.

Setup:
    Create `config/email.json` (gitignored, same pattern as
    `config/telegram.json`):
        {
            "imap_host": "imap.gmail.com",
            "imap_port": 993,
            "email_address": "you@gmail.com",
            "app_password": "xxxx xxxx xxxx xxxx"
        }
    For Gmail, `app_password` must be a 16-character App Password
    (requires 2FA enabled on the Google account), not your normal
    password.

If `config/email.json` doesn't exist yet, `EmailModule` methods raise
`EmailNotConfiguredError` with a message explaining how to set it up -
callers (e.g. the Telegram `/email` command) should catch this and
show that message to the user instead of crashing.
"""

import email
import imaplib
import json
import os
from email.header import decode_header

from modules.embodied.gemini_client import GeminiClient

CONFIG_PATH = "/Users/chenyouwei/Hermes_Brain/config/email.json"


class EmailNotConfiguredError(Exception):
    pass


def _decode_header_value(value):
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


class EmailModule:
    def __init__(self):
        self.gemini = GeminiClient()

    def _load_config(self):
        if not os.path.exists(CONFIG_PATH):
            raise EmailNotConfiguredError(
                "尚未設定 email 帳號。請複製 config/email.json.example 為 "
                "config/email.json 並填入你的帳號資訊:\n"
                '{\n'
                '  "imap_host": "imap.gmail.com",\n'
                '  "imap_port": 993,\n'
                '  "email_address": "you@gmail.com",\n'
                '  "app_password": "xxxx xxxx xxxx xxxx"\n'
                "}\n"
                "（Gmail 需先開啟兩步驟驗證，並產生「應用程式專用密碼」）"
            )
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def fetch_unread(self, limit=10):
        """Connect over IMAP and return the `limit` most recent unread
        messages as a list of {"from", "subject", "snippet"} dicts."""
        cfg = self._load_config()

        conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg.get("imap_port", 993))
        try:
            conn.login(cfg["email_address"], cfg["app_password"])
            conn.select("INBOX", readonly=True)

            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return []

            message_ids = data[0].split()
            message_ids = message_ids[-limit:]

            messages = []
            for msg_id in reversed(message_ids):
                status, msg_data = conn.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                msg = email.message_from_bytes(msg_data[0][1])

                subject = _decode_header_value(msg.get("Subject"))
                sender = _decode_header_value(msg.get("From"))
                snippet = self._extract_snippet(msg)

                messages.append({"from": sender, "subject": subject, "snippet": snippet})

            return messages
        finally:
            try:
                conn.close()
            except Exception:
                pass
            conn.logout()

    def _extract_snippet(self, msg, max_chars=300):
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = part.get_payload(decode=True).decode(charset, errors="replace")
                    except Exception:
                        body = ""
                    break
        else:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = msg.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                body = str(msg.get_payload())

        return " ".join(body.split())[:max_chars]

    def summarize_inbox(self, limit=10):
        """Fetch unread mail and return a short Traditional Chinese digest."""
        messages = self.fetch_unread(limit=limit)
        if not messages:
            return "📭 收件箱沒有未讀郵件。"

        lines = []
        for m in messages:
            lines.append(f"From: {m['from']}\nSubject: {m['subject']}\n{m['snippet']}")
        mail_text = "\n\n---\n\n".join(lines)

        prompt = (
            "以下是使用者信箱裡的未讀郵件原文，請用繁體中文條列出重點摘要，"
            "每封信一行，標出寄件人與最重要的一句話內容。如果是廣告/通知信，"
            "可以直接標註「廣告/通知」並簡化成一行。"
            "如果有任何來自物流/電商的「包裹配送」相關通知（例如已出貨、"
            "運送中、可取貨、已送達等狀態，寄件人如 7-11、全家、"
            "黑貓宅急便、蝦皮、momo、PChome 等），請額外用「📦 包裹動態」"
            "段落特別列出每筆包裹目前的狀態，方便使用者掌握配送進度。"
            "最後加一行總結，提醒有沒有需要優先處理的信件。\n\n"
            f"{mail_text}"
        )
        return self.gemini.chat(prompt)
