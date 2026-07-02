"""Hermes 新功能工具：設提醒、記帳。呼叫 Mac 的 8809 端點。"""
import json
import logging
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

_log = logging.getLogger("hermes_tools")

BASE = "http://host.docker.internal:8809"


def _post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


set_reminder_desc = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "當使用者要你提醒他做某件事、設鬧鐘、到某時間叫他時呼叫。"
            "例如「提醒我晚上8點吃藥」「9點半叫我開會」。"
            "【最重要】使用者如果【沒有明講要提早多久提醒】，你【絕對不要直接呼叫這個工具、也不要自己預設準時】——"
            "要先【開口反問他】：「這個要提早多久提醒你？還是準時就好？要用語音還是傳訊息？」，"
            "等他回答了，再帶著 lead_minutes 和 channel 呼叫。只有他已經講清楚提早多久時，才直接呼叫。"
            "（『X分鐘後/X小時後』這種相對時間本身就是準時、lead_minutes 填 0，不用問。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time": {"type": "string", "description": "事件時間，24小時制 HH:MM 或自然語言"
                         "（「禮拜五早上11點」「明天下午3點半」「30分鐘後」），系統會解析。"},
                "message": {"type": "string", "description": "要提醒的內容，例如「吃藥」「視訊面試」。"},
                "lead_minutes": {"type": "integer", "description": "提早幾分鐘提醒（事件時間之前）。"
                                 "使用者說『提早10分鐘』就填10；說『到時候/準時』就填0。沒講就先問他。"},
                "channel": {"type": "string", "enum": ["both", "telegram", "voice"],
                            "description": "怎麼通知：telegram=傳訊息、voice=StackChan語音講、both=兩個都。沒講就先問他。"},
                "repeat": {"type": "string", "description": "循環方式（選填）。只提醒一次就【不要填】。"
                           "每天=daily；每週X=weekly:N（N是0~6,0=週一..6=週日,例如每週三=weekly:2、每週日=weekly:6）；"
                           "每月X號=monthly:DD（例如每月5號=monthly:5）；每年X月X日(生日/紀念日)=annual:MM-DD。"},
            },
            "required": ["time", "message"],
        },
    },
}


@register_function("set_reminder", set_reminder_desc, ToolType.SYSTEM_CTL)
def set_reminder(conn, time: str, message: str, lead_minutes: int = 0, channel: str = "both", repeat: str = ""):
    try:
        _rpayload = {"time": time, "message": message,
                     "lead_minutes": lead_minutes, "channel": channel}
        if repeat:
            _rpayload["repeat"] = repeat
        d = _post("/reminder", _rpayload)
        if d.get("ok"):
            _lead = f"提早{lead_minutes}分鐘" if lead_minutes else "準時"
            _ch = {"telegram": "用Telegram", "voice": "用語音", "both": "Telegram加語音"}.get(channel, "")
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response=f"好，{d.get('time', time)} 的{message}，我{_lead}{_ch}提醒你")
        # 後端看不懂時間 → 唸出友善提示（不吐錯誤）
        return ActionResponse(action=Action.RESPONSE, result="bad_time",
                              response=d.get("text") or "我沒抓到時間耶，可以說「明天下午3點」這樣嗎？")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="提醒系統剛剛沒回應，等等再說一次")


add_expense_desc = {
    "type": "function",
    "function": {
        "name": "add_expense",
        "description": (
            "當使用者要記一筆帳、記錄花費時呼叫。例如「記一筆午餐120元」「我剛花了50塊買咖啡」。"
            "如果使用者說的是過去的日子（昨天/前天/上週X/X號花了…），用系統給你的今天日期換算成"
            "絕對日期填進 date（YYYY-MM-DD）；說「剛剛/今天/沒提時間」就不用填 date（預設今天）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "金額（數字）。"},
                "category": {"type": "string", "description": "類別，例如「午餐」「交通」「咖啡」。"},
                "note": {"type": "string", "description": "備註，可省略。"},
                "date": {"type": "string", "description": "選填，花費的日期 YYYY-MM-DD。只有使用者講過去日子（昨天/X號）才填，把相對日期換算成絕對日期。"},
            },
            "required": ["amount", "category"],
        },
    },
}


