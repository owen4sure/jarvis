"""HTTP side-channel for StackChan: voice in, voice out.

MQTT carries small control/sensor messages; audio is too big/slow for
that, so StackChan POSTs recordings here and gets back a WAV reply.

Endpoints:
    GET  /health           -> {"status": "ok"}
    POST /voice             (multipart "file") -> WAV audio reply
    GET  /audio/{filename}  -> serve a previously generated WAV
"""

import json
import os
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config
from .gemini_client import GeminiClient
from .command_mapper import send_command
from .skill_context import SkillContext
from . import tts
from ..memory import hermes_agent_bridge
from ..memory.memory_manager import MemoryManager

app = FastAPI(title="Hermes Embodied Audio Bridge")
app.state.bridge = None  # set by scripts/embodied_daemon.py at startup
gemini = GeminiClient()
memory = MemoryManager()

# 語音不適合觸發的指令：會改程式碼、需要上傳檔案、或純文字選單類，
# 用講的容易被誤辨識或回傳一大段不適合唸出來的文字。
_VOICE_BLOCKED_COMMANDS = {
    "dev", "plaud", "help", "start", "model", "models", "status", "sync",
}


class _VoiceContext:
    """Minimal ctx so COMMAND_TABLE handlers work from the voice loop,
    sharing the same memory/gemini as Telegram. No chat_id/handler — voice
    commands must return their reply synchronously (async ones are blocked)."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.skill_ctx = SkillContext(bridge, sensory=None)
        self.gemini = gemini
        self.memory = memory
        self.chat_id = None
        self.handler = None

    @property
    def research(self):
        from ..productivity.research_module import ResearchModule
        if not hasattr(self, "_research"):
            self._research = ResearchModule()
        return self._research


def _try_voice_command(transcript, bridge):
    """Route a voice transcript to a COMMAND_TABLE handler if it maps to one.
    Returns the command's text reply, or None to fall through to chat."""
    from modules.remote import intent_router
    from modules.remote.commands import COMMAND_TABLE

    intent = intent_router.classify(transcript, gemini)
    cmd_name = intent.get("command", "chat")
    if cmd_name in ("chat", "") or cmd_name in _VOICE_BLOCKED_COMMANDS:
        return None
    cmd_fn = COMMAND_TABLE.get(cmd_name)
    if not cmd_fn:
        return None
    try:
        ctx = _VoiceContext(bridge)
        return cmd_fn(intent.get("args", ""), ctx)
    except Exception as e:
        print(f"⚠️ [AudioBridge] 語音指令 {cmd_name} 執行失敗: {e}")
        return None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/voice")
async def voice(request: Request, file: UploadFile = File(...)):
    bridge = request.app.state.bridge

    os.makedirs(config.AUDIO_INBOX_DIR, exist_ok=True)
    audio_bytes = await file.read()

    inbox_path = os.path.join(
        config.AUDIO_INBOX_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    )
    with open(inbox_path, "wb") as f:
        f.write(audio_bytes)

    if bridge:
        send_command(bridge, "THINKING")

    try:
        # 1. 語音轉文字
        transcript = gemini.transcribe(audio_bytes, mime_type=file.content_type or "audio/wav")

        # 1-1. 意圖路由：先看這句話是不是在叫 Hermes 做某個功能（天氣/記帳/查詢…），
        # 與 Telegram 共用同一套路由與指令。是的話直接執行並把結果唸出來，
        # 不是的話（chat）才走下面的對話流程。讓 StackChan 用講的也能用所有功能。
        command_reply = _try_voice_command(transcript, bridge)
        if command_reply:
            reply_text = command_reply
            try:
                memory.remember(
                    f"User(語音): {transcript}\nHermes: {reply_text}",
                    category="stackchan_voice",
                    importance=2,
                )
            except Exception as e:
                print(f"⚠️ [AudioBridge] 記憶寫入失敗: {e}")
            _log_conversation(inbox_path, reply_text, transcript=transcript)
            wav_path = tts.synthesize(reply_text)
            if bridge:
                send_command(bridge, "STATUS_HAPPY")
            return FileResponse(wav_path, media_type="audio/wav", filename=os.path.basename(wav_path))

        # 2. 注入長期記憶 (Memory Recall) - 與 Telegram 共用同一份記憶
        context_messages = []

        # 2-1. 真實時間注入：Gemini 沒有時鐘，問「今天星期幾」會亂猜。與 Telegram 端一致。
        _WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]
        _now = datetime.now()
        _now_str = f"{_now.year}年{_now.month}月{_now.day}日 星期{_WEEKDAY_ZH[_now.weekday()]} {_now.strftime('%H:%M')}"
        context_messages.append(f"【現在的真實日期時間（請以此為準，不要自行推測）】:\n{_now_str}")

        # 2a. 與 hermes-agent 共用記憶 (USER.md / MEMORY.md) - 三端共用
        try:
            user_profile = hermes_agent_bridge.read_user_profile()
            if user_profile:
                context_messages.append(f"【使用者個人檔案 (hermes-agent)】:\n{user_profile}")
            project_memory = hermes_agent_bridge.read_project_memory()
            if project_memory:
                context_messages.append(f"【專案長期記憶 (hermes-agent)】:\n{project_memory}")
        except Exception as e:
            print(f"⚠️ [AudioBridge] 讀取 hermes-agent 記憶失敗: {e}")

        try:
            memories = memory.recall(transcript, top_k=3, exclude_categories=("telegram_chat", "stackchan_voice"))
            if memories:
                mem_str = "\n".join(f"- {m['text']} (相似度: {m['score']:.2f})" for m in memories)
                context_messages.append(f"【相關記憶回溯】:\n{mem_str}")
        except Exception as e:
            print(f"⚠️ [AudioBridge] 記憶檢索失敗: {e}")

        # 3. 產生回覆
        reply_text = gemini.reply_to_text(transcript, context_messages=context_messages)
    except Exception as e:
        if bridge:
            send_command(bridge, "STATUS_SAD")
        return JSONResponse(status_code=502, content={"error": str(e)})

    # 4. 對話寫回記憶 (Memory Write-back) - 與 Telegram 完全同步
    try:
        memory.remember(
            f"User: {transcript}\nHermes: {reply_text}",
            category="stackchan_voice",
            importance=2,
        )
    except Exception as e:
        print(f"⚠️ [AudioBridge] 記憶寫入失敗: {e}")

    _log_conversation(inbox_path, reply_text, transcript=transcript)

    wav_path = tts.synthesize(reply_text)
    if bridge:
        send_command(bridge, "STATUS_HAPPY")
    return FileResponse(wav_path, media_type="audio/wav", filename=os.path.basename(wav_path))


@app.get("/audio/{filename}")
def get_audio(filename: str):
    path = os.path.join(config.AUDIO_OUTBOX_DIR, filename)
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(path, media_type="audio/wav", filename=filename)


def _log_conversation(inbox_path, reply_text, transcript=None):
    os.makedirs(os.path.dirname(config.EVENTS_LOG_PATH), exist_ok=True)
    record = {
        "timestamp": datetime.now().isoformat(),
        "topic": "voice/conversation",
        "payload": {"audio_in": inbox_path, "transcript": transcript, "reply_text": reply_text},
    }
    with open(config.EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
