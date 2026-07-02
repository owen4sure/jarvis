"""Telegram command table.

Each entry maps a `/command` to a handler `(args: str, ctx: BotContext) -> str`.
`ctx` is built once in scripts/telegram_bot.py and gives commands access to
the embodied MQTT bridge (StackChan control), memory, and Gemini chat -
so improving the bot is just adding a new entry here, following the same
"small table, one file per concern" pattern as
modules/embodied/command_mapper.py.
"""

import os
import re
import urllib.request
import json
from datetime import datetime

from google import genai

from modules.embodied import config as embodied_config
from modules.embodied.skills.weather_alert_skill import _fetch_weather
from modules.productivity import (
    checklist_manager,
    contact_tracker,
    daily_content_skills,
    emergency_contact,
    expense_tracker,
    food_tracker,
    mood_tracker,
    quiz_manager,
    reminder_manager,
    sleep_tracker,
    watcher_manager,
)
from modules.productivity.email_module import EmailModule, EmailNotConfiguredError
from modules.productivity.plaud_style.integrator import PlaudIntegrator
from modules.productivity.research_module import ResearchModule
from scripts.key_manager import KeyManager

PLAUD_REPORTS_DIR = "/Users/chenyouwei/Hermes_Brain/memory/plaud_reports"

# 排除非聊天用模型（embedding/語音/圖像/影片等）
_MODEL_EXCLUDE_KEYWORDS = ("embedding", "tts", "veo", "live", "image", "audio")


def _list_available_models():
    """向 Gemini API 取得這組 key 真正可用的模型清單（去除 'models/' 前綴）。"""
    client = genai.Client(api_key=KeyManager().get_key())
    return sorted(m.name.split("/", 1)[-1] for m in client.models.list())

# Simple expression/LED/gesture shortcuts -> embodied intents
_EXPRESSION_COMMANDS = {
    "happy": "STATUS_HAPPY",
    "sad": "STATUS_SAD",
    "angry": "STATUS_ANGRY",
    "think": "THINKING",
    "standby": "STANDBY",
    "nod": "NOD",
    "shake": "SHAKE_HEAD",
}

_LED_COMMANDS = {
    "green": "LED_GREEN_BLINK",
    "red": "LED_RED_BLINK",
    "white": "EYE_LED_ON",
    "off": "LED_OFF",
}


def cmd_help(args, ctx):
    return (
        "🤖 Hermes Telegram 指令\n\n"
        "/status - 系統與 StackChan 連線狀態\n"
        "/say <文字> - 讓 StackChan 講這句話\n"
        "/happy /sad /angry /think /standby - 切換表情\n"
        "/nod /shake - 點頭 / 搖頭手勢\n"
        "/led <green|red|white|off> - 控制 LED\n"
        "/weather - 查詢目前天氣（含紫外線指數、空氣品質）\n"
        "/convert <金額> <原幣別> <目標幣別> - 匯率換算（例: /convert 100 USD TWD）\n"
        "/remember <文字> - 記住一件事\n"
        "/recall <關鍵字> - 回憶相關記憶\n"
        "/research <文字> - 深度研究（Gemini + Google Search）\n"
        "/plaud <音檔路徑> - 將會議錄音轉成逐字稿+摘要+待辦報告\n"
        "/email - 未讀郵件摘要（需先設定 config/email.json）\n"
        "/remind add/addskill HH:MM [規則] <訊息> / list / remove <編號> - 定時提醒"
        "（daily/weekly/monthly/annual/once，可接生日、證件到期、每日單字等）\n"
        "/watch crypto|stock|flight|earthquake|typhoon|checkin ... / list / remove - 價格/航班/地震/颱風/深夜未回警示\n"
        "/expense <金額> <類別> [備註] - 記帳\n"
        "/budget [set <金額>] - 查看/設定每月預算與剩餘\n"
        "/mood <1-5> [備註] - 記錄今天的心情\n"
        "/checklist [add|remove] [項目] - 出門清單\n"
        "/food [add|remove|list] - 食材到期追蹤\n"
        "/contact list/add/touch/remove - 久未聯絡提醒\n"
        "/sleep start/end/status/target HH:MM - 睡眠記錄與就寢提醒\n"
        "/emergency set <chat_id> [名字] / status / remove - 深夜未回緊急聯絡人\n"
        "/quiz [主題] - 出一題練習；/answer <回答> - 評分\n"
        "/model [model_id] - 查看/切換目前使用的 Gemini 模型\n"
        "/models - 列出可用的模型清單\n"
        "/sync - 要求 StackChan 補發離線事件\n"
        "/agent <任務> - 🧠 交給完整深度代理（寫程式/查資料/操作電腦，多步驟自動完成）\n\n"
        "✨ 也可以直接：\n"
        "• 傳文字 → Gemini 對話\n"
        "• 傳照片（可加說明）→ 看圖回答\n"
        "• 傳短語音（<60秒）→ 當對話；長語音/音訊檔 → 會議報告\n"
        "• 每天早上 08:00 會主動傳你一份簡報"
    )


def cmd_status(args, ctx):
    mqtt_ok = ctx.bridge.client.is_connected()
    lines = [f"MQTT Broker: {'✅ 已連線' if mqtt_ok else '❌ 未連線'}"]

    try:
        url = f"http://localhost:{embodied_config.AUDIO_BRIDGE_PORT}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            json.load(resp)
        lines.append("Embodied Daemon (audio bridge): ✅ 運作中")
    except Exception:
        lines.append("Embodied Daemon (audio bridge): ❌ 未啟動")

    try:
        from modules.memory.fact_manager import FactManager
        fact_count = len(FactManager().list_all())
        lines.append(f"記憶事實數量: {fact_count}")
    except Exception as e:
        print(f"⚠️ [status] memory {e}")
        lines.append("記憶系統: ⚠️ 暫時讀不到")

    return "\n".join(lines)


def cmd_say(args, ctx):
    text = args.strip()
    if not text:
        return "用法: /say <要說的話>"
    ctx.skill_ctx.speak(text)
    return f"🔊 已送出語音指令給 StackChan：「{text}」\n（StackChan 連線後才會實際播放）"


def cmd_expression(intent):
    def handler(args, ctx):
        ctx.skill_ctx.send_command(intent)
        return f"✅ 已送出表情/動作指令: {intent}"
    return handler


def cmd_led(args, ctx):
    color = args.strip().lower()
    if color not in _LED_COMMANDS:
        return "用法: /led <green|red|white|off>"
    intent = _LED_COMMANDS[color]
    ctx.skill_ctx.send_command(intent)
    return f"✅ 已送出 LED 指令: {intent}"


def cmd_weather(args, ctx):
    summary, _is_raining = _fetch_weather()
    return summary