@register_function("add_expense", add_expense_desc, ToolType.SYSTEM_CTL)
def add_expense(conn, amount=None, category: str = "其他", note: str = "", date: str = ""):
    # 金額容錯：ASR/LLM 可能填字串「120」甚至國字 → 後端會再驗證，這裡只保證不崩
    try:
        _payload = {"amount": amount, "category": category, "note": note}
        if date:
            _payload["date"] = date
        d = _post("/expense", _payload)
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response=d.get("text") or f"記好了，{category}")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response=d.get("text") or "金額我沒聽清楚，再說一次金額好嗎？")
    except Exception as e:
        _log.warning(f"[add_expense] {e}")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="記帳系統剛剛沒回應，等等再說一次")


def _get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=10))


query_reminders_desc = {
    "type": "function",
    "function": {
        "name": "query_reminders",
        "description": "當使用者問「我有什麼提醒」「等等要做什麼」「有沒有設鬧鐘」時呼叫，列出目前的提醒。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("query_reminders", query_reminders_desc, ToolType.SYSTEM_CTL)
def query_reminders(conn):
    try:
        d = _get("/reminders")
        nxt = d.get("next") or {}
        # 數量/過期數/下一個倒數都已由後端算好 → 直接給大腦唸，不要它自己數或比時鐘
        facts = "【已算好】共 %s 個提醒、其中 %s 個過期" % (
            d.get("count", 0), d.get("count_overdue", 0))
        if nxt:
            facts += "、下一個是「%s」約 %s 分鐘後" % (
                nxt.get("message", ""), nxt.get("minutes_from_now"))
        data = (facts + "。（請依他剛剛問的問題自然口語回答："
                "問「幾個/還有幾個」就講數量、問「下一個/接下來」就講下一個、"
                "問「我有哪些」才整列。數字照上面算好的講，別自己數）：\n")
        return ActionResponse(Action.REQLLM, data + str(d.get("text") or "目前沒有任何提醒"), None)
    except Exception as e:
        _log.warning(f"[query_reminders] {e}")
        return ActionResponse(Action.RESPONSE, "error", "查提醒時沒回應，等等再問我")


query_expenses_desc = {
    "type": "function",
    "function": {
        "name": "query_expenses",
        "description": ("當使用者問花費／開銷／花了多少／花在哪／某天花多少時呼叫，回報花費統計。"
                        "【可查指定某一天】：使用者說「昨天／6月25號／上週三」等某一天的花費，"
                        "就把它換算成 date=YYYY-MM-DD 傳進來（依系統給的現在時間換算）；問今天或沒指定就不要帶 date。"),
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "要查哪一天，格式 YYYY-MM-DD（例如昨天/6月25號就換算成 2026-06-25）。問今天或沒指定就留空。"}
        }},
    },
}


@register_function("query_expenses", query_expenses_desc, ToolType.SYSTEM_CTL)
def query_expenses(conn, date: str = ""):
    try:
        import urllib.parse as _upq
        _ep = "/expenses_summary"
        if date and str(date).strip():
            _ep += "?date=" + _upq.quote(str(date).strip()[:10])
        d = _get(_ep)
        # 用 REQLLM 讓大腦「依使用者剛剛問的問題」自然回答，不要逐字唸死公式：
        # 問「各是什麼項目/花在哪」就一條條講明細；問總額就講總額。
        data = ("（以下是使用者的記帳資料。請依他剛剛問的問題自然口語回答，不要照唸："
                "問「今天花在哪/各是什麼項目/明細/列出」就把今天每一筆一條條講給他聽；"
                "問「總共多少」才講總額。像真人一樣，只講他問的）：")
        return ActionResponse(Action.REQLLM, data + str(d.get("text") or ""), None)
    except Exception as e:
        _log.warning(f"[query_expenses] {e}")
        return ActionResponse(Action.RESPONSE, "error", "查花費明細時沒回應，等等再問我")


