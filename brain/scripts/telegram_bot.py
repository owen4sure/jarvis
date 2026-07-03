"""Hermes Telegram Bot - long-polling entrypoint.

Usage: ./scripts/start_telegram_bot.sh

Lets you control Hermes/StackChan and chat with Gemini from Telegram.
Authorization is whitelist-based via config/telegram.json
(allowed_user_ids). The first time a Telegram
user's numeric id is listed here, the bot will run no
commands for them - it only replies with their id so they can add themselves
and restart the bot (see scripts/telegram_bot.py).
"""

import mimetypes
import threading
import time
import os
from datetime import datetime

from modules.embodied.gemini_client import GeminiClient
from modules.embodied.mqtt_bridge import MQTTBridge
from modules.embodied.skill_context import SkillContext
from modules.memory import hermes_agent_bridge
from modules.memory.memory_manager import MemoryManager
from modules.productivity import activity_tracker
from modules.productivity.plaud_style.integrator import PlaudIntegrator
from modules.productivity.research_module import ResearchModule
from modules.remote.commands import COMMAND_TABLE
from modules.remote.telegram_handler import TelegramHandler
from modules.remote import intent_router
from modules.remote import pending_queue
from modules.embodied.llm_fallback import BrainUnavailable


PLAUD_INBOX_DIR = "/Users/USERNAME/Hermes_Brain/memory/plaud_inbox"
PLAUD_REPORTS_DIR = "/Users/USERNAME/Hermes_Brain/memory/plaud_reports"

# 每個聊天的最近幾輪對話（chat_id → deque[(使用者, Jarvis)]），給多輪 follow-up 用
import collections as _collections
_CONVO = _collections.defaultdict(lambda: _collections.deque(maxlen=4))


def _record_turn(chat_id, user_text, reply):
    """把一輪對話存進歷史，下一句 follow-up 才有脈絡。"""
    try:
        if chat_id is not None and user_text and reply:
            _CONVO[chat_id].append((str(user_text)[:200], str(reply)[:300]))
    except Exception:
        pass


# 觸發「專案全景圖」注入的關鍵字 - 只有問到系統/開發相關話題時才附帶
# MASTER_INDEX/SYSTEM_STATUS/HANDOFF_NOTES 的內容
_PROJECT_KEYWORDS = (
    "進度", "狀態", "系統", "修復", "修好", "bug", "記憶系統", "模型", "model",
    "telegram", "stackchan", "hermes", "專案", "開發", "程式碼", "代碼",
    "功能", "架構", "launchd", "config", "embedding", "roadmap",
)

def _classify_intent(text: str, gemini) -> dict:
    """Classify intent via the shared router (same logic as the voice loop)."""
    return intent_router.classify(text, gemini)


def _looks_like_usage(s) -> bool:
    """判斷指令回的是不是『用法/說明』這種機械訊息（代表它沒真的理解使用者）。
    是的話 dispatch 會改走智慧對話，而不是把生硬的 /指令用法丟給使用者。"""
    if not isinstance(s, str):
        return False
    head = s.strip()[:24]
    markers = ("用法:", "用法：", "用法 ", "❓ 未知", "格式:", "格式：", "請使用 /", "範例:")
    return head.startswith("用法") or any(m in head for m in markers)


class BotContext:
    def __init__(self, bridge):
        self.bridge = bridge
        self.skill_ctx = SkillContext(bridge, sensory=None)
        self.gemini = GeminiClient()
        self.memory = MemoryManager()
        self.research = ResearchModule()
        # Set per-message so async commands can send replies directly
        self.chat_id = None
        self.handler = None


import concurrent.futures as _cf
# 共用執行緒池：讓 recall / classify / 回覆 並行（代理多金鑰能同時跑，省掉序列相加）
_RECALL_POOL = _cf.ThreadPoolExecutor(max_workers=6, thread_name_prefix="tg-fast")