def cmd_remember(args, ctx):
    text = args.strip()
    if not text:
        return "用法：/remember <要記住的內容>，例如「我女朋友叫小美」"
    section = None
    # 主記憶：寫進 8809 RAG（facts.jsonl）→ dashboard / 語音 / Telegram 同一個腦，並自動歸類
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/remember",
                                   data=json.dumps({"fact": text}).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(r, timeout=8))
        section = d.get("section")
    except Exception as e:
        print(f"⚠️ [remember→8809] {e}")
    # 同步寫一份到 MemoryManager（向後相容既有檢索）
    try:
        ctx.memory.remember(text, category="telegram_note", importance=3)
    except Exception:
        pass
    if section:
        return f"🧠 記住了，放進「{section}」分類：「{text}」"
    return f"🧠 已記住：「{text}」"


def cmd_recall(args, ctx):
    query = args.strip()
    if not query:
        return "用法: /recall <關鍵字或問題>"
    results = ctx.memory.recall(query, top_k=5, exclude_categories=("telegram_chat", "stackchan_voice"))
    if not results:
        return "嗯…這個我目前沒有印象耶，你要不要跟我說一次，我幫你記住。"

    # 把記憶當素材，用有個性的對話模型自然回答，而不是生倒搜尋結果
    mem_text = "\n".join(f"- {r['text']}" for r in results)
    prompt = (
        f"使用者問：「{query}」\n\n"
        f"以下是你記憶庫裡相關的內容：\n{mem_text}\n\n"
        "請根據這些記憶，用自然、口語、有溫度的方式直接回答使用者的問題。"
        "不要列出記憶清單、不要顯示相似度分數、不要說「根據記憶」。"
        "如果記憶裡有明確答案就直接講；如果記憶不足以回答，就誠實說你不確定。"
    )
    try:
        return ctx.gemini.chat(prompt)
    except Exception:
        # 萬一對話模型掛了，至少把最相關那筆講出來
        return results[0]["text"]


_CURRENCY_ZH = {
    "美金": "USD", "美元": "USD", "鎂": "USD",
    "台幣": "TWD", "臺幣": "TWD", "新台幣": "TWD",
    "日圓": "JPY", "日元": "JPY", "日幣": "JPY",
    "歐元": "EUR", "英鎊": "GBP", "韓元": "KRW", "韓圓": "KRW",
    "人民幣": "CNY", "港幣": "HKD", "澳幣": "AUD", "加幣": "CAD",
    "泰銖": "THB", "越南盾": "VND", "披索": "PHP", "新加坡幣": "SGD", "星幣": "SGD",
}


def _norm_currency(s):
    """中文幣別→ISO 代碼；已是代碼就原樣。避免中文塞進 URL 爆 ascii 編碼。"""
    s = (s or "").strip()
    if s in _CURRENCY_ZH:
        return _CURRENCY_ZH[s]
    for zh, code in _CURRENCY_ZH.items():
        if zh in s:
            return code
    return re.sub(r"[^A-Za-z]", "", s).upper() or s


def cmd_convert(args, ctx):
    parts = args.split()
    if len(parts) != 3:
        m = re.search(r"(\d[\d,.]*)", args or "")
        zhs = [z for z in _CURRENCY_ZH if z in (args or "")]
        if m and len(zhs) >= 2:
            parts = [m.group(1).replace(",", ""), zhs[0], zhs[1]]
        else:
            return "想換匯的話跟我說「100 美金換台幣」這樣就好。"

    amount_str, from_currency, to_currency = parts
    try:
        amount = float(amount_str.replace(",", ""))
    except ValueError:
        return "想換匯的話跟我說「100 美金換台幣」我就懂。"
    import math as _math
    if not _math.isfinite(amount) or amount <= 0:
        return "金額要是大於 0 的正常數字喔。"

    from_currency = _norm_currency(from_currency)
    to_currency = _norm_currency(to_currency)

    try:
        url = f"https://open.er-api.com/v6/latest/{from_currency}"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"⚠️ [convert] {e}")
        return "匯率服務剛剛連不上，等一下再試一次 🙏"

    if data.get("result") != "success":
        return f"⚠️ 找不到幣別「{from_currency}」，請確認是否為正確的貨幣代碼（如 USD、TWD、JPY）。"

    rates = data.get("rates", {})
    if to_currency not in rates:
        return f"⚠️ 找不到幣別「{to_currency}」，請確認是否為正確的貨幣代碼（如 USD、TWD、JPY）。"

    converted = amount * rates[to_currency]
    return f"💱 {amount:g} {from_currency} = {converted:,.2f} {to_currency}"


def cmd_email(args, ctx):
    try:
        return EmailModule().summarize_inbox(limit=10)
    except EmailNotConfiguredError as e:
        return f"📭 {e}"   # 自訂例外，訊息本身就是給使用者看的設定提示
    except Exception as e:
        print(f"⚠️ [email] {e}")
        return "讀信箱時出了點狀況，等等再試 🙏"


_REMIND_USAGE = (
    "用法:\n"
    "/remind list - 查看目前所有提醒\n"
    "/remind add HH:MM [規則] <訊息> - 新增提醒\n"
    "/remind addskill HH:MM [規則] <skill名稱> - 新增自動產生內容的提醒\n"
    "/remind remove <編號> - 刪除提醒\n\n"
    "規則（可省略，預設 daily）:\n"
    "  daily - 每天\n"
    "  weekly:0-6 - 每週（0=一 ... 6=日）\n"
    "  monthly:DD - 每月第幾天\n"
    "  annual:MM-DD - 每年（生日/紀念日）\n"
    "  once:YYYY-MM-DD - 單次（證件到期/車輛保養）\n\n"
    "skill 名稱: daily_word, daily_thought, daily_digest, "
    "expense_weekly_report, mood_weekly_trend, budget_remaining, "
    "food_expiry_check, contact_check, bedtime_check, "
    "family_schedule_check"
)


def _split_repeat_and_rest(rest):
    """`rest` is "[repeat] <message/skill>". Returns (repeat, remainder)."""
    parts = rest.split(maxsplit=1)
    if len(parts) == 2 and reminder_manager.is_repeat_token(parts[0]):
        return parts[0], parts[1]
    return "daily", rest


def _format_reminder_line(r):
    repeat = r.get("repeat", "daily")
    label = f" [{r['message']}]" if r.get("skill") else f" - {r['message']}"
    skill_tag = f" (skill: {r['skill']})" if r.get("skill") else ""
    return f"#{r['id']} {r['time']} ({repeat}){label}{skill_tag}"


