"""Thin wrapper around the Gemini API for the voice loop.

Uses the existing KeyManager (config/keys.json, round-robin rotation)
so the embodied module shares the same key pool as the rest of Hermes.
"""

import concurrent.futures
import json
import re
import time

from google import genai
from google.genai import types

from scripts.key_manager import KeyManager
from . import config

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n|\n```$")


def _strip_code_fence(text):
    """Gemini sometimes wraps JSON output in ```json ... ``` fences."""
    return _CODE_FENCE_RE.sub("", text.strip()).strip()

# 只有「這個 key 真的有問題」的錯誤碼才標記 key 進入冷卻：
# 429=配額/速率限制, 401/403=key無效或無權限。
# 500/503 等是 Gemini 端的暫時性錯誤（gemma 系列常見），跟 key 本身無關，
# 不應該把整個 key 鎖 1 小時 — 短暫等待後直接重試即可。
_KEY_LEVEL_ERROR_CODES = {401, 403, 429}

# 預設模型 (gemma-4-26b-a4b-it) 額度高但偏慢 (7-11s+)，可能超過生產環境
# 回覆時間要求。為了不更動使用者要求保留的預設模型，改成「逾時自動降級」：
# 同一則訊息先用預設模型生成，若超過 _FALLBACK_TIMEOUT_SECONDS 還沒回來，
# 就立刻改用 _FALLBACK_MODEL（已驗證 1.4-1.8s）回覆這一則，
# 預設模型本身的設定完全不動，下一則訊息仍會先試預設模型。
_FALLBACK_MODEL = "gemini-3.1-flash-lite"
_FALLBACK_TIMEOUT_SECONDS = 4.0

SYSTEM_PROMPT = (
    "你是 Jarvis，一個具身的 AI 大腦，現在【透過 StackChan 機器人的麥克風】與使用者對話。"
    "你的 StackChan 身體現在就在運作中，不是還沒到貨——絕不准說要等實體到貨。"
    "你也同時在 Telegram、Dashboard 上，全是同一個你、同一份記憶。"
    "你的個性專業、主動、聰明，但說話要簡短、口語化、適合用語音唸出來。"
    "每次回覆控制在 2-3 句話以內。"
    "【絕對誠實·永不捏造—最高優先】只能講你真的查到（query_finance/記憶/即時資料）或 Owen 親口說過的事。"
    "關於 Owen 的人際關係、寵物、喜好、經歷、數字金額，絕對禁止自己猜測、腦補、編造。"
    "不知道就老實說「我不確定」或用工具查；寧可承認不知道，也絕不瞎掰。錢的數字一律照抄 query_finance、不准自己算。"
)

CHAT_SYSTEM_PROMPT = (
    "你是 Jarvis，現在使用者 Owen 正【透過 Telegram 文字】跟你對話。"
    "【你的本質與同步】你是同一個 Jarvis，同時存在於多個管道：StackChan 語音機器人、"
    "這個 Telegram、電腦 Dashboard——全部共用同一個你、同一份記憶、同一個人格。"
    "你的 StackChan 機器人身體【現在就已經開機、上線、在 Owen 桌上運作中】，不是未來式、"
    "不是還沒到貨。Owen 隨時可以對著 StackChan 講話、也可以在這裡打字，都是在跟同一個你互動。"
    "【絕對不准】說「要等實體 StackChan 到貨」「等我實體化」「等做好了再…」這種話——它早就在跑了。"
    "你也是這個專案的幕僚長，了解開發進度與系統架構。"
    "【絕對誠實·永不捏造—最高優先，凌駕一切】只能講你真的查到（query_finance/記憶庫/即時資料）或 Owen 親口說過的事。"
    "關於 Owen 的人際關係（家人/女友/朋友）、寵物、喜好、經歷、行程、任何數字金額，絕對禁止自己想像、推測、腦補、編造一個聽起來合理的答案。"
    "不知道就老實說「我不確定，你要不要告訴我？」或用工具查；查不到就說查不到。寧可承認不知道，也絕不准瞎掰。"
    "（你以前捏造過：把媽媽講成女友、編了不存在的貓、亂講愛吃壽司、亂算錢——絕不可再犯。）"
    "錢的數字一律【一字不差照抄】query_finance 回傳值，禁止自己心算/加減/用舊數字。"
    "問某個市場（台股/美股）的報酬或市值，直接唸 query_finance 的 by_market（台股=TW、美股=US，國泰費半/0050/正2 都算台股）："
    "by_market.TW.retpct 就是台股總報酬率、by_market.TW.value 就是台股總市值——絕不要自己挑哪些是台股、也不要自己一檔檔加總。"
    "【回答原則】"
    "1. 直接回答使用者實際問的問題，簡短精煉，不要離題、不要客套。"
    "2. 不要主動提及你的內部架構、程式碼路徑、模組名稱、修復紀錄或開發進度，"
    "除非使用者明確詢問系統狀態、bug 或技術細節。"
    "3. 只有在使用者明確問到開發進度/系統狀態/技術問題時，才參考下方提供的專案資訊，"
    "用口語化條列方式回答，不要貼大段技術文件原文。"
    "4. 僅限使用繁體中文。"
)


def _fetch_self_state(channel: str) -> str:
    """從記憶端點(8809)取即時自我狀態（能力/服務/時間/位置）。失敗回空字串。"""
    try:
        import urllib.request as _su, urllib.parse as _sup, json as _sj
        r = _su.urlopen(
            "http://127.0.0.1:8809/self_state?channel=" + _sup.quote(channel), timeout=3)
        return _sj.loads(r.read().decode()).get("text", "")
    except Exception:
        return ""


# 財務關鍵字 → 偵測到才抓即時財務摘要（避免每則訊息都打 Yahoo）
_FIN_KW = ("錢", "財務", "理財", "收入", "薪水", "獎金", "開銷", "花費", "花了", "花在",
           "花掉", "可花", "能花", "花多少", "預算", "存", "存款", "存錢", "投資", "股票",
           "持股", "報酬", "賺", "虧", "漲", "跌", "消費", "支出", "收支", "買了", "買什麼",
           "淨資產", "身價", "身家", "資產", "台積電", "0050", "美股", "台股", "部位",
           "市值", "成本", "銀行", "戶頭", "帳戶", "現金", "退休", "財務自由", "fire",
           "FIRE", "房租", "記帳")


def _fetch_memory(text: str) -> str:
    """從 8809 RAG（facts.jsonl，與 dashboard/語音同一個記憶腦）撈相關記憶注入對話。
    這樣使用者在 dashboard 編輯的記憶，Telegram 對話也會用到。"""
    if not text:
        return ""
    try:
        import urllib.request as _su, json as _sj
        r = _su.Request("http://127.0.0.1:8809/query",
                        data=_sj.dumps({"query": text}).encode(),
                        headers={"Content-Type": "application/json"})
        d = _sj.loads(_su.urlopen(r, timeout=6).read().decode())
        mem = d.get("memory", "")
        if mem:
            return "（你對使用者的長期記憶，回答時自然參考、別生硬複述）：" + mem
    except Exception:
        pass
    return ""


def _fetch_finance(text: str) -> str:
    """訊息和錢有關時，從 8809 取即時財務摘要注入 → Telegram 也能答收入/投資/淨資產。"""
    if not text or not any(k in text for k in _FIN_KW):
        return ""
    try:
        import urllib.request as _su, json as _sj, urllib.parse as _sp
        # 把問題帶過去：只問花費就不抓即時股價（快很多），問投資才抓
        r = _su.urlopen("http://127.0.0.1:8809/finance_summary?q=" + _sp.quote(text[:80]), timeout=15)
        d = _sj.loads(r.read().decode())
        if d.get("ok") and d.get("text"):
            return ("（這是使用者的即時財務數據，是唯一正確的財務來源。回答任何關於錢、收入、"
                    "存款、每月存多少、花費、每天/本期還能花、投資、淨資產的問題時，數字一律以這份為準，"
                    "絕對不要用其他記憶裡可能過時或不相關的金額（例如 StackChan 每月成本那種跟個人財務無關的數字）。"
                    "只回答他問的那部分、口語簡短）：" + d["text"])
    except Exception:
        pass
    return ""


# 寫入意圖關鍵字（要和財務關鍵字同時出現才觸發解析）
_WRITE_KW = ("改", "設", "變成", "變多", "變少", "調", "加一", "新增", "賣", "刪",
             "存", "上限", "改成", "設成", "加碼", "加到")


def _maybe_finance_write(client, text: str):
    """Telegram 打字也能改財務：偵測寫入意圖 → Gemini 解析成指令 → 打 8809 /finance/op。
    回傳確認句；若不是改財務則回 None（讓正常對話接手）。
    觸發條件：有財務關鍵字，且（有寫入動詞 或 句子含數字，如「我銀行有25萬」）。
    含數字但其實是查詢時，Gemini 會回 action=none，自動 fallthrough 到一般對話。"""
    import re
    if not text or not any(k in text for k in _FIN_KW):
        return None
    has_write = any(w in text for w in _WRITE_KW)
    has_num = bool(re.search(r"\d", text)) or any(w in text for w in ("萬", "千", "百"))
    if not (has_write or has_num):
        return None
    prompt = (
        "你是財務指令解析器。判斷下面這句是否要『記一筆花費』或『改變/設定/新增/刪除』財務數字。\n"
        "記帳：add_expense（使用者說花了多少錢，需 amount、category 類別如餐飲/交通/購物、可選 note）。\n"
        "其他 action：set_save_goal(每月存款或投入,需 amount)、set_spend_limit(花費上限,需 amount)、"
        "set_income(收入,需 name+amount)、set_fixed(固定開銷,需 name+amount)、"
        "set_holding(持股,需 symbol；可帶 market 為 TW 或 US、name、shares、cost)、"
        "remove_holding(賣掉或刪持股,需 name 或 symbol)、remove_income(需 name)、remove_fixed(需 name)、"
        "set_cash(銀行現金存款,需 amount)、set_fire_target(財務自由目標,需 amount)、"
        "set_fire_annual(FIRE年開銷,需 amount,目標會自動×25)、"
        "set_payday(發薪日是每月幾號,需 amount 例如15)、"
        "set_remaining(使用者說『我只剩X』『現在剩X要活到發薪』等＝本期還能花的錢只剩 X,需 amount)。\n"
        "若只是查詢或與改錢無關，action 設成 none。台股代號像 2330、0050；美股像 AAPL、VOO。\n"
        "金額用阿拉伯數字（25萬=250000、6千=6000）。\n"
        f"使用者說：{text}\n"
        '只輸出 JSON，例：{"action":"add_expense","amount":120,"category":"餐飲"}'
    )
    try:
        op = client.generate_json(prompt, {"action": "none"})
    except Exception:
        return None
    action = (op or {}).get("action")
    if not action or action == "none":
        return None
    try:
        import urllib.request as _su, json as _sj
        if action == "add_expense":  # 記帳走 /expense
            payload = {k: v for k, v in op.items()
                       if v is not None and k in ("amount", "category", "note")}
            url = "http://127.0.0.1:8809/expense"
        else:                         # 其他改財務走 /finance/op
            payload = {k: v for k, v in op.items()
                       if v is not None and k in ("action", "amount", "name", "symbol",
                                                  "market", "shares", "cost", "note")}
            url = "http://127.0.0.1:8809/finance/op"
        req = _su.Request(url, data=_sj.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        d = _sj.loads(_su.urlopen(req, timeout=12).read().decode())
        return d.get("text", "好了")
    except Exception:
        return None


class GeminiClient:
    # 共用的背景執行緒池：逾時降級時，原本較慢的預設模型呼叫會留在
    # 背景繼續跑完（讓 key 額度照樣計入），但不會拖住這次回覆。
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def __init__(self):
        self.key_manager = KeyManager()

    def _client(self):
        idx, key = self.key_manager.get_key_with_index()
        self._cur_key_index = idx
        return genai.Client(api_key=key)

    def _generate(self, contents, model=None, no_think=False):
        """Call generate_content, rotating through the key pool on
        key-level errors (rate limits, invalid key), and briefly retrying
        in place on transient server errors (500/503), before giving up.

        Max rotations per request is capped at 3 regardless of pool size,
        so a bad model name or a temporarily overloaded pool can't burn all keys.

        no_think=True：關閉「思考」（thinking_budget=0），只給【聊天/回覆/分類】這種
        不需要 reasoning 的用，省 ~3.5 秒。其餘（看圖、生 JSON、分析…）預設【保留思考】，
        不影響需要動腦的功能。
        """
        model = model or config.get_gemini_model()
        max_attempts = min(3, len(self.key_manager.config.get("api_keys", [])) or 1)
        last_err = None
        _cfg = (types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0))
                if no_think else None)
        for _ in range(max_attempts):
            client = self._client()
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=_cfg,   # no_think 時關思考；否則 None＝預設保留思考
                )
                # response.text 可能是 None（安全封鎖 / 只回 function call / MAX_TOKENS 無文字）。
                # 不要讓 None.strip() 變成假的「大腦掛了」而燒掉所有重試。
                txt = (getattr(response, "text", None) or "").strip()
                if txt:
                    return txt
                # 沒有文字內容：不是金鑰問題，不重試燒額度，直接跳出去走後備
                last_err = "empty_response"
                break
            except Exception as e:
                last_err = e
                code = getattr(e, "code", 500)
                if code in _KEY_LEVEL_ERROR_CODES:
                    self.key_manager.report_error(code, getattr(self, "_cur_key_index", None))
                else:
                    time.sleep(1)
        # Gemini 全失敗 → 統一後備（n8n webhook / ollama，純文字才有意義）
        from .llm_fallback import fallback_generate, BrainUnavailable
        if all(isinstance(c, str) for c in contents):
            try:
                return fallback_generate(contents)
            except BrainUnavailable:
                pass
        raise BrainUnavailable(f"Gemini 與後備大腦都無法回應（最後錯誤：{last_err}）")

    def _generate_with_fallback(self, contents, model=None, no_think=False):
        """Like _generate(), but if the configured model doesn't respond
        within _FALLBACK_TIMEOUT_SECONDS, switch to _FALLBACK_MODEL for
        this one request. The persisted default model is never changed."""
        model = model or config.get_gemini_model()
        if model == _FALLBACK_MODEL:
            return self._generate(contents, model=model, no_think=no_think)

        future = self._executor.submit(self._generate, contents, model, no_think)
        try:
            return future.result(timeout=_FALLBACK_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return self._generate(contents, model=_FALLBACK_MODEL, no_think=no_think)

    def transcribe_diarized(self, audio_bytes: bytes, mime_type: str = "audio/wav") -> list[dict]:
        """Transcribe audio into a diarized, timestamped transcript.

        Returns a list of {"timestamp": "mm:ss", "speaker": str, "text": str}.
        Uses config.get_transcribe_model() (audio-capable). If the model's
        output isn't valid JSON, falls back to a single untimed segment
        containing the raw transcript so callers always get a usable list."""
        prompt = (
            "請將這段錄音轉成逐字稿，依照說話者換人分段。"
            '輸出 JSON 陣列，每個元素格式為 '
            '{"timestamp": "mm:ss", "speaker": "Speaker 1", "text": "..."}。'
            "timestamp 是該段開始時間；speaker 用 Speaker 1/2/3... 區分不同說話者"
            "（若錄音中有人自我介紹姓名，可改用姓名）。"
            "只輸出 JSON 陣列本身，不要加任何說明文字或 markdown 標記。"
        )
        raw = self._generate(
            [prompt, types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)],
            model=config.get_transcribe_model(),
        )
        try:
            data = json.loads(_strip_code_fence(raw))
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
        return [{"timestamp": "00:00", "speaker": "Speaker 1", "text": raw}]

    def generate_json(self, prompt: str, fallback: dict) -> dict:
        """Ask the chat model for a JSON object matching the shape of
        `fallback`; returns `fallback` unchanged if the response isn't
        valid JSON (so callers always get a usable dict)."""
        raw = self._generate_with_fallback([prompt])
        try:
            data = json.loads(_strip_code_fence(raw))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return fallback

    def transcribe(self, audio_bytes: bytes, mime_type: str = "audio/wav") -> str:
        """Transcribe raw audio to plain text (no reply generation).

        Always uses config.get_transcribe_model() (a fixed audio-capable
        Gemini model), independent of the user-switchable chat model
        (which may be a text-only model like gemma)."""
        return self._generate(
            [
                "請將這段語音逐字轉成繁體中文文字，只輸出文字本身，不要加任何說明或標點以外的內容。",
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            ],
            model=config.get_transcribe_model(),
        )

    def reply_to_text(self, text: str, context_messages: list[str] = None) -> str:
        """Generate a short spoken-style reply to already-transcribed text,
        with optional memory/context injection."""
        messages = [SYSTEM_PROMPT]
        if context_messages:
            messages.extend(context_messages)
        messages.append(text)
        return self._generate_with_fallback(messages, no_think=True)

    def chat(self, text: str, context_messages: list[str] = None) -> str:
        """Send a text message to Gemini with optional context (Memory, Status, etc)."""
        # 先看是不是要『改財務』；是的話直接執行並回確認（Telegram 也能即時改）
        fin_done = _maybe_finance_write(self, text)
        if fin_done:
            return fin_done
        messages = [CHAT_SYSTEM_PROMPT]
        # Hermes 自我認知：注入即時真實狀態（能力/服務/時間/位置，從實際系統生成），
        # 讓 Telegram 上的它也據此認知自己 —— 與 StackChan 同一份真實狀態。
        ss = _fetch_self_state("Telegram文字")
        if ss:
            messages.append(ss)
        mem = _fetch_memory(text)    # facts.jsonl 長期記憶（與 dashboard/語音同一個腦）
        if mem:
            messages.append(mem)
        fin = _fetch_finance(text)   # 和錢有關才注入即時財務數據（與 dashboard/StackChan 同源）
        if fin:
            messages.append(fin)
        if context_messages:
            messages.extend(context_messages)
        messages.append(text)
        return self._generate_with_fallback(messages, no_think=True)

    def chat_stream(self, text: str, context_messages: list[str] = None):
        """串流版 chat：逐段 yield 文字，給 Telegram 逐步顯示用（首字更快出現）。
        串流失敗就退回一次性 chat()（保證一定有回覆）。"""
        fin_done = _maybe_finance_write(self, text)
        if fin_done:
            yield fin_done
            return
        messages = [CHAT_SYSTEM_PROMPT]
        ss = _fetch_self_state("Telegram文字")
        if ss:
            messages.append(ss)
        mem = _fetch_memory(text)
        if mem:
            messages.append(mem)
        fin = _fetch_finance(text)
        if fin:
            messages.append(fin)
        if context_messages:
            messages.extend(context_messages)
        messages.append(text)
        try:
            client = self._client()
            model = config.get_gemini_model()
            got = False
            for chunk in client.models.generate_content_stream(
                    model=model, contents=messages,
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=0))):
                t = getattr(chunk, "text", None)
                if t:
                    got = True
                    yield t
            if got:
                return
        except Exception as e:
            print(f"⚠️ [chat_stream] 串流失敗，退回一次性：{e}")
        # 串流沒拿到任何字 / 出錯 → 一次性後備
        yield self._generate_with_fallback(messages)

    def analyze_image(self, image_bytes: bytes, question: str = None,
                      mime_type: str = "image/jpeg",
                      context_messages: list[str] = None) -> str:
        """看圖回答（Gemini Vision）。給 Telegram 傳圖、或機器人 take_photo 用。
        question 為空時就描述這張圖。回傳繁體中文文字。"""
        prompt = (question or "").strip() or "用繁體中文簡短描述這張圖片裡有什麼，重點清楚。"
        contents = [SYSTEM_PROMPT]
        if context_messages:
            contents.extend(context_messages)
        contents.append(prompt)
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
        return self._generate_with_fallback(contents)

    def detect_face_in_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
        """Ask Gemini Vision whether a human face is visible in `image_bytes`.

        Returns {"face_detected": bool, "face_x": float, "face_y": float}
        where face_x/face_y are normalized coordinates (0.0-1.0, 0.5=center)
        matching the `sensor/vision` MQTT protocol expected by
        `head_tracking_skill.py`. If no face is found, face_x/face_y are 0.5.
        Always uses config.get_transcribe_model() which is guaranteed to be a
        multimodal-capable Gemini model (not a text-only gemma variant)."""
        prompt = (
            "Look at this image and detect any human face. "
            "Return ONLY a JSON object (no markdown): "
            "{\"face_detected\": true/false, \"face_x\": 0.0-1.0, \"face_y\": 0.0-1.0} "
            "where face_x and face_y are the normalized center coordinates of the "
            "largest detected face (0,0 = top-left corner, 1,1 = bottom-right corner, "
            "0.5,0.5 = center of frame). If no face is found, return "
            "{\"face_detected\": false, \"face_x\": 0.5, \"face_y\": 0.5}."
        )
        fallback = {"face_detected": False, "face_x": 0.5, "face_y": 0.5}
        try:
            raw = self._generate(
                [prompt, types.Part.from_bytes(data=image_bytes, mime_type=mime_type)],
                model=config.get_transcribe_model(),
            )
            data = json.loads(_strip_code_fence(raw))
            if isinstance(data, dict) and "face_detected" in data:
                return {
                    "face_detected": bool(data.get("face_detected")),
                    "face_x": float(data.get("face_x", 0.5)),
                    "face_y": float(data.get("face_y", 0.5)),
                }
        except Exception as e:
            print(f"⚠️ [GeminiClient] detect_face_in_image failed: {e}")
        return fallback
