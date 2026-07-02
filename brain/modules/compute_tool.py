"""
通用計算工具：把使用者的各種資料（財務、記帳、提醒、待辦、活動、睡眠）交給 Gemini，
用 code execution『真的跑程式碼』算出精確答案——任何「數/加總/佔比/平均/排序/幾倍/哪天最多」
都能算且精確，取代 LLM 心算（語言模型不會精確算術，會猜錯且前後不一）。
"""
import json
import os
import urllib.request
import datetime

GEMINI = "http://127.0.0.1:8808/v1beta/models/gemini-2.5-flash:generateContent"
_CFG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")


def _read_json(name, default=None):
    p = os.path.join(_CFG, name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _gather():
    """收集使用者所有結構化資料（永遠用最新檔案）。"""
    data = {}
    now = datetime.datetime.now()
    data["today"] = {"date": now.strftime("%Y-%m-%d"),
                     "weekday": "一二三四五六日"[now.weekday()]}
    # 財務（含即時報價 + 算好的可花/還能花）
    try:
        from modules.finance import wealth
        ov = wealth.overview()
        data["finance"] = {
            "income_total": ov.get("income"), "fixed_total": ov.get("fixed"),
            "spendable": ov.get("spendable"), "remaining": ov.get("remaining"),
            "spent_this_cycle": ov.get("month_var"), "daily_allowance": ov.get("daily_allowance"),
            "days_left": ov.get("days_left"), "cycle": ov.get("cycle_label"),
            "income_items": ov.get("income_list"), "fixed_items": ov.get("fixed_list"),
            "usd_twd": ov.get("portfolio", {}).get("usd_twd"),
            "holdings": ov.get("portfolio", {}).get("items"),
            "by_market": ov.get("portfolio", {}).get("by_market"),
            "net_worth": ov.get("net_worth"),
        }
    except Exception as e:
        data["finance_error"] = str(e)
    # 記帳、提醒、待辦、活動、睡眠、清單
    exp = _read_json("expenses.json", {})
    data["expenses"] = exp.get("expenses") if isinstance(exp, dict) else exp
    rem = _read_json("reminders.json", {})
    data["reminders"] = rem.get("reminders") if isinstance(rem, dict) else rem
    data["activity"] = _read_json("activity.json")
    data["sleep"] = _read_json("sleep.json")
    data["checklists"] = _read_json("checklists.json")
    # 待辦（從 8809 端點抓）
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/todos", timeout=5))
        data["todos"] = d.get("todos") or d
    except Exception:
        pass
    return data


def _gemini_text(prompt, timeout=30):
    """純文字 generateContent（不用 code_execution → 不會撞 code_execution 的 503/額度）。"""
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0}}).encode("utf-8")
    req = urllib.request.Request(GEMINI, data=body,
                                 headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    cands = resp.get("candidates") or []
    if not cands:
        return ""
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


# LLM 寫的計算 code 只准用這些（沒有 import / open / eval / exec → 不能讀寫檔案或亂搞）
_SAFE_BUILTINS = {
    "sum": sum, "min": min, "max": max, "sorted": sorted, "len": len, "round": round,
    "abs": abs, "int": int, "float": float, "str": str, "bool": bool, "list": list,
    "dict": dict, "set": set, "tuple": tuple, "range": range, "enumerate": enumerate,
    "any": any, "all": all, "map": map, "filter": filter, "zip": zip, "reversed": reversed,
}


def _strip_fence(code):
    import re
    m = re.search(r"```(?:python)?\s*(.*?)```", code or "", re.S)
    return (m.group(1) if m else (code or "")).strip()


def _safe_exec(code, data, timeout=4):
    """在受限環境跑 LLM 寫的計算 code（無 import/IO），取出 result。逾時/出錯丟例外。
    常用的安全模組(datetime/collections/math…)直接放進命名空間，LLM 不必也不能 import。"""
    import threading
    import datetime as _dt
    import collections as _col
    import math as _math
    import statistics as _stat
    import re as _re
    ns = {"__builtins__": dict(_SAFE_BUILTINS), "data": data, "result": None,
          "datetime": _dt, "collections": _col, "math": _math,
          "statistics": _stat, "re": _re}
    err = {}

    def _run():
        try:
            exec(code, ns)
        except Exception as e:
            err["e"] = e
    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError("計算超時")
    if "e" in err:
        raise err["e"]
    return ns.get("result")


def calc(question):
    """讓 LLM 寫 Python（純文字、不用 Gemini code_execution → 不會 503），在本地安全執行 → 快、穩、免額度。
    語言模型的強項是『寫程式』，弱項是『心算』；所以讓它寫、由本地電腦算。"""
    data = _gather()
    base = (
        "你是資料分析助理。下面是 Owen 的資料，已是一個 Python dict 變數 `data`：\n"
        "# " + json.dumps(data, ensure_ascii=False, default=str) + "\n\n"
        "【重要】比較台股 vs 美股、或算某市場市值/報酬，直接用已換算好台幣的 "
        "data['finance']['by_market']（['TW'] 台股、['US'] 美股，各有 value 市值、retpct 報酬率）—— "
        "不要自己從 holdings 重算（美股換匯很容易算錯）。\n"
        "其他規則：金額單位台幣；用 data['today']['date'] 當今天算過期/這週/這個月。\n"
        "可直接使用（不要也不能 import）：datetime、collections、math、statistics、re，以及 "
        "sum/min/max/sorted/len/round 等基本函式。讀寫檔案、import 都禁止。\n"
        "請寫一小段 Python，把『給使用者看的繁體中文一句話答案（含算出來的數字）』指派給變數 result。\n"
        "只輸出 Python 程式碼本身，不要解說、不要 markdown。問題：「" + str(question) + "」"
    )
    import time as _t
    last, prev_code, prev_err = None, None, None
    for _attempt in range(3):
        prompt = base
        if prev_err:   # 把上次的錯誤餵回去讓 LLM 自己改（模仿 code execution 的迭代）
            prompt += ("\n\n上次你寫的程式碼執行出錯，請修正後重寫（記得不要 import）：\n"
                       + str(prev_code)[:600] + "\n錯誤訊息：" + str(prev_err)[:200])
        try:
            code = _strip_fence(_gemini_text(prompt))
            if not code:
                last = "沒寫出程式"; _t.sleep(1.0); continue
            result = _safe_exec(code, data)
            if result is None or not str(result).strip():
                prev_code, prev_err = code, "result 沒被指派或為空"
                last = prev_err; _t.sleep(0.5); continue
            return {"ok": True, "answer": str(result)}
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code in (500, 502, 503, 504) and _attempt < 2:
                _t.sleep(2); continue
            break
        except Exception as ex:   # code 跑錯 → 把錯誤餵回去重寫
            prev_code = locals().get("code")
            prev_err = last = str(ex)
            if _attempt < 2:
                _t.sleep(0.4); continue
            break
    return {"ok": False, "error": "算這題時卡住了，等等再問我一次（%s）" % last}