query_finance_desc = {
    "type": "function",
    "function": {
        "name": "query_finance",
        "description": ("當使用者『單純查』跟錢、財務、收入、薪水、開銷、預算、存款、"
                        "投資、股票、持股、報酬、賺多少、虧多少、漲跌、淨資產有關的數字時呼叫。"
                        "例如「我投資賺多少」「這個月還能花多少」「我的淨資產多少」「台積電賺多少」「我這個月收支」。"
                        "【但如果問題要『算或比較』——哪天/哪筆花最多、佔比、平均、幾倍、排序、加總某幾項——"
                        "那是 calc 的工作，不要用這個】。"),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "使用者剛剛問的原話（用來判斷要不要抓即時股價：只問花費就不抓、更快）"}
        }},
    },
}


@register_function("query_finance", query_finance_desc, ToolType.SYSTEM_CTL)
def query_finance(conn, question: str = ""):
    try:
        # 把問題帶過去：只問花費就不抓即時股價（快很多），問投資才抓
        import urllib.parse as _up
        _url = BASE + "/finance_summary"
        if question:
            _url += "?q=" + _up.quote(question[:80])
        d = json.load(urllib.request.urlopen(_url, timeout=15))
        # 用 REQLLM：把完整財務資料交給大腦，依「使用者剛剛問的問題」回答。
        # 關鍵：問「花在哪/哪些/明細/列出/列一下/買了什麼」就要逐項把明細唸出來，不要只報總額；
        # 問總額/還能花/報酬就講那個數字。口語自然，不要把所有資料全唸一遍。
        data = ("（以下是你掌握的即時財務數據。請『針對使用者剛剛問的那個問題』回答，"
                "而不是回固定內容：他若問「花在哪些/明細/列出/買了什麼」就把對應的逐項明細一條條唸給他聽；"
                "他若問總額/還能花/報酬率/淨資產就講那個數字。口語自然、只講他問的部分）：")
        return ActionResponse(Action.REQLLM, data + str(d.get("text") or ""), None)
    except Exception as e:
        # 失敗一律走 RESPONSE 給固定友善句，絕不把例外字串 REQLLM 餵給 LLM（會被照唸或瞎掰）
        _log.warning(f"[query_finance] {e}")
        return ActionResponse(Action.RESPONSE, "error",
                              "我這邊一時抓不到財務數據，等一下再問我一次好嗎")


calc_desc = {
    "type": "function",
    "function": {
        "name": "calc",
        "description": ("任何問題需要『計算/統計』時呼叫——不限財務，含記帳、提醒、待辦、活動、睡眠。"
                        "例如幾倍、加總、比例、占比、平均、排序、合計、「台股是美股幾倍」「這個月哪天花最多」"
                        "「外食佔比多少」「我有幾個過期提醒」「待辦完成幾%」「這週運動幾次」。"
                        "它會抓你的真實資料、用程式碼真的算出精確答案。【你自己絕對不要心算/估計/數，你算一定會錯且每次不一樣】。"
                        "把使用者原本問的整句話放進 question。"),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "使用者要計算的完整問題原文"}
        }, "required": ["question"]},
    },
}


@register_function("calc", calc_desc, ToolType.SYSTEM_CTL)
def calc(conn, question: str = ""):
    try:
        body = json.dumps({"question": question}).encode("utf-8")
        req = urllib.request.Request(BASE + "/calc", data=body,
                                     headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=75))
        if d.get("ok"):
            return ActionResponse(Action.REQLLM,
                                  "（這是程式實際執行算出來的精確答案，照這個數字講給使用者，不要改）："
                                  + str(d.get("answer")), None)
        return ActionResponse(Action.RESPONSE, "error", "算這題時卡了一下，等等再問我一次")
    except Exception as e:
        _log.warning(f"[calc] {e}")
        return ActionResponse(Action.RESPONSE, "error", "算這題時卡了一下，等等再問我一次")