# 待補「提早多久」的提醒：使用者沒講提早多久時，先存這裡並反問，等他回答再真正建立
_PENDING_REMINDER = {}  # chat_id -> {time, message, repeat, channel, desc}


def _parse_lead_and_channel(text):
    """從使用者回答抽出『提早幾分鐘』+『通知方式』。回 (lead 或 None, channel 或 None)。"""
    import re as _re
    t = (text or "").strip()
    lead = None
    if any(k in t for k in ("準時", "不用提早", "不需要", "到時候", "當下", "到時再")):
        lead = 0
    else:
        m = _re.search(r"(\d+)\s*(小時|鐘頭|分鐘|分)", t)
        if m:
            lead = int(m.group(1)) * (60 if m.group(2) in ("小時", "鐘頭") else 1)
    ch = None
    if any(k in t for k in ("語音", "用講的", "唸", "口頭")):
        ch = "voice"
    elif any(k in t for k in ("訊息", "telegram", "文字", "打字", "傳給我")):
        ch = "telegram"
    return lead, ch


def complete_pending_reminder(chat_id, text):
    """若該 chat 正在等『提早多久』的回答，解析並建立提醒。回覆字串，或 None（沒在等/看不懂）。"""
    p = _PENDING_REMINDER.get(chat_id)
    if not p:
        return None
    lead, ch = _parse_lead_and_channel(text)
    if lead is None:
        # 看不懂他在回提早多久 → 放棄這個 pending，讓訊息照常處理（不卡住使用者）
        _PENDING_REMINDER.pop(chat_id, None)
        return None
    _PENDING_REMINDER.pop(chat_id, None)
    channel = ch or p.get("channel", "both")
    try:
        reminder_manager.add_reminder(p["time"], p["message"], repeat=p["repeat"],
                                      lead_minutes=lead, channel=channel)
    except Exception:
        return "設定提醒時出了點狀況，再說一次時間給我好嗎 🙏"
    _lt = f"提早 {lead} 分鐘" if lead else "準時"
    _ct = {"voice": "用語音講", "telegram": "傳訊息", "both": "Telegram＋語音"}[channel]
    return f"✅ 好，{p['desc']} 的「{p['message']}」，{_lt}{_ct}提醒你"


def cmd_remind(args, ctx):
    args = (args or "").strip()
    parts = args.split(maxsplit=2)
    sub = parts[0].lower() if parts else ""

    if not parts or sub == "list":
        reminders = reminder_manager.list_reminders()
        if not reminders:
            return "目前沒有任何提醒。你可以直接說「明天下午3點 開會」或「禮拜五早上11點 面試」。"
        lines = ["⏰ 目前的提醒："]
        for r in reminders:
            lines.append(_format_reminder_line(r))
        return "\n".join(lines)

    if sub in ("remove", "delete", "del"):
        if len(parts) < 2 or not parts[1].isdigit():
            return "用法：/remind remove <編號>（用 /remind list 查看編號）"
        if reminder_manager.remove_reminder(int(parts[1])):
            return f"✅ 已刪除提醒 #{parts[1]}"
        return f"⚠️ 找不到提醒 #{parts[1]}"

    # /remind addskill HH:MM <skill> — 保留原本明確語法
    if sub == "addskill" and len(parts) >= 3:
        time_str = parts[1]
        skill_name = parts[2].strip()
        if skill_name not in daily_content_skills.SKILLS:
            return f"⚠️ 未知的 skill「{skill_name}」，可用：{', '.join(daily_content_skills.SKILLS)}"
        try:
            rid = reminder_manager.add_reminder(time_str, f"({skill_name})", repeat="daily", skill=skill_name)
            return f"✅ 已新增每日提醒 #{rid}：{time_str} -> {skill_name}"
        except Exception:
            return "這個 skill 提醒的時間格式怪怪的，請用 HH:MM（例如 08:30）。"

    # 其餘一律當「新增提醒」，用自然語言解析時間（核心修正：懂禮拜幾/明天/早上幾點…）
    nl_text = args
    if sub in ("add", "新增", "提醒", "remind"):
        nl_text = args.split(maxsplit=1)[1] if len(args.split(maxsplit=1)) > 1 else ""

    from modules.productivity import nl_datetime as nl
    recur = None
    if any(k in nl_text for k in ("每天", "每日", "天天")):
        recur = "daily"
        for k in ("每天", "每日", "天天"):
            nl_text = nl_text.replace(k, "")

    # 提取「提早多久」+「怎麼提醒」（有講就用、沒講就預設，回覆會問要不要改）
    import re as _re
    lead_minutes = 0
    _m = _re.search(r"提早\s*(\d+)\s*(小時|鐘頭|分鐘|分)", nl_text)
    if _m:
        _n = int(_m.group(1))
        lead_minutes = _n * 60 if _m.group(2) in ("小時", "鐘頭") else _n
        nl_text = nl_text[:_m.start()] + nl_text[_m.end():]
    channel, _ch_said = "both", False
    if any(k in nl_text for k in ("用語音", "語音講", "唸給我", "用講的", "口頭")):
        channel, _ch_said = "voice", True
        for k in ("用語音", "語音講", "唸給我", "用講的", "口頭"):
            nl_text = nl_text.replace(k, "")
    elif any(k in nl_text for k in ("用telegram", "傳訊息", "傳給我", "用文字", "打字")):
        channel, _ch_said = "telegram", True
        for k in ("用telegram", "傳訊息", "傳給我", "用文字", "打字"):
            nl_text = nl_text.replace(k, "")

    try:
        fire, message = nl.parse_when(nl_text)
    except Exception:
        fire, message = None, nl_text
    if not fire:
        return ("我沒抓到要提醒的時間 🤔 可以說清楚一點，例如：\n"
                "・「明天下午3點 開會」\n・「禮拜五早上11點 面試」\n・「每天早上8點 吃藥」\n・「30分鐘後 倒垃圾」")
    message = (message or "").strip() or "提醒"
    _repeat = "daily" if recur == "daily" else ("once:" + fire.strftime("%Y-%m-%d"))
    _desc = ("每天 " + fire.strftime("%H:%M")) if recur == "daily" else \
            (nl.describe(fire) + "（" + fire.strftime("%m/%d %H:%M") + "）")

    # 沒講「提早多久」→ 先反問，存成待補，等他回答提早多久再真正建立（不給死預設）
    if not _m:
        _PENDING_REMINDER[getattr(ctx, "chat_id", None)] = {
            "time": fire.strftime("%H:%M"), "message": message,
            "repeat": _repeat, "channel": channel, "desc": _desc}
        return (f"好，{_desc} 的「{message}」 👌\n"
                "要**提早多久**提醒你？（例如「提早10分鐘」，或「準時就好」"
                + ("；也可以順便說用語音講還是傳訊息）" if not _ch_said else "）"))

    # 有講提早多久 → 直接建立
    _ch_txt = {"voice": "用語音講", "telegram": "傳訊息", "both": "Telegram＋語音"}[channel]
    _lead_txt = f"提早 {lead_minutes} 分鐘" if lead_minutes else "準時"
    try:
        reminder_manager.add_reminder(fire.strftime("%H:%M"), message, repeat=_repeat,
                                      lead_minutes=lead_minutes, channel=channel)
        return f"✅ 好的，{_desc} 的{message}，{_lead_txt}{_ch_txt}提醒你"
    except Exception:
        return "設定提醒時出了點狀況，換個說法再試一次好嗎 🙏（例如「明天下午3點 開會」）"


