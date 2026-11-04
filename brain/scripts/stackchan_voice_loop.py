"""
StackChan Voice Loop — 家裡免持語音迴圈（給 xiaozhi / stackchan-mcp 裝置）
============================================================================
裝置端「喚醒詞 / 按鍵 / 觸控」觸發的錄音，會被 stackchan-mcp gateway 打包成
Ogg/Opus 然後 POST 到這個端點 (STACKCHAN_AUDIO_HOOK_URL)。流程：

  裝置麥克風 → (gateway audio hook) → 本端點
     → 轉文字 (gemini transcribe)
     → 大腦回覆 (voice_brain.handle_utterance，與 Telegram 共用記憶/人格)
     → 透過 gateway say() 在裝置上說出來 + set_avatar 表情

這樣「在家跟 StackChan 講話」用的是和 Telegram 同一顆大腦、同一份記憶。

啟動：./.venv/bin/python -m scripts.stackchan_voice_loop
Port 由 config/stackchan.json 的 voice_loop_port 決定（預設 8801）。
"""
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from modules.embodied.gemini_client import GeminiClient
from modules.embodied.voice_brain import handle_utterance
from modules.embodied.stackchan_mcp_client import StackChanClient
from modules.memory.memory_manager import MemoryManager

_CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config", "stackchan.json")
_cfg = json.load(open(_CFG, encoding="utf-8")) if os.path.exists(_CFG) else {}

app = FastAPI(title="Hermes StackChan Voice Loop")
gemini = GeminiClient()
memory = MemoryManager()
robot = StackChanClient()

EXPECTED_TOKEN = _cfg.get("token", "")


@app.get("/health")
def health():
    status = robot.get_status()
    return {"status": "ok", "device": status.get("result", status)}


# 進行中的「一起玩遊戲」session（語音迴圈是單一常駐程序，用 module 全域即可）
_game_session = None


def _send_meeting_to_telegram(result):
    """會議報告完成 → 推到 Telegram（摘要 + 報告檔 + 待辦）。"""
    try:
        from modules.remote.telegram_handler import TelegramHandler
        import json as _j
        cfg = _j.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "config", "telegram.json")))
        h = TelegramHandler()
        summary = result.get("summary", "")
        tasks = result.get("tasks", [])
        msg = f"📋 會議總結\n\n{summary}"
        if tasks:
            msg += "\n\n✅ 待辦：\n" + "\n".join(
                f"• {t.get('owner','?')}：{t.get('task','')}（{t.get('deadline','TBD')}）" for t in tasks)
        for uid in cfg.get("allowed_user_ids", []):
            try:
                h.send_message(uid, msg)
                if result.get("path"):
                    h.send_document(uid, result["path"], caption="完整會議報告")
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ [Meeting] 推 Telegram 失敗: {e}")


def _play_move(line_seed=None, transcript=None):
    """遊戲中的一手：讓夥伴回應，必要時開相機看實體狀態。"""
    line = _game_session.respond(transcript)
    if _game_session.needs_to_see(line):
        try:
            shot = robot.call_tool("take_photo", {"question": "牌桌/手勢/骰子上現在是什麼？"})
            blob = str(shot.get("result", shot))
            follow = _game_session.respond(f"（我看到了：{blob[:200]}）")
            return f"{line} {follow}"
        except Exception:
            pass
    return line