update_finance_desc = {
    "type": "function",
    "function": {
        "name": "update_finance",
        "description": ("當使用者要『改變/設定/新增/刪除』財務數字時呼叫（不是查詢）。例如："
                        "「這個月改成存7000」→set_save_goal amount=7000；"
                        "「我薪水變成40000」→set_income name=薪水 amount=40000；"
                        "「房租改18000」→set_fixed name=房租 amount=18000；"
                        "「本期能花改成X」「這個月可花X」「我想本期花X」「可花金額設成X」「每月最多花三萬」→set_spend_limit amount=X（直接設定/覆寫本期可花金額，設0則回到自動估算＝收入−固定−存款）；"
                        "「加一檔台積電100股成本900」→set_holding symbol=2330 market=TW name=台積電 shares=100 cost=900；"
                        "「台積電改成150股」→set_holding symbol=2330 shares=150；"
                        "「把特斯拉賣掉」→remove_holding name=特斯拉；"
                        "「我銀行有50萬」→set_cash amount=500000；"
                        "「財務自由目標設一千萬」→set_fire_target amount=10000000；"
                        "「FIRE年開銷設120萬」「退休後一年要花120萬」→set_fire_annual amount=1200000。"),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["set_save_goal", "set_spend_limit", "set_income",
                                    "set_fixed", "remove_income", "remove_fixed",
                                    "set_holding", "remove_holding",
                                    "set_cash", "set_fire_target", "set_fire_annual",
                                    "set_payday", "set_remaining"],
                           "description": "要執行的動作（set_cash=銀行現金，set_fire_target=財務自由目標，set_fire_annual=FIRE年開銷×25，set_payday=發薪日幾號，set_remaining=使用者說『我只剩X要活到發薪』＝本期還能花只剩X）"},
                "amount": {"type": "number", "description": "金額（存款/上限/收入/開銷）"},
                "name": {"type": "string", "description": "項目名稱（薪水/房租）或股票中文名"},
                "symbol": {"type": "string", "description": "股票代號，如 2330、AAPL"},
                "market": {"type": "string", "enum": ["TW", "US"], "description": "台股 TW / 美股 US"},
                "shares": {"type": "number", "description": "股數"},
                "cost": {"type": "number", "description": "每股平均成本"},
                "note": {"type": "string", "description": "備註（選填）"},
            },
            "required": ["action"],
        },
    },
}


@register_function("update_finance", update_finance_desc, ToolType.SYSTEM_CTL)
def update_finance(conn, action, amount=None, name=None, symbol=None,
                   market=None, shares=None, cost=None, note=None):
    try:
        payload = {"action": action}
        for k, v in (("amount", amount), ("name", name), ("symbol", symbol),
                     ("market", market), ("shares", shares), ("cost", cost), ("note", note)):
            if v is not None:
                payload[k] = v
        d = _post("/finance/op", payload)
        # 一定要看 ok：後端可能因金額無效/找不到股票而沒改成功，這時別騙使用者說「改好了」
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response=d.get("text") or "好，改好了")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response=d.get("text") or "這個我沒改成功，再說一次看看？")
    except Exception as e:
        _log.warning(f"[update_finance] {e}")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="改的時候沒回應，等等再說一次")


set_timer_desc = {
    "type": "function",
    "function": {
        "name": "set_timer",
        "description": "當使用者要計時、設定幾分鐘後提醒時呼叫。例如「計時5分鐘」「10分鐘後叫我」「幫我設個3分鐘的計時器」。",
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "幾分鐘後，例如 5、10、0.5。"},
                "label": {"type": "string", "description": "計時的事由，可省略，例如「泡麵」「休息結束」。"},
            },
            "required": ["minutes"],
        },
    },
}