_WATCH_USAGE = (
    "用法:\n"
    "/watch list - 查看目前所有警示\n"
    "/watch crypto <coin_id> <above|below> <價格> [幣別=usd] - 加密貨幣價格警示\n"
    "  範例: /watch crypto bitcoin above 100000\n"
    "  coin_id 用 CoinGecko 的 id（如 bitcoin、ethereum、solana）\n"
    "/watch stock <代號> <above|below> <價格> - 股票價格警示\n"
    "  範例: /watch stock 2330.tw above 1200（代號用 stooq 格式，如 aapl.us、2330.tw）\n"
    "/watch flight <呼號> - 航班起飛/降落狀態（OpenSky Network，免金鑰，每15分鐘檢查一次）\n"
    "  範例: /watch flight CAL123（呼號格式依航空公司，如華航通常是 CAL+航班號）\n"
    "/watch earthquake <最小規模> - 地震速報\n"
    "/watch typhoon - 颱風警報\n"
    "（earthquake/typhoon 需先設定 config/cwa.json，"
    "免費註冊 https://opendata.cwa.gov.tw/）\n"
    "/watch checkin [HH:MM=23:30] - 深夜未回緊急聯絡人\n"
    "  （需先用 /emergency set <chat_id> [名字] 設定緊急聯絡人）\n"
    "/watch remove <編號> - 刪除警示"
)


def _format_watcher_line(w):
    if w["type"] == "crypto":
        return (
            f"#{w['id']} crypto: {w['coin_id']} "
            f"{'>=' if w['condition'] == 'above' else '<='} "
            f"{w['price']:,} {w.get('vs_currency', 'usd').upper()}"
        )
    if w["type"] == "stock":
        return (
            f"#{w['id']} stock: {w['symbol']} "
            f"{'>=' if w['condition'] == 'above' else '<='} {w['price']:,}"
        )
    if w["type"] == "earthquake":
        return f"#{w['id']} earthquake: 規模 >= {w.get('min_magnitude', 4.0)}"
    if w["type"] == "typhoon":
        return f"#{w['id']} typhoon: 任何新警報"
    if w["type"] == "flight":
        return f"#{w['id']} flight: {w['callsign']}"
    if w["type"] == "late_night_checkin":
        return f"#{w['id']} checkin: 若 {w.get('cutoff', '23:30')} 前沒有任何訊息就通知緊急聯絡人"
    return f"#{w['id']} {w['type']}"


def _cwa_not_configured_note():
    if os.path.exists("/Users/chenyouwei/Hermes_Brain/config/cwa.json"):
        return ""
    return (
        "\n⚠️ 尚未設定 config/cwa.json，暫時不會觸發。"
        "請至 https://opendata.cwa.gov.tw/ 免費註冊取得授權碼，"
        "複製 config/cwa.json.example 為 config/cwa.json 並填入授權碼"
    )


def cmd_watch(args, ctx):
    parts = args.split()

    if not parts or parts[0] == "list":
        watchers = watcher_manager.list_watchers()
        if not watchers:
            return "目前沒有設定任何警示。\n" + _WATCH_USAGE
        lines = ["🚨 目前的警示："]
        for w in watchers:
            lines.append(_format_watcher_line(w))
        return "\n".join(lines)

    if parts[0] == "crypto":
        if len(parts) < 4:
            return _WATCH_USAGE
        coin_id, condition, price_str = parts[1], parts[2], parts[3]
        vs_currency = parts[4] if len(parts) > 4 else "usd"
        if condition not in ("above", "below"):
            return "⚠️ 條件必須是 above 或 below"
        try:
            price = float(price_str)
        except ValueError:
            return "⚠️ 價格必須是數字"
        watcher_id = watcher_manager.add_watcher({
            "type": "crypto",
            "coin_id": coin_id,
            "condition": condition,
            "price": price,
            "vs_currency": vs_currency,
            "fired": False,
        })
        return f"✅ 已新增警示 #{watcher_id}：{coin_id} {condition} {price:,} {vs_currency.upper()}"

    if parts[0] == "stock":
        if len(parts) < 4:
            return _WATCH_USAGE
        symbol, condition, price_str = parts[1], parts[2], parts[3]
        if condition not in ("above", "below"):
            return "⚠️ 條件必須是 above 或 below"
        try:
            price = float(price_str)
        except ValueError:
            return "⚠️ 價格必須是數字"
        watcher_id = watcher_manager.add_watcher({
            "type": "stock",
            "symbol": symbol,
            "condition": condition,
            "price": price,
            "fired": False,
        })
        return f"✅ 已新增警示 #{watcher_id}：{symbol} {condition} {price:,}"

    if parts[0] == "flight":
        if len(parts) < 2:
            return _WATCH_USAGE
        callsign = parts[1]
        watcher_id = watcher_manager.add_watcher({
            "type": "flight",
            "callsign": callsign,
            "on_ground": None,
            "last_checked": 0,
        })
        return f"✅ 已新增警示 #{watcher_id}：追蹤航班 {callsign}（每15分鐘檢查一次）"

    if parts[0] == "earthquake":
        if len(parts) < 2:
            return _WATCH_USAGE
        try:
            min_magnitude = float(parts[1])
        except ValueError:
            return "⚠️ 規模必須是數字"
        watcher_id = watcher_manager.add_watcher({
            "type": "earthquake",
            "min_magnitude": min_magnitude,
            "last_report_id": None,
        })
        return f"✅ 已新增警示 #{watcher_id}：規模 >= {min_magnitude} 的地震{_cwa_not_configured_note()}"

    if parts[0] == "typhoon":
        watcher_id = watcher_manager.add_watcher({
            "type": "typhoon",
            "last_report_id": None,
        })
        return f"✅ 已新增警示 #{watcher_id}：颱風警報{_cwa_not_configured_note()}"

    if parts[0] == "checkin":
        cutoff = parts[1] if len(parts) > 1 else "23:30"
        watcher_id = watcher_manager.add_watcher({
            "type": "late_night_checkin",
            "cutoff": cutoff,
            "last_checked_date": None,
        })
        note = ""
        if not emergency_contact.get_contact():
            note = "\n⚠️ 尚未設定緊急聯絡人，請先用 /emergency set <chat_id> [名字] 設定"
        return f"✅ 已新增警示 #{watcher_id}：若 {cutoff} 前沒有任何訊息就通知緊急聯絡人{note}"

    if parts[0] in ("remove", "delete", "del"):
        if len(parts) < 2 or not parts[1].isdigit():
            return "用法: /watch remove <編號>（用 /watch list 查看編號）"
        if watcher_manager.remove_watcher(int(parts[1])):
            return f"✅ 已刪除警示 #{parts[1]}"
        return f"⚠️ 找不到警示 #{parts[1]}"

    return _WATCH_USAGE


