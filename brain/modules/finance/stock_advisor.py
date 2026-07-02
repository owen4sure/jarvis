"""
AI 投資分析（套用 daily_stock_analysis 的概念：每日 AI 選股/持股分析）。
用 Owen 既有的 portfolio()(YFinance 即時報價) + Gemini，產生每檔訊號 + 組合評估。
輕量整合：不搬整套 FastAPI 平台，只把「分析能力」接進現有 dashboard 投資頁。
"""
import json
import urllib.request

from modules.finance import wealth

GEMINI = "http://127.0.0.1:8808/v1beta/models/gemini-2.5-flash:generateContent"


def _gemini(prompt, timeout=60):
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }).encode("utf-8")
    req = urllib.request.Request(GEMINI, data=body,
                                 headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    cands = resp.get("candidates") or []
    if not cands:
        raise ValueError("Gemini 無回應（可能被安全過濾或額度用盡）")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)   # 合併所有 part，不只第一段


def _extract_json(txt):
    """從 Gemini 文字中穩健抽出 JSON：去掉 ``` 圍欄、容忍前後贅述。"""
    import re
    t = (txt or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e < 0:
        raise ValueError("找不到 JSON")
    return json.loads(t[s:e + 1])


def analyze():
    """回傳每檔持股的 AI 訊號 + 組合評估。資料來自即時報價。"""
    p = wealth.portfolio()
    items = p.get("items", [])
    if not items:
        return {"ok": False, "error": "沒有持股資料"}

    lines = []
    for h in items:
        lines.append("- %s(%s/%s)：報酬率 %s%%、今日 %s%%、市值 %s、現價 %s" % (
            h.get("name"), h.get("symbol"), h.get("market"),
            h.get("retpct"), h.get("todaypct"), h.get("value"), h.get("price")))

    prompt = (
        "你是 Owen 的投資幕僚，務實、白話、不浮誇。以下是他目前的持股（即時報價）：\n"
        + "\n".join(lines)
        + "\n\n總市值 %s、總報酬率 %s%%、今日 %s%%。\n\n" % (
            p.get("total_value"), p.get("total_retpct"), p.get("total_todaypct"))
        + "請給一份簡潔的每日投資分析（繁體中文）：\n"
          "1. 每檔一個訊號（續抱 / 加碼 / 減碼 / 觀望）＋ 一句白話理由。\n"
          "2. 整體組合評估（集中度、風險、配置）。\n"
          "3. 今天該注意的 1–2 點。\n"
          '只回 JSON：{"stocks":[{"name":"","signal":"","reason":""}],"overall":"","watch":""}'
    )
    try:
        data = _extract_json(_gemini(prompt))
    except Exception as ex:
        return {"ok": False, "error": "AI 分析失敗：%s" % ex}

    data["ok"] = True
    data["total_value"] = p.get("total_value")
    data["total_retpct"] = p.get("total_retpct")
    data["disclaimer"] = "AI 分析僅供參考，非投資建議"
    return data


def compute(question):
    """用 Gemini 的 code execution『真的跑程式碼』計算財務問題——任何問法都能算、且精確，
    取代 LLM 心算（語言模型不會精確算術，會猜錯）。"""
    p = wealth.portfolio()
    data = {
        "usd_twd": p.get("usd_twd"),
        "total_value_twd": p.get("total_value"),
        "total_retpct": p.get("total_retpct"),
        "holdings": [{
            "name": h.get("name"), "market": h.get("market"),
            "shares": h.get("shares"), "cost": h.get("cost"), "price": h.get("price"),
            "currency": h.get("currency"),
            "value_native": h.get("value"), "ret_native": h.get("ret"),
            "retpct": h.get("retpct"), "today_native": h.get("today"), "todaypct": h.get("todaypct"),
        } for h in p.get("items", [])],
    }
    prompt = (
        "你是 Owen 的投資助理。以下是他的持股資料（JSON）。\n"
        + json.dumps(data, ensure_ascii=False)
        + "\n\n規則：market='TW' 是台股、'US' 是美股；美股金額是美元，要×usd_twd 換成台幣才能跟台股比較。"
          "report 報酬率(retpct) 已是百分比。\n"
          "請【務必用 Python code execution 實際執行計算】來回答下面的問題，絕對不要自己心算或估計：\n「"
        + str(question) + "」\n"
          "最後用繁體中文一句話給出答案數字 + 簡短說明（數字要照程式算出來的，不要改）。"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"code_execution": {}}],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(GEMINI, data=body,
                                     headers={"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=70))
        cands = resp.get("candidates") or []
        if not cands:
            return {"ok": False, "error": "計算無回應（可能被過濾或額度用盡）"}
        parts = (cands[0].get("content") or {}).get("parts") or []
        # code execution 回傳會混 text(計畫) / executableCode / codeExecutionResult / text(最終答案)。
        # 取「最後一段純文字」= 最終結論，不要把中間的計畫步驟也唸出來。
        texts = [p.get("text", "").strip() for p in parts if p.get("text", "").strip()]
        ans = texts[-1] if texts else ""
        return {"ok": True, "answer": ans or "（沒有取得文字答案）"}
    except Exception as ex:
        return {"ok": False, "error": "計算失敗：%s" % ex}


def news():
    """用 Gemini 接地搜尋（Google Search grounding）抓持股的最新新聞 + 影響判讀。"""
    p = wealth.portfolio()
    items = p.get("items", [])
    if not items:
        return {"ok": False, "error": "沒有持股資料"}
    names = "、".join(h.get("name", "") for h in items if h.get("name"))
    query = (
        "用 Google 搜尋幫我查這些持股/標的『最近幾天』的重要新聞：" + names + "。\n"
        "用繁體中文，每個有新聞的標的列最多 2 則最重要的，每則一行，格式：\n"
        "• 〔利多/利空/中性〕標的名：一句話講重點。\n"
        "沒有近期新聞的標的就略過。最後用一段「📈 整體市場氛圍」總結今天台股/美股的氣氛。\n"
        "簡潔、白話、務實，不要長篇。"
    )
    # 用 8808 proxy(管理金鑰) + google_search grounding，不走會缺金鑰的 ResearchModule
    body = json.dumps({
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(GEMINI, data=body,
                                     headers={"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=85))
        cands = resp.get("candidates") or []
        if not cands:
            return {"ok": False, "error": "新聞搜尋無回應（可能被安全過濾或額度用盡）"}
        parts = (cands[0].get("content") or {}).get("parts") or []
        txt = "".join(p.get("text", "") for p in parts)
    except Exception as ex:
        return {"ok": False, "error": "新聞搜尋失敗：%s" % ex}
    return {"ok": True, "news": (txt or "").strip(),
            "disclaimer": "新聞由 AI 接地搜尋彙整，僅供參考"}