@register_function("set_timer", set_timer_desc, ToolType.SYSTEM_CTL)
def set_timer(conn, minutes=None, label: str = "時間到"):
    try:
        d = _post("/timer", {"minutes": minutes, "label": label})
        if d.get("ok"):
            # 後端的 text 已含正確分鐘數；避免在這裡對可能是字串的 minutes 做 {:g} 格式化崩潰
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response=d.get("text") or f"好，{label}的計時器設好了")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response=d.get("text") or "計時時間我沒聽清楚，再說一次幾分鐘？")
    except Exception as e:
        _log.warning(f"[set_timer] {e}")
        return ActionResponse(action=Action.RESPONSE, result="error", response="計時器系統沒回應")


get_news_desc = {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": "當使用者問新聞、最近發生什麼事、有什麼頭條時呼叫。回報台灣即時新聞頭條。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("get_news", get_news_desc, ToolType.SYSTEM_CTL)
def get_news(conn):
    try:
        d = _get("/news")
        return ActionResponse(action=Action.REQLLM, result="今日新聞頭條：" + str(d.get("text") or "今天沒有抓到新聞"), response=None)
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error", response="查新聞時沒回應")


add_todo_desc = {
    "type": "function",
    "function": {
        "name": "add_todo",
        "description": "當使用者要把一件事加進待辦清單、記下要做的事時呼叫。例如「待辦加上買牛奶」「我要記得繳電費」「幫我列進待辦」。",
        "parameters": {
            "type": "object",
            "properties": {"item": {"type": "string", "description": "待辦事項內容。"}},
            "required": ["item"],
        },
    },
}


@register_function("add_todo", add_todo_desc, ToolType.SYSTEM_CTL)
def add_todo(conn, item: str):
    try:
        d = _post("/todo", {"item": item})
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok", response=f"好，加到待辦了：{item}")
        return ActionResponse(action=Action.RESPONSE, result="error", response="加待辦出了點問題")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error", response="待辦系統沒回應")


list_todo_desc = {
    "type": "function",
    "function": {
        "name": "list_todo",
        "description": "當使用者問待辦清單、還有什麼要做、我的清單時呼叫。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("list_todo", list_todo_desc, ToolType.SYSTEM_CTL)
def list_todo(conn):
    try:
        d = _get("/todo")
        facts = "【已算好】共 %s 項待辦、完成 %s 項、還剩 %s 項。" % (
            d.get("total", 0), d.get("done_count", 0), d.get("remaining_count", 0))
        data = (facts + "（請依他剛剛問的問題自然口語回答："
                "問「剩幾項/完成幾項」就講上面算好的數字、問「還有什麼沒做」就講未完成的、"
                "問「有哪些」才全列。數字照上面講，別自己數）：\n")
        return ActionResponse(Action.REQLLM, data + str(d.get("text") or "目前沒有待辦事項"), None)
    except Exception as e:
        _log.warning(f"[list_todo] {e}")
        return ActionResponse(Action.RESPONSE, "error", "查待辦時沒回應，等等再問我")


convert_currency_desc = {
    "type": "function",
    "function": {
        "name": "convert_currency",
        "description": "當使用者問匯率、貨幣換算時呼叫。例如「100美金是多少台幣」「日幣換台幣」「1歐元等於幾塊」。常見代碼:台幣TWD 美金USD 日圓JPY 歐元EUR 人民幣CNY 港幣HKD 英鎊GBP 韓元KRW。",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "金額,沒講就用1。"},
                "frm": {"type": "string", "description": "來源貨幣代碼,例如 USD。"},
                "to": {"type": "string", "description": "目標貨幣代碼,例如 TWD。沒講就 TWD。"},
            },
            "required": ["frm"],
        },
    },
}


@register_function("convert_currency", convert_currency_desc, ToolType.SYSTEM_CTL)
def convert_currency(conn, frm: str, amount: float = 1, to: str = "TWD"):
    try:
        import urllib.parse as _uq
        d = _get("/currency?" + _uq.urlencode({"amount": amount, "frm": frm, "to": to}))
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok", response=d.get("text") or "好了")
        return ActionResponse(action=Action.RESPONSE, result="error", response="這個貨幣我換不了")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error", response="匯率服務沒回應")