def cmd_expense(args, ctx):
    """記帳：走新財務系統 8809 /expense（含預算提醒、簡轉繁、即時同步）。
    金額可在任何位置，例如「午餐 120」「120 餐飲 便當」都能懂。"""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", args or "")   # 容許千分位逗號、單一小數點
    if not m:
        # 沒金額＝不是記帳（多半是「列出/查詢今天消費」這類被誤路由的查詢，或不完整）
        # → 回 None 退回 chat，由它帶財務明細好好回答，不要回生硬的記帳提示
        return None
    try:
        amount = float(m.group(0).replace(",", ""))
    except ValueError:
        return "金額我沒看懂，跟我說「午餐花了120」這樣就好。"
    if amount <= 0 or amount > 1e12:
        return "這金額怪怪的，再確認一下？"
    rest = (args[:m.start()] + " " + args[m.end():]).split()
    # 過濾掉純數字/標點的殘渣 token（如「1.2.3」剩下的「.3」），避免變成怪分類
    rest = [r for r in rest if re.search(r"[^\d.,]", r)]
    category = rest[0] if rest else "其他"
    note = " ".join(rest[1:]) if len(rest) > 1 else ""
    try:
        req = urllib.request.Request("http://127.0.0.1:8809/expense",
                                     data=json.dumps({"amount": amount, "category": category,
                                                      "note": note}).encode(),
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=8).read())
        return d.get("text") or f"記好了，{category} {amount:g}元"
    except Exception as e:
        print(f"⚠️ [expense] {e}")
        return "記帳系統剛剛沒回應，等等再說一次"


def cmd_mood(args, ctx):
    parts = args.split(maxsplit=1)
    if not parts:
        return "用法: /mood <1-5> [備註]\n1=很差 2=不好 3=普通 4=不錯 5=很棒"

    try:
        score = int(parts[0])
    except ValueError:
        return "⚠️ 分數必須是 1-5 的數字"

    note = parts[1] if len(parts) > 1 else ""
    try:
        mood_tracker.add_mood(score, note)
    except ValueError:
        return "心情分數請給 1 到 5 的數字喔（1=很差、5=很好）"
    return f"✅ 已記錄今天的心情：{score}/5" + (f"（{note}）" if note else "")


def cmd_contact(args, ctx):
    parts = args.split(maxsplit=2)

    if not parts or parts[0] == "list":
        contacts = contact_tracker.list_contacts()
        if not contacts:
            return "目前沒有追蹤任何人。用法: /contact add <名字> [幾天提醒一次=7]"
        lines = ["👥 追蹤中的聯絡對象："]
        for c in contacts:
            lines.append(f"- {c['name']}（每 {c.get('remind_after_days', 7)} 天，上次聯絡 {c['last_contact']}）")
        return "\n".join(lines)

    if parts[0] == "add":
        if len(parts) < 2:
            return "用法: /contact add <名字> [幾天提醒一次=7]"
        name = parts[1]
        days = 7
        if len(parts) > 2:
            try:
                days = int(parts[2])
            except ValueError:
                return "⚠️ 天數必須是數字"
        contact_tracker.add_contact(name, days)
        return f"✅ 已追蹤「{name}」，超過 {days} 天沒聯絡會提醒你"

    if parts[0] == "touch":
        if len(parts) < 2:
            return "用法: /contact touch <名字>"
        if contact_tracker.touch_contact(parts[1]):
            return f"✅ 已將「{parts[1]}」的上次聯絡日更新為今天"
        return f"⚠️ 找不到「{parts[1]}」，先用 /contact add 新增"

    if parts[0] in ("remove", "delete", "del"):
        if len(parts) < 2:
            return "用法: /contact remove <名字>"
        if contact_tracker.remove_contact(parts[1]):
            return f"✅ 已移除「{parts[1]}」"
        return f"⚠️ 找不到「{parts[1]}」"

    return (
        "用法:\n"
        "/contact list - 查看追蹤中的聯絡對象\n"
        "/contact add <名字> [幾天提醒一次=7] - 新增追蹤\n"
        "/contact touch <名字> - 標記今天已聯絡\n"
        "/contact remove <名字> - 移除追蹤"
    )


def cmd_food(args, ctx):
    parts = args.split(maxsplit=2)

    if not parts or parts[0] == "list":
        items = food_tracker.list_items()
        if not items:
            return "目前沒有追蹤任何食材。用法: /food add <品項> <YYYY-MM-DD>"
        lines = ["🥬 追蹤中的食材："]
        for item in items:
            lines.append(f"- {item['name']}（到期日 {item['expiry']}）")
        return "\n".join(lines)

    if parts[0] == "add":
        if len(parts) < 3:
            return "用法: /food add <品項> <YYYY-MM-DD>\n範例: /food add 牛奶 2026-06-20"
        try:
            food_tracker.add_item(parts[1], parts[2])
        except ValueError:
            return "⚠️ 日期格式錯誤，請用 YYYY-MM-DD，例如 2026-06-20"
        return f"✅ 已新增食材：{parts[1]}（到期日 {parts[2]}）"

    if parts[0] in ("remove", "delete", "del"):
        if len(parts) < 2:
            return "用法: /food remove <品項>"
        if food_tracker.remove_item(parts[1]):
            return f"✅ 已移除「{parts[1]}」"
        return f"⚠️ 找不到「{parts[1]}」"

    return (
        "用法:\n"
        "/food list - 查看追蹤中的食材\n"
        "/food add <品項> <YYYY-MM-DD> - 新增食材與到期日\n"
        "/food remove <品項> - 移除食材"
    )