def _safe_recall(ctx, text):
    try:
        return ctx.memory.recall(text, top_k=3,
                                 exclude_categories=("telegram_chat", "stackchan_voice"))
    except Exception as e:
        print(f"⚠️ [Telegram Bot] 記憶檢索失敗: {e}")
        return []


def _brain_via_bridge(text, context_messages):
    """把 Telegram 訊息走語音那條【橋接 8643】→ 同一個大腦(hermes-agent)+完整 MCP 工具
    +確定性記帳攔截+身份注入，跟語音完全一致。context_messages 是一串情境 str，併成一則 system 訊息。"""
    import urllib.request as _u
    import json as _j
    msgs = []
    if context_messages:
        msgs.append({"role": "system", "content": "\n\n".join(str(c) for c in context_messages)})
    msgs.append({"role": "user", "content": str(text)})
    body = {"model": "hermes", "messages": msgs, "stream": False}
    req = _u.Request("http://127.0.0.1:8643/v1/chat/completions",
                     data=_j.dumps(body).encode(),
                     headers={"Content-Type": "application/json"})
    r = _j.loads(_u.urlopen(req, timeout=90).read())
    return r["choices"][0]["message"]["content"]


def _safe_chat(ctx, text, context_messages):
    """投機先算好的 chat 回覆。回 (status, reply)：ok / unavailable / error。
    先走橋接(跟語音同一個大腦+工具+記帳攔截)；橋接/大腦掛了才 fallback 回直連 Gemini(不會比以前差)。"""
    try:
        return ("ok", _brain_via_bridge(text, context_messages))
    except Exception as _be:
        print(f"⚠️ [Telegram Bot] 橋接大腦失敗，fallback 直連 Gemini: {_be}")
        try:
            return ("ok", ctx.gemini.chat(text, context_messages=context_messages))
        except BrainUnavailable:
            return ("unavailable", None)
        except Exception as e:
            import traceback
            print(f"⚠️ [Telegram Bot] 回覆失敗: {e}\n{traceback.format_exc()}")
            return ("error", None)