dispatch_task_desc = {
    "type": "function",
    "function": {
        "name": "dispatch_task",
        "description": (
            "只有當使用者用【明確祈使句要你去執行一個多步驟、要花好幾分鐘產出的工作】才呼叫,"
            "例如「幫我規劃一個五天行程」「幫我整理這些資料做成報告」「幫我研究並比較三個方案再給我完整建議」。"
            "【以下絕對不要呼叫,要直接用你自己的知識回答】："
            "「你對X的想法/看法」「X是做什麼的」「X是什麼公司/東西」「你覺得…」「介紹一下X」「解釋X」"
            "——這些是知識/意見問題,你直接講你知道的就好,不是派任務。"
            "判準:心裡能直接講出答案的=直接答;真的要你動手做一份會花時間的產出才派。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "完整的任務描述。"}},
            "required": ["task"],
        },
    },
}


@register_function("dispatch_task", dispatch_task_desc, ToolType.SYSTEM_CTL)
def dispatch_task(conn, task: str):
    try:
        d = _post("/dispatch_task", {"task": task})
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response="好,這個交給我,我去認真處理,弄好馬上跟你說")
        return ActionResponse(action=Action.RESPONSE, result="error", response="派任務出了點問題")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error", response="後台沒回應")


build_with_claude_desc = {
    "type": "function",
    "function": {
        "name": "build_with_claude",
        "description": ("當使用者要你『做/寫一個東西且需要寫程式』時呼叫——做網站、做網頁、寫 html、"
                        "做一個小工具/小遊戲/小程式/app、寫一段程式、做個計算機/番茄鐘/作品集之類。"
                        "它會叫電腦終端機裡『真正的 Claude Code』在獨立資料夾把成品寫出來，做好再回報你。"
                        "【你自己絕對不要在嘴上硬寫 code，一律交給它】。把使用者要做的東西原話放進 task。"),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "使用者要做的東西的完整描述（原話）"}
        }, "required": ["task"]},
    },
}


@register_function("build_with_claude", build_with_claude_desc, ToolType.SYSTEM_CTL)
def build_with_claude(conn, task: str = ""):
    try:
        d = _post("/code_task", {"task": task})
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response="好，這個我交給 Claude Code 去寫，做好馬上跟你說")
        return ActionResponse(action=Action.RESPONSE, result="error", response="派這個任務出了點問題")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error", response="後台沒回應，等等再試")


search_web_desc = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": ("當使用者問的是『當前/最新/外部』的事實、或你自己不確定、或是會變動的即時資訊時呼叫——"
                        "例如最新新聞、現在的價格/匯率/比分、某公司或人物近況、某產品規格、今天發生什麼、"
                        "任何你不敢肯定的事實。【會變動或你不確定的事,絕不要憑記憶瞎掰,一律用這個查】。把問題放進 query。"),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "要查的問題原文"}
        }, "required": ["query"]},
    },
}


@register_function("search_web", search_web_desc, ToolType.SYSTEM_CTL)
def search_web(conn, query: str = ""):
    try:
        d = _post("/search", {"query": query})
        if d.get("ok"):
            return ActionResponse(Action.REQLLM,
                                  "（這是即時上網查到的，照這個講、別加油添醋）：" + str(d.get("answer")), None)
        return ActionResponse(Action.RESPONSE, "error", "查這個時網路卡了一下，等等再問我")
    except Exception as e:
        _log.warning(f"[search_web] {e}")
        return ActionResponse(Action.RESPONSE, "error", "查這個時網路卡了一下，等等再問我")