def cmd_budget(args, ctx):
    """用新的財務系統（發薪週期 + 存款先扣）回報預算，並算到下次發薪每天可花多少。"""
    import datetime as _dt
    import zoneinfo as _zi
    # 偵測「我只剩X 要活到發薪」→ 把本期還能花設成 X。
    # 嚴格條件：要有「剩」+數字 + 金錢語境(活到/發薪/月底/錢/塊/元/萬/千/花)，
    # 否則像「我剩3個朋友沒聯絡」會被誤判成設定餘額。
    _money_ctx = any(k in (args or "") for k in
                     ("活到", "發薪", "月底", "錢", "塊", "元", "萬", "千", "花", "預算"))
    if (any(k in (args or "") for k in ("剩", "只剩", "剩下"))
            and re.search(r"\d", args or "") and _money_ctx):
        m = re.search(r"(\d[\d,]*)\s*(萬|千|塊|元)?", args)
        if m:
            amt = float(m.group(1).replace(",", ""))
            if m.group(2) == "萬":
                amt *= 10000
            elif m.group(2) == "千":
                amt *= 1000
            try:
                d = _post_finance_op({"action": "set_remaining", "amount": amt})
                return "💰 " + (d.get("text") or f"好，本期還能花記成 {int(amt):,} 元")
            except Exception as e:
                print(f"⚠️ [budget set_remaining] {e}")
                return "我聽到了，但更新的時候連不上財務系統，等等再說一次 🙏"
    parts = args.split()
    # 設定花費上限：/budget set 數字 或 自然語言含數字
    if parts and parts[0] == "set":
        m = re.search(r"\d[\d,]*", args)
        if not m:
            return "想設每月花費上限的話，跟我說「花費上限設兩萬」這樣就好。"
        amt = float(m.group(0).replace(",", ""))
        try:
            _post_finance_op({"action": "set_spend_limit", "amount": amt})
            return f"✅ 已把每月花費上限設成 {int(amt):,} 元"
        except Exception:
            return "設定的時候連不上財務系統，等等再試 🙏"
    # 查預算現況
    try:
        d = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:8809/finance", timeout=12).read())["data"]
    except Exception as e:
        print(f"⚠️ [budget] {e}")
        return "現在查不到你的預算，等一下再問我一次 🙏"
    try:
        end = _dt.datetime.strptime(d["cycle_end"], "%Y-%m-%d").date()
        today = _dt.datetime.now(_zi.ZoneInfo("Asia/Taipei")).date()
        days = max(1, (end - today).days + 1)
    except Exception:
        days = 1
    try:
        rem = float(d.get("remaining") or 0)
    except (TypeError, ValueError):
        rem = 0
    daily = int(rem / days) if days else int(rem)
    lines = [
        f"💰 本期預算（{d.get('cycle_label', '')}，{d.get('payday', 15)}號發薪起算）",
        f"・本期可花：${d.get('spendable', 0):,}（收入{d.get('income', 0):,} − 固定{d.get('fixed', 0):,} − 自動存{d.get('auto_saved', 0):,}）",
        f"・已花：${d.get('month_var', 0):,}　·　還能花：${rem:,}",
        f"・距下次發薪還 {days} 天 → 每天約可花 ${daily:,}",
    ]
    return "\n".join(lines)


def _post_finance_op(payload):
    req = urllib.request.Request("http://127.0.0.1:8809/finance/op",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=8).read())


def cmd_checklist(args, ctx):
    parts = args.split(maxsplit=1)

    if not parts or parts[0] == "show":
        items = checklist_manager.get_items()
        if not items:
            return "出門清單是空的。用 /checklist add <項目> 新增。"
        lines = ["🎒 出門清單："] + [f"- {item}" for item in items]
        return "\n".join(lines)

    if parts[0] == "add":
        if len(parts) < 2:
            return "用法: /checklist add <項目>"
        items = checklist_manager.add_item(parts[1].strip())
        return "✅ 已加入。目前清單：\n" + "\n".join(f"- {item}" for item in items)

    if parts[0] in ("remove", "delete", "del"):
        if len(parts) < 2:
            return "用法: /checklist remove <項目>"
        if checklist_manager.remove_item(parts[1].strip()):
            return f"✅ 已移除「{parts[1].strip()}」"
        return f"⚠️ 清單裡沒有「{parts[1].strip()}」"

    return (
        "用法:\n"
        "/checklist - 顯示出門清單\n"
        "/checklist add <項目> - 新增項目\n"
        "/checklist remove <項目> - 移除項目"
    )


def cmd_sleep(args, ctx):
    parts = args.split()

    if not parts or parts[0] == "status":
        return sleep_tracker.status()

    if parts[0] == "start":
        return sleep_tracker.start_sleep()

    if parts[0] == "end":
        return sleep_tracker.end_sleep()

    if parts[0] == "target":
        if len(parts) < 2:
            return "用法: /sleep target HH:MM"
        return sleep_tracker.set_target_bedtime(parts[1])

    return (
        "用法:\n"
        "/sleep start - 記錄現在為就寢時間\n"
        "/sleep end - 記錄現在為起床時間\n"
        "/sleep status - 查看最近一筆紀錄與目標就寢時間\n"
        "/sleep target HH:MM - 設定目標就寢時間（預設 23:00）\n\n"
        "搭配 /remind addskill HH:MM daily bedtime_check 達成「睡眠守門人」：\n"
        "若到了目標就寢時間還沒 /sleep start，會自動提醒你"
    )


def cmd_emergency(args, ctx):
    parts = args.split(maxsplit=2)

    if not parts or parts[0] == "status":
        contact = emergency_contact.get_contact()
        if not contact:
            return "目前沒有設定緊急聯絡人。用法: /emergency set <chat_id> [名字]"
        name = contact.get("name") or "(未命名)"
        return f"👤 緊急聯絡人：{name}（chat_id={contact['chat_id']}）"

    if parts[0] == "set":
        if len(parts) < 2:
            return "用法: /emergency set <chat_id> [名字]"
        try:
            chat_id = int(parts[1])
        except ValueError:
            return "⚠️ chat_id 必須是數字"
        name = parts[2] if len(parts) > 2 else ""
        emergency_contact.set_contact(chat_id, name)
        return f"✅ 已設定緊急聯絡人：{name or '(未命名)'}（chat_id={chat_id}）"

    if parts[0] in ("remove", "delete", "del"):
        if emergency_contact.remove_contact():
            return "✅ 已移除緊急聯絡人"
        return "目前沒有設定緊急聯絡人"

    return (
        "用法:\n"
        "/emergency set <chat_id> [名字] - 設定緊急聯絡人\n"
        "/emergency status - 查看目前設定\n"
        "/emergency remove - 移除設定\n\n"
        "chat_id 取得方式：請該聯絡人傳一句話給這個 Bot，"
        "Bot 會回覆「你的 user_id 是: ...」，那個數字就是 chat_id\n"
        "（私聊時 chat_id 與 user_id 相同）。設定後用 "
        "/watch checkin [HH:MM=23:30] 開啟深夜未回警示"
    )


