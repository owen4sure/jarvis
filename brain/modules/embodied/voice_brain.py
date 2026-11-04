"""
voice_brain — 家裡語音互動的「大腦核心」（與 Telegram 共用記憶/金鑰/人格）
============================================================================
把一句使用者的話（transcript）變成 Hermes 的回覆文字。包含：
  - 意圖路由（與 Telegram 共用 COMMAND_TABLE：天氣/記帳/查詢…）
  - 長期記憶注入（hermes-agent USER.md/MEMORY.md + 向量回溯）
  - 真實時間注入
  - 對話寫回記憶（與 Telegram 完全同步）

這個函式「不碰硬體」——表情/說話由呼叫端決定（舊 MQTT 韌體用 send_command，
新 xiaozhi 裝置用 stackchan-mcp 的 say()/set_avatar()）。這樣同一顆大腦可以
同時服務兩種輸出管道，符合「三者同一個東西、好管理」。
"""
from datetime import datetime

from .gemini_client import GeminiClient
from .skill_context import SkillContext
from ..memory import hermes_agent_bridge
from ..memory.memory_manager import MemoryManager

_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]

# 語音不適合觸發的指令（會改程式碼/需上傳檔案/純文字選單）
VOICE_BLOCKED_COMMANDS = {
    "dev", "plaud", "help", "start", "model", "models", "status", "sync",
}


class _VoiceContext:
    """讓 COMMAND_TABLE handler 能在語音迴圈裡跑，共用同一份 memory/gemini。"""

    def __init__(self, gemini, memory, bridge=None):
        self.bridge = bridge
        self.skill_ctx = SkillContext(bridge, sensory=None) if bridge else None
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


def _try_voice_command(transcript, gemini, memory, bridge=None, intent=None):
    """若這句話對應到某個指令就執行，回傳文字回覆；否則 None（走聊天）。
    intent 可由上層先分類好傳入，避免重複呼叫分類器。"""
    from modules.remote import intent_router
    from modules.remote.commands import COMMAND_TABLE

    if intent is None:
        intent = intent_router.classify(transcript, gemini)
    cmd_name = intent.get("command", "chat")
    if cmd_name in ("chat", "") or cmd_name in VOICE_BLOCKED_COMMANDS:
        return None
    cmd_fn = COMMAND_TABLE.get(cmd_name)
    if not cmd_fn:
        return None
    try:
        ctx = _VoiceContext(gemini, memory, bridge)
        return cmd_fn(intent.get("args", ""), ctx)
    except Exception as e:
        print(f"⚠️ [voice_brain] 語音指令 {cmd_name} 執行失敗: {e}")
        return None


def handle_utterance(transcript, gemini=None, memory=None, bridge=None, intent=None):
    """把使用者語音文字 → Hermes 回覆文字。失敗時丟例外由呼叫端處理。
    intent 可由上層先分類好傳入（語音迴圈遊戲路由會這樣做），避免重複分類。"""
    gemini = gemini or GeminiClient()
    memory = memory or MemoryManager()

    # 1. 意圖路由（與 Telegram 共用）
    command_reply = _try_voice_command(transcript, gemini, memory, bridge, intent=intent)
    if command_reply:
        _remember(memory, transcript, command_reply)
        return command_reply

    # 2. 注入長期記憶 + 真實時間
    context_messages = []
    now = datetime.now()
    now_str = (f"{now.year}年{now.month}月{now.day}日 "
               f"星期{_WEEKDAY_ZH[now.weekday()]} {now.strftime('%H:%M')}")
    context_messages.append(f"【現在的真實日期時間（請以此為準，不要自行推測）】:\n{now_str}")

    try:
        user_profile = hermes_agent_bridge.read_user_profile()
        if user_profile:
            context_messages.append(f"【使用者個人檔案 (hermes-agent)】:\n{user_profile}")
        project_memory = hermes_agent_bridge.read_project_memory()
        if project_memory:
            context_messages.append(f"【專案長期記憶 (hermes-agent)】:\n{project_memory}")
    except Exception as e:
        print(f"⚠️ [voice_brain] 讀取 hermes-agent 記憶失敗: {e}")

    try:
        memories = memory.recall(transcript, top_k=3,
                                 exclude_categories=("telegram_chat", "stackchan_voice"))
        if memories:
            mem_str = "\n".join(f"- {m['text']} (相似度: {m['score']:.2f})" for m in memories)
            context_messages.append(f"【相關記憶回溯】:\n{mem_str}")
    except Exception as e:
        print(f"⚠️ [voice_brain] 記憶檢索失敗: {e}")

    # 3. 產生回覆 + 寫回記憶
    reply_text = gemini.reply_to_text(transcript, context_messages=context_messages)
    _remember(memory, transcript, reply_text)
    return reply_text


def _remember(memory, transcript, reply_text):
    try:
        memory.remember(
            f"User(語音): {transcript}\nHermes: {reply_text}",
            category="stackchan_voice",
            importance=2,
        )
    except Exception as e:
        print(f"⚠️ [voice_brain] 記憶寫入失敗: {e}")