dance_desc = {
    "type": "function",
    "function": {
        "name": "dance",
        "description": "當使用者叫你跳舞、扭一下、動一動、表演一下時呼叫。你會用頭部和 LED 跳一段舞。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("dance", dance_desc, ToolType.SYSTEM_CTL)
def dance(conn):
    try:
        import asyncio
        import json as _j
        mcp = getattr(conn, "mcp_client", None)
        loop = getattr(conn, "loop", None)
        if mcp and loop:
            from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool

            async def _dance():
                moves = [(70, 25), (-70, 25), (60, 60), (-60, 60),
                         (0, 45), (75, 35), (-75, 35), (40, 65), (-40, 65), (0, 45)]
                for i, (y, p) in enumerate(moves):
                    try:
                        for n in ("self.robot.set_head_angles", "self_robot_set_head_angles"):
                            if mcp.has_tool(n):
                                await call_mcp_tool(conn, mcp, n,
                                                    _j.dumps({"yaw": y, "pitch": p}), timeout=6)
                                break
                        for ln in ("self.led.set_all", "self_led_set_all"):
                            if mcp.has_tool(ln):
                                await call_mcp_tool(conn, mcp, ln,
                                                    _j.dumps({"r": (i * 70) % 256,
                                                              "g": (255 - i * 50) % 256,
                                                              "b": (i * 110) % 256}), timeout=6)
                                break
                        await asyncio.sleep(0.45)
                    except Exception:
                        pass

            asyncio.run_coroutine_threadsafe(_dance(), loop)
        return ActionResponse(action=Action.RESPONSE, result="ok", response="好，看我跳～")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error", response="我扭一下給你看～")


DASH = "http://host.docker.internal:8811"

save_to_desktop_desc = {
    "type": "function",
    "function": {
        "name": "save_to_desktop",
        "description": (
            "當使用者要你把內容『存成檔案』『存到桌面』『寫成檔案給我』『幫我記成txt』時呼叫。"
            "例如「把剛剛那段話存成檔案放桌面」「幫我把這份清單存成檔案」。"
            "你要把要儲存的完整內容放進 content，並取一個合適的檔名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "檔名，例如「會議筆記.txt」「購物清單.md」。"},
                "content": {"type": "string",
                            "description": "要寫進檔案的完整文字內容。"},
            },
            "required": ["filename", "content"],
        },
    },
}


@register_function("save_to_desktop", save_to_desktop_desc, ToolType.SYSTEM_CTL)
def save_to_desktop(conn, filename: str, content: str):
    try:
        req = urllib.request.Request(
            DASH + "/api/save_file",
            data=json.dumps({"filename": filename, "content": content}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=10))
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response=f"好了，我把它存成「{d.get('name')}」放在你桌面上了")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="存檔時出了點問題，再說一次要存什麼")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="存檔服務剛剛沒回應，等等再試")


find_nearby_desc = {
    "type": "function",
    "function": {
        "name": "find_nearby",
        "description": (
            "當使用者問『附近有什麼』『周圍的東西』『幫我找一間XX』『最近的XX在哪』"
            "『附近的餐廳/咖啡廳/按摩/藥局/便利商店/醫院』時呼叫。會用地圖查真實地點。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string",
                            "description": "要找什麼，例如「按摩」「咖啡廳」「藥局」「拉麵」。"},
                "location": {"type": "string",
                             "description": "區域，例如「信義區」「板橋」。沒講就留空（用裝置定位）。"},
            },
            "required": ["keyword"],
        },
    },
}


@register_function("find_nearby", find_nearby_desc, ToolType.SYSTEM_CTL)
def find_nearby(conn, keyword: str, location: str = ""):
    try:
        import urllib.parse
        url = BASE + "/nearby?" + urllib.parse.urlencode({"keyword": keyword, "location": location})
        d = json.load(urllib.request.urlopen(url, timeout=15))
        txt = d.get("text")
        if not txt:
            return ActionResponse(Action.RESPONSE, "error", "附近我找不到耶，換個說法或地點試試")
        data = ("（以下是附近地點的查詢結果。請依使用者剛剛問的自然口語回答："
                "他問最近的就講最近那間、問評價/距離就挑那個資訊講，像真人一樣只講重點）：")
        return ActionResponse(Action.REQLLM, data + str(txt), None)
    except Exception as e:
        _log.warning(f"[find_nearby] {e}")
        return ActionResponse(Action.RESPONSE, "error", "地圖服務剛剛沒回應，等等再問我")