def cmd_quiz(args, ctx):
    topic = args.strip()
    try:
        question = quiz_manager.new_question(topic)
        return f"❓ {question}\n\n（用 /answer <你的回答> 來回答）"
    except Exception as e:
        print(f"⚠️ [quiz] {e}")
        return "出題的時候卡了一下，等等再跟我說一次 🙏"


def cmd_answer(args, ctx):
    answer = args.strip()
    if not answer:
        return "想回答的話直接打答案就好。"
    try:
        return quiz_manager.grade_answer(answer)
    except Exception as e:
        print(f"⚠️ [answer] {e}")
        return "對答案時出了點問題，再試一次？"


def cmd_research(args, ctx):
    query = args.strip()
    if not query:
        return "想查什麼跟我說，例如「幫我研究電動車市場」。"
    try:
        return ctx.research.deep_search(query)
    except Exception as e:
        print(f"⚠️ [research] {e}")
        return "查資料時連不上，等一下再問我一次 🙏"


def cmd_plaud(args, ctx):
    audio_path = args.strip()
    if not audio_path:
        return (
            "用法: /plaud <音檔路徑>\n"
            "或直接傳送語音訊息／音訊檔給我，我會自動轉錄、摘要，並回傳完整報告檔。"
        )
    if not os.path.exists(audio_path):
        return f"⚠️ 找不到檔案: {audio_path}"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        os.makedirs(PLAUD_REPORTS_DIR, exist_ok=True)
        report_path = os.path.join(PLAUD_REPORTS_DIR, f"{timestamp}_report.md")
        report = PlaudIntegrator().run_pipeline(audio_path, report_path)
    except Exception as e:
        print(f"⚠️ [plaud] {e}")
        return "處理這個音檔時出了點問題，可能格式不支援或檔案壞了 🙏"
    try:
        ctx.memory.remember(
            f"Plaud 會議報告 ({timestamp}, {os.path.basename(audio_path)}):\n{report[:1000]}",
            category="plaud_report", importance=3)
    except Exception:
        pass
    preview = report if len(report) <= 3500 else report[:3500] + "\n…（完整內容見報告檔）"
    return f"✅ 報告已產生: {report_path}\n\n{preview}"


def cmd_model(args, ctx):
    model_id = args.strip()
    current = embodied_config.get_gemini_model()

    if not model_id:
        return (
            f"🧠 目前使用的模型：{current}\n\n"
            "用法: /model <model_id> 切換模型\n"
            "範例: /model gemini-3.1-flash-lite\n\n"
            "推薦選項：\n"
            "- gemini-2.5-flash-lite（輕量快速）\n"
            "- gemini-2.5-flash（更聰明一點）\n"
            "- gemini-3.1-flash-lite（新一代輕量）\n"
            "- gemini-3.1-pro-preview（最強，較貴較慢）\n\n"
            "輸入 /models 查看完整可用清單"
        )

    try:
        available = _list_available_models()
    except Exception as e:
        print(f"⚠️ [switch_model] {e}")
        return "現在抓不到可用模型清單，等一下再試 🙏"

    if model_id not in available:
        close = [m for m in available if model_id.lower() in m.lower() or m.lower() in model_id.lower()]
        suggestion = ""
        if close:
            suggestion = "\n\n可能你要找的是：\n" + "\n".join(f"- {m}" for m in close[:8])
        return f"⚠️ 「{model_id}」不是這組 API key 可用的模型名稱。{suggestion}\n\n輸入 /models 查看完整清單"

    embodied_config.set_gemini_model(model_id)
    return (
        f"✅ 已切換模型：{current} → {model_id}\n"
        "（Telegram / StackChan 語音 / /research 立即生效，不用重啟）"
    )


def cmd_models(args, ctx):
    try:
        names = _list_available_models()
    except Exception as e:
        print(f"⚠️ [models] {e}")
        return "現在抓不到模型清單，等一下再試 🙏"

    chat_models = [n for n in names if not any(k in n for k in _MODEL_EXCLUDE_KEYWORDS)]
    return "📋 可用聊天模型：\n" + "\n".join(f"- {m}" for m in chat_models)


def cmd_sync(args, ctx):
    ctx.skill_ctx.send_command("SYNC_REQUEST")
    return "🔄 已要求 StackChan 補發離線緩衝事件。"


def cmd_dev(args, ctx):
    from modules.remote.dev_runner import run_dev_task, restart_bot

    if not args or not args.strip():
        return (
            "🔧 /dev 功能描述\n"
            "例：/dev 加一個 /joke 指令，每次回傳一個隨機笑話\n\n"
            "Claude Code 會直接修改 Hermes Brain 程式碼並重啟 bot 上線。\n"
            "⚠️ 只限 Hermes_Brain 目錄內，不動電腦其他地方。"
        )

    task = args.strip()
    chat_id = ctx.chat_id
    handler = ctx.handler

    handler.send_message(
        chat_id,
        f"🔧 任務已收到，交給 Claude Code 執行中...\n\n📋 {task}\n\n（最長等 10 分鐘，每 30 秒會更新進度）"
    )

    def on_progress(msg):
        handler.send_message(chat_id, msg)

    def on_done(success, output):
        summary = output[-2000:] if len(output) > 2000 else output
        if success:
            handler.send_message(
                chat_id,
                f"✅ 功能實作完成！\n\n{summary}\n\n🔄 重啟 bot 載入新功能..."
            )
            import time as _time; _time.sleep(2)
            restart_bot()
        else:
            handler.send_message(chat_id, f"❌ Claude Code 執行失敗\n\n{summary}")

    run_dev_task(task, on_progress, on_done)
    return None


def cmd_agent(args, ctx):
    """把需要『真的動手』的任務交給完整 hermes-agent 深度代理（寫程式/查資料/操作電腦）。
    Telegram 端非同步執行（不卡住 bot），完成後回報。"""
    task = args.strip()
    if not task:
        return ("用法: /agent <任務>\n"
                "把需要動手的任務交給深度代理，它會自己多步驟完成。\n"
                "例：/agent 幫我研究 2026 年最值得學的 3 個 AI 技能並整理成清單")
    from .hermes_agent_runner import run_agent_task

    handler = getattr(ctx, "handler", None)
    chat_id = getattr(ctx, "chat_id", None)
    if handler and chat_id:
        import threading

        def _run():
            try:
                result = run_agent_task(task)
            except Exception as e:
                result = f"⚠️ 深度代理失敗：{e}"
            handler.send_message(chat_id, f"🧠 深度代理完成：\n\n{result}")
            try:
                ctx.memory.remember(
                    f"深度代理任務: {task}\n結果摘要: {result[:500]}",
                    category="agent_task", importance=3,
                )
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
        return "🧠 收到，已交給深度代理執行中（完成後回報，期間你可以繼續做別的）…"

    # 語音 / 無非同步管道：同步跑、縮短逾時
    return run_agent_task(task, timeout=180)