def _route_utterance(transcript: str) -> str:
    """純語意驅動：不靠任何固定關鍵字，一切交給語意分類器判斷『你想做什麼』。
    一次分類涵蓋遊戲意圖 + 所有功能 + 一般對話，分類結果往下複用、不重複呼叫。
    指令/觸發詞只是可選捷徑，永遠不會限制理解。"""
    global _game_session
    from modules.remote import intent_router
    from modules.games.companion import GameCompanion, parse_game_name

    intent = intent_router.classify(transcript, gemini)
    cmd = intent.get("command", "chat")

    # ── 會議記錄模式（放著全程聆聽 → 散會後總結）──
    from modules.embodied import meeting as _mtg
    rec = _mtg.active_recorder()
    if rec:
        if cmd == "meeting_stop":
            result = rec.stop()
            return "好，我來幫你總結這場會議。" + (result.get("summary", "") if result else "")
        return ""  # 記錄中：安靜聆聽，不插話（迴圈自己會在聽到「散會」時收工）
    if cmd == "meeting_start":
        label = (intent.get("args") or "").strip()
        # args 若像一整句指令（含「幫我記/錄/聽/總結/這場/一下」）就用預設名
        if not label or len(label) > 10 or any(w in label for w in
                ("幫我", "記", "錄", "聽", "總結", "這場", "一下", "等等", "會議記錄")):
            label = "現場會議"
        new = _mtg.MeetingRecorder(robot=robot, gemini=gemini, on_done=_send_meeting_to_telegram)
        return new.start(label=label)

    in_game = bool(_game_session and _game_session.active)

    if in_game:
        if cmd == "game_stop":
            _game_session = None
            return "好喔，那這場先到這～ 玩得很開心！想再玩隨時找我。"
        # 想在玩的途中改教/改玩另一個遊戲
        if cmd in ("game_join", "game_teach"):
            name = (intent.get("args") or "").strip() or parse_game_name(transcript)
            _game_session = GameCompanion(gemini, game=name, learning=(cmd == "game_teach"))
            return _game_session.opening()
        # 其餘一律當成這局的一手
        return _play_move(transcript=transcript)

    if cmd in ("game_join", "game_teach"):
        name = (intent.get("args") or "").strip() or parse_game_name(transcript)
        _game_session = GameCompanion(gemini, game=name, learning=(cmd == "game_teach"))
        return _game_session.opening()

    # 一般對話 / 功能：把已分類好的 intent 傳下去，不重複分類
    return handle_utterance(transcript, gemini=gemini, memory=memory, intent=intent)


@app.post("/hook")
async def hook(request: Request):
    # 驗證 bearer token（gateway 用 STACKCHAN_AUDIO_HOOK_TOKEN = 同一把 token）
    if EXPECTED_TOKEN:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {EXPECTED_TOKEN}":
            return JSONResponse(status_code=401, content={"error": "unauthorized"})

    # 記下「剛剛在互動」，讓待機生命感 (liveliness) 讓位、不打斷對話
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "memory", "voice_last.txt"), "w") as f:
            import time as _t
            f.write(str(_t.time()))
    except Exception:
        pass

    audio_bytes = await request.body()
    if not audio_bytes:
        return JSONResponse(status_code=400, content={"error": "empty body"})

    mime = request.headers.get("content-type", "audio/ogg") or "audio/ogg"
    # 多段式 (multipart) 時內容型別會是 multipart/*，gemini 直接吃原始 bytes 即可
    if mime.startswith("multipart"):
        mime = "audio/ogg"

    robot.set_avatar("thinking")
    try:
        transcript = gemini.transcribe(audio_bytes, mime_type=mime)
        if not transcript or not transcript.strip():
            robot.set_avatar("neutral")
            return {"ok": True, "note": "empty transcript"}

        reply_text = _route_utterance(transcript)
    except Exception as e:
        robot.set_avatar("sad")
        return JSONResponse(status_code=502, content={"error": str(e)})

    # 會議記錄模式中、或刻意不回話時：安靜，不唸出來
    if not reply_text or not reply_text.strip():
        return {"ok": True, "transcript": transcript, "reply": "", "spoken": False}

    # 在裝置上把回覆唸出來——臉 + LED 配合情緒（有表情的夥伴）
    from modules.embodied.expression import expressive_say
    say_result = expressive_say(robot, reply_text)
    return {"ok": True, "transcript": transcript, "reply": reply_text,
            "spoken": say_result.get("ok", False)}


if __name__ == "__main__":
    import uvicorn
    port = int(_cfg.get("voice_loop_port", 8801))
    print(f"🎙️  [StackChan Voice Loop] 0.0.0.0:{port}  (hook=/hook)")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