build_feature_desc = {
    "type": "function",
    "function": {
        "name": "build_feature",
        "description": (
            "當使用者要的功能你【現在真的還沒有】、現有工具都做不到時呼叫——"
            "這會請後台的工程師(Claude Code)【真的把這個新功能寫出來】。"
            "例如使用者要「幫我看 YouTube 訂閱數」「幫我控制家裡的燈」「加一個記血壓的功能」"
            "這類你目前沒有對應工具的需求。"
            "【重要】呼叫前先確認現有工具真的做不到（查天氣/找地點/記帳/提醒/音樂等已經有了，別亂呼叫）。"
            "呼叫後告訴使用者你已經請工程師去做了、做好會通知他。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string",
                                "description": "要做的新功能的清楚描述（你幫使用者整理成一句完整需求）。"},
            },
            "required": ["description"],
        },
    },
}


@register_function("build_feature", build_feature_desc, ToolType.SYSTEM_CTL)
def build_feature(conn, description: str):
    try:
        req = urllib.request.Request(
            BASE + "/build_feature",
            data=json.dumps({"description": description}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=10))
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response="好，這個我目前還沒有，我已經請工程師幫你做了，做好我馬上通知你～")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="我想幫你加這個功能，但派工時卡住了，等等再試")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="自我擴充服務剛剛沒回應，等等再說一次")


do_on_computer_desc = {
    "type": "function",
    "function": {
        "name": "do_on_computer",
        "description": (
            "當使用者要你在他電腦上做事、需要操作電腦/上網/查檔案才能完成的任務時呼叫。"
            "例如「幫我查我專案資料夾裡有什麼」「上網查X幫我整理」「幫我在電腦上做Y」。"
            "純查資料/讀取會自動做；任何會建立、修改或刪除檔案的，系統會排到控制台等使用者按確認才執行（保護使用者）。"
            "簡單的查天氣/找地點/記帳/提醒/放音樂已有專門工具，用那些。"
            "呼叫後告訴使用者你在處理了，需要動到檔案的話會請他到控制台確認。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "要在電腦上完成的任務（整理成清楚的一句）。"},
            },
            "required": ["task"],
        },
    },
}


@register_function("do_on_computer", do_on_computer_desc, ToolType.SYSTEM_CTL)
def do_on_computer(conn, task: str):
    try:
        req = urllib.request.Request(
            BASE + "/do_on_computer",
            data=json.dumps({"task": task}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=10))
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response="好，我幫你處理；如果會動到檔案，我會放到控制台請你按確認再做～")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="我想幫你做，但派工時卡住了，等等再試")
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="電腦助理服務剛剛沒回應，再說一次")


# ── 喝水記錄（自主build做了後端+dashboard，語音工具由這裡補完整條鏈）──
drink_water_desc = {
    "type": "function",
    "function": {
        "name": "drink_water",
        "description": "使用者說「喝水」「我喝了一杯水」「記一杯水」時呼叫，記錄一杯並回報今天累計。"
                       "問「今天喝幾杯水」「喝水喝多少」也用這個（cups 填 0 表示只查詢不加）。",
        "parameters": {
            "type": "object",
            "properties": {
                "cups": {"type": "number", "description": "要加記的杯數，預設 1；只是查詢就填 0。"},
            },
            "required": [],
        },
    },
}


@register_function("drink_water", drink_water_desc, ToolType.SYSTEM_CTL)
def drink_water(conn, cups=1):
    try:
        try:
            cups = int(float(cups))
        except (TypeError, ValueError):
            cups = 1
        if cups > 0:
            d = _post("/water/add", {"cups": cups})
        else:
            d = _get("/water/today")
        if d.get("ok"):
            return ActionResponse(action=Action.RESPONSE, result="ok",
                                  response=d.get("text") or "記好了")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="喝水記錄剛剛沒回應，等等再說一次")
    except Exception as e:
        _log.warning(f"[drink_water] {e}")
        return ActionResponse(action=Action.RESPONSE, result="error",
                              response="喝水記錄剛剛沒回應，等等再說一次")
