"""LINE Messaging API webhook receiver - "LINE 重要訊息播報" wish-list item.

Listens for LINE webhook POSTs, verifies the signature, asks Gemini
whether each incoming text message looks "important" (vs. routine
chit-chat/spam), and if so forwards it to the user's Telegram chat
(`forward_to_telegram_chat_id` in config/line.json) so it shows up
alongside everything else Hermes already surfaces there.

Requires `config/line.json` (see config/line.json.example and
modules/remote/line_handler.py for setup instructions from the LINE
Developers console).

IMPORTANT: LINE delivers webhooks to a *public HTTPS* URL. This script
only runs the local HTTP server (default port 8002) - you still need to
expose it to the internet (e.g. a reverse proxy / tunnel) and register
that public URL as the webhook URL in the LINE Developers console before
LINE will actually call it. Until then this server runs but receives
nothing, same as `/watch earthquake` before `config/cwa.json` exists.

Run via: ./.venv/bin/python -m scripts.line_webhook_server
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from modules.embodied.gemini_client import GeminiClient
from modules.remote.line_handler import LineHandler
from modules.remote.telegram_handler import TelegramHandler

PORT = 8002


def _is_important(text):
    prompt = (
        "以下是一則 LINE 訊息內容，請判斷這則訊息對收訊者來說是否「重要」"
        "（例如：緊急聯絡、要求回覆的工作/家庭事項、帳務/時間敏感通知），"
        "還是「不重要」（例如：日常聊天、貼圖訊息、廣告、群組閒聊）。"
        "只回答一個字：「重要」或「不重要」。\n\n"
        f"訊息內容：{text}"
    )
    try:
        verdict = GeminiClient().chat(prompt).strip()
    except Exception:
        return True  # fail open: if classification fails, forward it anyway
    return "重要" in verdict


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/line/webhook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        signature = self.headers.get("X-Line-Signature", "")

        try:
            line = LineHandler()
        except Exception as e:
            print(f"⚠️ [LineWebhook] {e}")
            self.send_response(500)
            self.end_headers()
            return

        if not line.verify_signature(body, signature):
            print("⚠️ [LineWebhook] 簽章驗證失敗，忽略此請求")
            self.send_response(403)
            self.end_headers()
            return

        # Always 200 OK quickly so LINE doesn't retry, then process.
        self.send_response(200)
        self.end_headers()

        for event in line.parse_events(body):
            text = event["text"]
            print(f"📩 [LineWebhook] 收到訊息: {text}")
            if _is_important(text):
                if line.forward_to_telegram_chat_id:
                    TelegramHandler().send_message(
                        line.forward_to_telegram_chat_id,
                        f"📱 LINE 重要訊息：\n{text}",
                    )
            else:
                print("ℹ️ [LineWebhook] 判定為不重要，不轉發")

    def log_message(self, fmt, *args):
        pass  # quiet; we print our own status lines above


def main():
    print(f"📱 [LineWebhook] 啟動中，監聽 0.0.0.0:{PORT}/line/webhook ...")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