def dispatch(text, ctx):
    text = text.strip()
    if not text:
        return None

    if text.startswith("/"):
        parts = text[1:].split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        handler = COMMAND_TABLE.get(command)
        if handler is None:
            return f"❓ 未知指令: /{command}\n輸入 /help 看可用指令。"
        try:
            return handler(args, ctx)
        except Exception as e:
            import traceback
            print(f"⚠️ [指令 /{command}] 失敗: {e}\n{traceback.format_exc()}")
            return "這個我剛剛沒做好 🙏 換個說法再跟我說一次？"

    # 客製化指令（硬觸發）：說「開工」就直接執行動作，不靠 LLM 注意記憶 → 可靠、秒回
    try:
        import modules.custom_commands as _cc
        _cmd = _cc.match(text)
        if _cmd:
            _r = _cc.execute(_cmd)
            if _r:
                return _r
    except Exception as e:
        print(f"⚠️ [Telegram Bot] 客製化指令出錯: {e}")

    # 正在等使用者回答「提早多久提醒」→ 把這句當答案，補上並建立提醒
    try:
        import modules.remote.commands as _cmds
        _pend = _cmds.complete_pending_reminder(getattr(ctx, "chat_id", None), text)
        if _pend:
            return _pend
    except Exception as e:
        print(f"⚠️ [Telegram Bot] 提醒待補出錯: {e}")

    # 速度優化（投機並行）：先把 context 建好（recall 已是本地 ~0.17s），再把 classify 與
    # chat 回覆「同時」丟代理跑（實測代理多金鑰能並行）。chat 意圖（多數）→ 回覆早就算好，
    # 省掉「先等 classify 1 秒、再等 reply 1 秒」的相加。
    _recall_future = _RECALL_POOL.submit(_safe_recall, ctx, text)

    # ---- 建 context（近期對話、真實時間、共用記憶、專案檔、長期記憶回溯）----
    context_messages = []
    try:
        _h = _CONVO.get(getattr(ctx, "chat_id", None))
        if _h:
            _convo = "\n".join(f"你：{u}\nJarvis：{a}" for u, a in _h)
            context_messages.append("【最近這幾輪對話（理解 follow-up／代名詞用，別重複問已知的事）】：\n" + _convo)
    except Exception:
        pass

    _WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]
    _now = datetime.now()
    _now_str = f"{_now.year}年{_now.month}月{_now.day}日 星期{_WEEKDAY_ZH[_now.weekday()]} {_now.strftime('%H:%M')}"
    context_messages.append(f"【現在的真實日期時間（請以此為準，不要自行推測）】:\n{_now_str}")

    try:
        user_profile = hermes_agent_bridge.read_user_profile()
        if user_profile:
            context_messages.append(f"【使用者個人檔案 (hermes-agent)】:\n{user_profile}")
        project_memory = hermes_agent_bridge.read_project_memory()
        if project_memory:
            context_messages.append(f"【專案長期記憶 (hermes-agent)】:\n{project_memory}")
    except Exception as e:
        print(f"⚠️ [Telegram Bot] 讀取 hermes-agent 記憶失敗: {e}")

    if any(kw.lower() in text.lower() for kw in _PROJECT_KEYWORDS):
        project_files = {
            "MASTER_INDEX.md": "專案總綱與架構指南",
            "SYSTEM_STATUS.md": "當前系統狀態與進度報告",
            "HANDOFF_NOTES.md": "近期學習紀錄與坑點總結",
        }
        for filename, description in project_files.items():
            filepath = os.path.join("/Users/USERNAME/Hermes_Brain", filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                        context_messages.append(f"【{description}】:\n{content[-1500:]}")
                except Exception as e:
                    print(f"⚠️ [Telegram Bot] 讀取 {filename} 失敗: {e}")

    try:
        memories = _recall_future.result(timeout=3)
        if memories:
            mem_str = "\n".join([f"- {m['text']} (相似度: {m['score']:.2f})" for m in memories])
            context_messages.append(f"【相關記憶回溯】:\n{mem_str}")
    except Exception as e:
        print(f"⚠️ [Telegram Bot] 記憶檢索失敗: {e}")

    # ---- 投機並行：classify 與 chat 回覆同時開跑（多數是 chat → 回覆已 ready）----
    intent_future = _RECALL_POOL.submit(_classify_intent, text, ctx.gemini)
    reply_future = _RECALL_POOL.submit(_safe_chat, ctx, text, context_messages)

    intent = intent_future.result()
    cmd_name = intent.get("command", "chat")
    cmd_args = intent.get("args", "")
    if cmd_name != "chat":
        cmd_fn = COMMAND_TABLE.get(cmd_name)
        if cmd_fn:
            try:
                result = cmd_fn(cmd_args, ctx)
                # 指令真的有處理才回（投機的 chat 回覆就丟棄）；回「用法/說明」代表它沒聽懂
                # 自然語言，就改用投機已算好的 chat 回覆，不把生硬用法丟給使用者。
                if result and not _looks_like_usage(result):
                    return result
            except Exception as e:
                import traceback
                print(f"⚠️ [意圖 {cmd_name}] 失敗: {e}\n{traceback.format_exc()}")

    # chat 意圖：直接取投機已算好的回覆（多數情況早就 ready，幾乎不用再等）
    status, reply = reply_future.result()
    if status == "unavailable":
        try:
            pending_queue.enqueue(getattr(ctx, "chat_id", None), text)
        except Exception as e:
            print(f"⚠️ [Telegram Bot] 排入待回覆佇列失敗: {e}")
        return ("⚠️ 我的大腦暫時連不上（Gemini 沒回應）。\n"
                "已經把你的訊息記下來，等連線恢復我會自動補上回覆 🙏")
    if status == "error" or reply is None:
        return "我這邊剛剛有點忙不過來，再跟我說一次好嗎 🙏"

    # 3. 對話寫回記憶 (Memory Write-back) - 讓記憶與對話完全同步
    # 在背景執行緒寫入，不拖慢回覆速度（embedding API 還要再花 1-2 秒）
    def _write_back():
        try:
            ctx.memory.remember(
                f"User: {text}\nHermes: {reply}",
                category="telegram_chat",
                importance=2,
            )
        except Exception as e:
            print(f"⚠️ [Telegram Bot] 記憶寫入失敗: {e}")
        # 同步進 dashboard 對話頁（標來源 Telegram）→ 對話頁就會有 Telegram 的對話
        try:
            import urllib.request as _u, json as _j
            _u.urlopen(_u.Request("http://127.0.0.1:8811/api/chat/log",
                       data=_j.dumps({"user": text, "assistant": reply, "source": "Telegram"}).encode(),
                       headers={"Content-Type": "application/json"}), timeout=3)
        except Exception:
            pass

    threading.Thread(target=_write_back, daemon=True).start()

    return reply


def _run_plaud_pipeline(audio_path, chat_id, telegram, ctx):
    """Run STT + summary + action-item extraction on `audio_path`, send the
    resulting report back as a file, and remember it (importance=3)."""
    os.makedirs(PLAUD_REPORTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(PLAUD_REPORTS_DIR, f"{timestamp}_report.md")
    try:
        report = PlaudIntegrator().run_pipeline(audio_path, report_path)
        telegram.send_document(chat_id, report_path, caption="📋 會議報告")
        ctx.memory.remember(
            f"Plaud 會議報告 ({timestamp}, {os.path.basename(audio_path)}):\n{report[:1000]}",
            category="plaud_report",
            importance=3,
        )
    except Exception as e:
        telegram.send_message(chat_id, f"⚠️ Plaud 處理失敗: {e}")


def _handle_photo(photo_or_doc, caption, chat_id, telegram, ctx):
    """傳圖 → Gemini Vision 看圖回答（背景執行，不卡輪詢）。
    也是機器人『眼睛』的同一條視覺管道。"""
    telegram.send_message(chat_id, "👀 看圖中…")
    os.makedirs(PLAUD_INBOX_DIR, exist_ok=True)
    file_id = photo_or_doc["file_id"]
    img_path = os.path.join(PLAUD_INBOX_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_img.jpg")

    def _run():
        try:
            telegram.download_file(file_id, img_path)
            with open(img_path, "rb") as f:
                image_bytes = f.read()
            mime = photo_or_doc.get("mime_type", "image/jpeg")
            reply = ctx.gemini.analyze_image(image_bytes, question=caption, mime_type=mime)
            telegram.send_message(chat_id, reply)
            ctx.memory.remember(
                f"User 傳了一張圖" + (f"（說明：{caption}）" if caption else "") +
                f"，Hermes 看到：{reply[:400]}",
                category="telegram_vision", importance=2,
            )
        except Exception as e:
            telegram.send_message(chat_id, f"⚠️ 看圖失敗: {e}")

    threading.Thread(target=_run, daemon=True).start()


def _handle_voice_conversation(voice_obj, chat_id, telegram, ctx):
    """短語音訊息 → 轉文字 → 走和打字一樣的對話/意圖流程（背景執行）。
    與 StackChan 語音、Telegram 打字共用同一顆大腦與記憶。"""
    def _run():
        os.makedirs(PLAUD_INBOX_DIR, exist_ok=True)
        mime = voice_obj.get("mime_type", "audio/ogg")
        ext = mimetypes.guess_extension(mime) or ".ogg"
        path = os.path.join(PLAUD_INBOX_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_vn{ext}")
        try:
            telegram.download_file(voice_obj["file_id"], path)
            with open(path, "rb") as f:
                audio_bytes = f.read()
            transcript = ctx.gemini.transcribe(audio_bytes, mime_type=mime)
        except Exception as e:
            telegram.send_message(chat_id, f"⚠️ 語音辨識失敗: {e}")
            return
        if not transcript or not transcript.strip():
            telegram.send_message(chat_id, "（沒聽清楚，再說一次？）")
            return
        ctx.chat_id = chat_id
        ctx.handler = telegram
        reply = dispatch(transcript, ctx)
        _record_turn(chat_id, transcript, reply)
        if reply:
            telegram.send_message(chat_id, f"🎙️ 「{transcript.strip()}」\n\n{reply}")

    threading.Thread(target=_run, daemon=True).start()


def _handle_plaud_audio(audio_obj, chat_id, telegram, ctx):
    """Download a voice/audio message from Telegram and run it through the
    Plaud pipeline in the background (doesn't block the polling loop)."""
    telegram.send_message(chat_id, "🎙️ 收到錄音，開始轉錄與分析（完成後會回傳報告檔）...")

    os.makedirs(PLAUD_INBOX_DIR, exist_ok=True)
    mime_type = audio_obj.get("mime_type", "audio/ogg")
    ext = mimetypes.guess_extension(mime_type) or ".ogg"
    audio_path = os.path.join(PLAUD_INBOX_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}")

    def _run():
        try:
            telegram.download_file(audio_obj["file_id"], audio_path)
        except Exception as e:
            telegram.send_message(chat_id, f"⚠️ 下載錄音失敗: {e}")
            return
        _run_plaud_pipeline(audio_path, chat_id, telegram, ctx)

    threading.Thread(target=_run, daemon=True).start()


def _pending_retrier(telegram, ctx):
    """背景每 60 秒檢查待回覆佇列；大腦恢復就把積壓的問題補上回覆。"""
    from modules.remote import pending_queue
    while True:
        time.sleep(60)
        try:
            pending_queue.purge_expired()
            for item in pending_queue.list_pending():
                text = item.get("text", "")
                chat_id = item.get("chat_id")
                if not text or chat_id is None:
                    pending_queue.remove(item["id"])
                    continue
                try:
                    try:
                        reply = _brain_via_bridge(text, [])   # 跟語音同一個大腦+工具
                    except Exception:
                        reply = ctx.gemini.chat(text)          # 橋接掛 → fallback 直連
                except BrainUnavailable:
                    break  # 大腦還沒好，下一輪再試
                except Exception:
                    pending_queue.bump_try(item["id"])
                    continue
                telegram.send_message(chat_id, f"🔁 補回覆「{text}」：\n\n{reply}")
                pending_queue.remove(item["id"])
        except Exception as e:
            print(f"⚠️ [Telegram Bot] 待回覆佇列處理失敗: {e}")


def _maybe_announce_recovery(telegram):
    """重開機/長時間離線後，主動告訴使用者『我回來了，離線了多久』。"""
    from modules.remote import presence
    down = presence.downtime_seconds()
    if down >= 600:  # 離線超過 10 分鐘才通知，避免單純重啟也吵
        msg = f"✅ 我回來上線了（剛剛離線約 {presence.human_duration(down)}）。"
        for uid in getattr(telegram, "allowed_user_ids", []):
            try:
                telegram.send_message(uid, msg)
            except Exception:
                pass


def main():
    print("🤖 [Telegram Bot] 啟動中...")

    telegram = TelegramHandler()

    bridge = MQTTBridge(client_id="telegram_bot")
    try:
        bridge.connect()
        bridge.loop_start()
        print("✅ [MQTTBridge] 已連線")
    except Exception as e:
        print(f"⚠️ [MQTTBridge] 無法連線到硬體 (MQTT)，將以「純軟體模式」運行。錯誤: {e}")

    ctx = BotContext(bridge)

    # Mac 關機重開：從上次 offset 接續、偵測離線時長並通知
    from modules.remote import presence
    _maybe_announce_recovery(telegram)
    offset = presence.last_offset()
    presence.write(offset)
    threading.Thread(target=_pending_retrier, args=(telegram, ctx), daemon=True).start()

    print("✅ [Telegram Bot] 已連線，開始輪詢訊息...")

    _last_heartbeat = 0
    while True:
        try:
            updates = telegram.get_updates(offset=offset)
        except Exception as e:
            # 連線卡死/逾時 → 快速重連撈訊息（別讓你的訊息枯等），只稍歇避免狂打
            print(f"⚠️ [Telegram Bot] getUpdates 失敗，立即重連: {e}")
            time.sleep(1)
            continue

        # 心跳：每 ~30 秒寫一次（含目前 offset），給離線偵測用
        if time.time() - _last_heartbeat > 30:
            presence.write(offset)
            _last_heartbeat = time.time()

        for update in updates:
            offset = update["update_id"] + 1
            presence.write(offset)

            message = update.get("message")
            if not message:
                continue

            # 頻道貼文 / 編輯訊息等沒有 from/chat → 跳過，不要讓整個 polling 迴圈崩掉
            _from = message.get("from") or {}
            _chat = message.get("chat") or {}
            user_id = _from.get("id")
            chat_id = _chat.get("id")
            if user_id is None or chat_id is None:
                continue

            if not telegram.is_authorized(user_id):
                telegram.send_message(
                    chat_id,
                    "⚠️ 你尚未被授權使用這個 Bot。\n"
                    f"你的 user_id 是: {user_id}\n"
                    "請將這個數字加入 Hermes_Brain/config/telegram.json 的 "
                    "allowed_user_ids 陣列，並重新啟動 telegram_bot 後再試一次。",
                )
                print(f"🚫 [Telegram Bot] 未授權使用者 user_id={user_id} chat_id={chat_id}")
                continue

            # 記錄使用者活動時間，供「深夜未回通知緊急聯絡人」watcher 判斷
            try:
                activity_tracker.record_activity()
            except Exception as e:
                print(f"⚠️ [Telegram Bot] 記錄活動時間失敗: {e}")

            # 圖片（或圖片文件）-> Gemini Vision 看圖回答
            photo = message.get("photo")
            doc = message.get("document")
            if photo:
                largest = photo[-1]  # Telegram 給多種尺寸，取最大張
                _handle_photo(largest, message.get("caption", ""), chat_id, telegram, ctx)
                continue
            if doc and str(doc.get("mime_type", "")).startswith("image/"):
                _handle_photo(doc, message.get("caption", ""), chat_id, telegram, ctx)
                continue

            # 語音訊息：短的 (<60s) 當「對話」，長的當「會議錄音」跑 Plaud 報告。
            # 音訊檔 / 音訊文件一律 Plaud。
            voice = message.get("voice")
            if voice and voice.get("duration", 0) < 60:
                _handle_voice_conversation(voice, chat_id, telegram, ctx)
                continue
            audio_obj = voice or message.get("audio")
            if not audio_obj and doc and str(doc.get("mime_type", "")).startswith("audio/"):
                audio_obj = doc
            if audio_obj:
                _handle_plaud_audio(audio_obj, chat_id, telegram, ctx)
                continue

            if "text" not in message:
                continue

            ctx.chat_id = chat_id
            ctx.handler = telegram
            telegram.send_chat_action(chat_id, "typing")  # 瞬間顯示「輸入中…」，消除死等的延遲感
            import time as _tm
            _t_recv = _tm.time()
            reply = dispatch(message["text"], ctx)
            print(f"⏱ [dispatch] 「{str(message.get('text',''))[:24]}」 實際花了 {_tm.time()-_t_recv:.2f} 秒")
            _record_turn(chat_id, message["text"], reply)
            if reply and not getattr(ctx, "_already_sent", False):
                telegram.send_message(chat_id, reply)
            ctx._already_sent = False


if __name__ == "__main__":
    main()