def cmd_game(args, ctx):
    """StackChan 當主持人，跟現場朋友玩多人遊戲（目前：問答）。"""
    parts = args.split()
    if not parts:
        return ("🎮 用法: /game [遊戲] 玩家1 玩家2 …\n"
                "例: /game trivia 小明 阿華 小美\n"
                "目前有：trivia（多人現場問答，StackChan 出題、轉頭問人、聽答案、計分）")
    if parts[0] in ("trivia", "quiz", "問答"):
        players = parts[1:]
    else:
        players = parts
    if not players:
        return "至少給一個玩家名字，例: /game trivia 小明 阿華"

    from modules.embodied import notify
    if not notify.robot_present():
        return ("🎮 這個遊戲要 StackChan 在場主持（用講的跟大家互動）。\n"
                "等實體到貨連上後就能玩！想先看流程可在電腦跑：\n"
                "  ./scripts/play_game.py trivia " + " ".join(players))

    import threading

    def _run():
        from modules.games import trivia
        from modules.games.host import RobotIO
        try:
            result = trivia.play(ctx.gemini, players, io=RobotIO(gemini=ctx.gemini))
            if getattr(ctx, "handler", None) and getattr(ctx, "chat_id", None):
                board = "、".join(f"{k} {v}分" for k, v in result.items())
                ctx.handler.send_message(ctx.chat_id, f"🏆 遊戲結束！比分：{board}")
        except Exception as e:
            if getattr(ctx, "handler", None) and getattr(ctx, "chat_id", None):
                ctx.handler.send_message(ctx.chat_id, f"⚠️ 遊戲出錯: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return f"🎮 開始問答！StackChan 正在主持，玩家：{'、'.join(players)}。大家看著它、用講的搶答吧！"


COMMAND_TABLE = {
    "start": cmd_help,
    "agent": cmd_agent,
    "game": cmd_game,
    "help": cmd_help,
    "status": cmd_status,
    "say": cmd_say,
    "weather": cmd_weather,
    "convert": cmd_convert,
    "remember": cmd_remember,
    "recall": cmd_recall,
    "research": cmd_research,
    "plaud": cmd_plaud,
    "model": cmd_model,
    "models": cmd_models,
    "sync": cmd_sync,
    "led": cmd_led,
    "email": cmd_email,
    "remind": cmd_remind,
    "watch": cmd_watch,
    "expense": cmd_expense,
    "budget": cmd_budget,
    "mood": cmd_mood,
    "checklist": cmd_checklist,
    "food": cmd_food,
    "contact": cmd_contact,
    "sleep": cmd_sleep,
    "emergency": cmd_emergency,
    "quiz": cmd_quiz,
    "answer": cmd_answer,
    "dev": cmd_dev,
}

for _name, _intent in _EXPRESSION_COMMANDS.items():
    COMMAND_TABLE[_name] = cmd_expression(_intent)


# 意圖路由描述表：每個可路由的功能都要在這裡登記一行自然語言描述。
# 格式："command_name": "中文描述 [參數格式]: 例子或觸發時機"
# ⚠️ 新增功能時必須同步新增一筆，否則自然語言無法觸發該功能。
COMMAND_DESCRIPTIONS = {
    # 生活工具
    "weather":   "天氣查詢 [城市(可省略)]: 問天氣、氣溫、會不會下雨、空氣品質",
    "convert":   "匯率換算 [金額 來源貨幣 目標貨幣]: 例「100美金換台幣」",
    "research":  "搜尋資料 [主題]: 上網查某個主題、新聞、知識",
    "email":     "郵件摘要: 看信箱有什麼新郵件、有無包裹通知",
    # 財務
    "expense":   "記帳 [金額 描述]: 記錄消費，例「花了150買午餐」「剛買咖啡80」",
    "budget":    "預算查詢: 這週/這月還剩多少預算可以花",
    # 健康與生活節奏
    "mood":      "情緒記錄 [描述]: 記錄心情，例「今天很開心」「有點焦慮」",
    "sleep":     "睡眠記錄 [start/end]: 說「要去睡覺」或「起床了」",
    "food":      "食材管理 [add 食材 天數 / list / expiring]: 例「加了雞蛋還有5天」「快過期的食材有什麼」",
    # 記憶與任務
    "remember":  "記住某事 [內容]: 明確叫我記住某件事，例「幫我記住瓦斯費月底要繳」",
    "recall":    "翻記憶 [關鍵字]: 只有在使用者明確叫你『查記憶/搜尋你記過什麼』時才用。一般對話式問題（我叫什麼、你記得我嗎、我老婆生日）一律用 chat，不要用這個",
    "checklist": "待辦清單 [add 項目 / list / done 項目 / clear]: 管理出門清單或任務",
    "contact":   "聯絡人追蹤 [list / add 名字 / check]: 查久未聯絡的朋友",
    "remind":    "提醒設定 [add/list/remove]: 設定定時提醒、每日/每週提醒",
    # 學習
    "quiz":      "出題考我: 讓 Hermes 出一道題目",
    "answer":    "回答題目 [答案]: 回答上一題",
    # StackChan 肢體語言（到貨後生效）
    "say":       "讓 StackChan 說話 [內容]: 叫機器人開口說某句話",
    "happy":     "StackChan 開心表情: 說「開心一下」「笑一個」",
    "sad":       "StackChan 難過表情: 說「難過一下」",
    "angry":     "StackChan 生氣表情: 說「生氣一下」",
    "think":     "StackChan 思考表情: 說「想一想」",
    "nod":       "StackChan 點頭: 說「點頭」",
    "shake":     "StackChan 搖頭: 說「搖頭」",
    # 系統
    "agent":     "深度代理 [任務]: 需要真的動手的任務——寫程式、跑指令、上網深入研究、操作電腦、多步驟自動完成。例「幫我研究X並整理」「寫一個腳本做Y」",
    "game":      "玩遊戲 [玩家名單]: 跟現場朋友玩多人問答，StackChan 當主持人出題計分。例「來玩問答 小明 阿華」",
    "dev":       "開發新功能 [描述]: 要求 Claude Code 幫 Hermes 新增/修改/實作功能",
    "watch":     "監控警示 [crypto/stock/earthquake/typhoon/flight/checkin]: 設定價格或災害警示",
    "emergency": "緊急聯絡人 [set/get/remove]: 設定深夜未回應時通知的聯絡人",
}
