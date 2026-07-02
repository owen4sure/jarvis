"""Shared natural-language intent router.

Both the Telegram bot and the StackChan voice loop use this so the user
never needs to memorize /commands on either channel — they just say what
they want and it maps to the right COMMAND_TABLE entry.

The feature list is built dynamically from commands.COMMAND_DESCRIPTIONS,
so every new command (including ones Claude Code adds via /dev) becomes
voice- and chat-routable automatically, as long as it registers a
description there.

設計原則（2026-06 大改）：**預設一律 chat，只有「明確要求某動作」才路由到功能**。
過去太愛硬塞指令，導致「晚安→記起床」「好累→記心情」「股票→緊急聯絡人」這種離譜誤判。
現在用嚴格規則 + few-shot 範例，模糊/抒發/打招呼/亂打字一律走 chat（chat 本身很聰明，
會處理記帳、改財務、查財務、回答問題）。
"""

import json
import re

_RULES = """你是 Hermes 的意圖路由器。判斷使用者這句話是否「明確要執行下面某個功能」。

功能列表：
{feature_list}
- chat: 一般聊天、閒聊、抒發心情、打招呼、講自己的近況、問你是誰、問財務/股票狀況、或任何不明確對應上面功能的話

【最重要原則】
1. 預設一律回 chat。只有當使用者「明確、清楚地要求」某個功能時，才選那個功能。
2. 只要有一點不確定，或這句是在「陳述、抒發情緒、打招呼、閒聊、亂打字、太模糊」→ 一律 chat。
3. 寧可 chat 也不要硬塞功能。chat 很聰明，會自己處理記帳、改財務數字、查財務、查股票、回答問題、記住個人資訊。
4. 絕對不要因為句子裡剛好有某個字（如「累/睡/股票/銀行/錢/朋友」）就硬套功能——要看「使用者是不是真的在要求那個動作」。

【判斷範例】（務必照這個尺度）
"幫我記午餐花了120" → {{"command":"expense","args":"120 午餐"}}
"剛買咖啡85" → {{"command":"expense","args":"85 咖啡"}}
"禮拜五早上十點提醒我開會" → {{"command":"remind","args":"禮拜五早上十點 開會"}}
"20分鐘後叫我" → {{"command":"remind","args":"20分鐘後 提醒"}}
"台北天氣如何" → {{"command":"weather","args":"台北"}}
"100美金多少台幣" → {{"command":"convert","args":"100 美金 台幣"}}
"幫我記住瓦斯費月底要繳" → {{"command":"remember","args":"瓦斯費月底要繳"}}
"我女朋友叫小美" → {{"command":"remember","args":"我女朋友叫小美"}}
"我住新北市" → {{"command":"remember","args":"我住新北市"}}
"幫我查一下台積電的新聞" → {{"command":"research","args":"台積電 新聞"}}
"我的股票現在賺還是賠" → {{"command":"chat","args":""}}
"我還能花多少錢" → {{"command":"chat","args":""}}
"我銀行剩兩萬" → {{"command":"chat","args":""}}
"這個月想多存五千" → {{"command":"chat","args":""}}
"薪水多少" → {{"command":"chat","args":""}}
"我今天好累" → {{"command":"chat","args":""}}
"晚安" → {{"command":"chat","args":""}}
"你好" → {{"command":"chat","args":""}}
"在嗎" → {{"command":"chat","args":""}}
"你是誰" → {{"command":"chat","args":""}}
"謝謝你" → {{"command":"chat","args":""}}
"asdfghjkl" → {{"command":"chat","args":""}}
"????" → {{"command":"chat","args":""}}
"。。。" → {{"command":"chat","args":""}}
"幫我看一下" → {{"command":"chat","args":""}}
"怎麼辦啦" → {{"command":"chat","args":""}}
"可以嗎" → {{"command":"chat","args":""}}
"1+1等於多少" → {{"command":"chat","args":""}}
"我想把這個功能刪掉" → {{"command":"chat","args":""}}

規則：
1. 只回傳一行 JSON：{{"command":"功能名稱","args":"萃取的參數"}}
2. args 保留原文語意，讓下游指令能理解
3. 不確定、模糊、抒發、打招呼、亂打字 → 一律 chat
4. 不要輸出任何說明文字，只有 JSON

使用者說："""


def build_prompt() -> str:
    """Build the router prompt from the live COMMAND_DESCRIPTIONS table."""
    from modules.remote.commands import COMMAND_DESCRIPTIONS
    lines = [f"- {cmd}: {desc}" for cmd, desc in COMMAND_DESCRIPTIONS.items()]
    return _RULES.format(feature_list="\n".join(lines))


def classify(text: str, gemini) -> dict:
    """Classify intent with a single Gemini call. Returns {"command": str, "args": str}.

    Deliberately uses a single direct client call (not gemini._generate's
    key-rotation loop) — intent classification is optional; a failure should
    silently fall back to chat, never burn the whole API key pool.
    """
    from modules.embodied import config as _cfg
    try:
        from modules.remote.commands import COMMAND_TABLE
        prompt = build_prompt() + (text or "")[:2000]   # 上限，避免超長輸入爆 token
        client = gemini._client()
        from google.genai import types as _gt
        response = client.models.generate_content(
            model=_cfg.get_gemini_model(),
            contents=[prompt],
            # 關掉思考：意圖分類是分類任務、不需 reasoning，省 ~3 秒
            config=_gt.GenerateContentConfig(
                thinking_config=_gt.ThinkingConfig(thinking_budget=0)),
        )
        raw = (response.text or "").strip()              # 安全過濾時 .text 可能 None
        # 穩健抓出 JSON 物件（避免 .strip("```json") 字元集啃字串）
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"command": "chat", "args": ""}
        result = json.loads(m.group(0))
        cmd = result.get("command", "chat")
        # 白名單：只接受真的有 handler 的指令，否則一律 chat（防 LLM 幻覺出無 handler 的指令崩潰）
        if cmd != "chat" and cmd not in COMMAND_TABLE:
            cmd = "chat"
        return {"command": cmd, "args": result.get("args", "") if cmd != "chat" else ""}
    except Exception:
        pass
    return {"command": "chat", "args": ""}
