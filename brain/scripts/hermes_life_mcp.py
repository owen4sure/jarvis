"""
Hermes 生活工具 MCP Server
============================
把 Owen 的生活功能（財務/記帳/提醒/音樂/天氣）暴露成 MCP 工具，掛給 hermes-agent。
這樣 hermes-agent 當語音大腦時，同時擁有它的進化能力 ＋ 這些既有能力（真正更強）。

每個工具只是轉呼叫既有的本機端點（8809 記憶/財務/提醒、8810 音樂），不重造邏輯。
用 stackchan-mcp 的 venv 跑（它已有 mcp 套件）：
  /Users/USERNAME/.local/share/uv/tools/stackchan-mcp/bin/python3 hermes_life_mcp.py
掛上去：
  hermes mcp add hermes-life --url http://127.0.0.1:8769/mcp
"""
import json
import subprocess
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

MEM = "http://127.0.0.1:8809"   # 財務/記帳/提醒/記憶
MUSIC = "http://127.0.0.1:8810"  # 音樂

mcp = FastMCP("hermes-life")
mcp.settings.host = "127.0.0.1"
mcp.settings.port = 8769


def _get(url: str, timeout: float = 20.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _post(url: str, payload: dict, timeout: float = 20.0) -> dict:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def query_finance(question: str = "") -> str:
    """查 Owen 的財務/預算/投資/持股：賺多少、報酬率、淨資產、每天還能花多少、各檔股票、台股美股。摘要已算好「每天可花」，照唸別自己算。question 帶原問題。"""
    u = f"{MEM}/finance_summary?q=" + urllib.parse.quote(question or "財務")
    d = _get(u, timeout=25)
    return d.get("text") or d.get("error") or "查不到財務資料"


@mcp.tool()
def project_wealth_goal(target: float = 4000000, years: float = 0, target_age: int = 0) -> str:
    """財務目標試算：幾歲存到多少、需要多少報酬、還差多少。問到資產目標就用這個、別自己心算。target=目標金額，target_age=幾歲達標，years=還有幾年。"""
    import urllib.parse as _up
    qs = f"target={target}"
    if years:
        qs += f"&years={years}"
    if target_age:
        qs += f"&target_age={target_age}"
    d = _get(f"{MEM}/finance/project?{qs}", timeout=25)
    return d.get("text") or d.get("error") or "試算失敗"


@mcp.tool()
def wealth_trend() -> str:
    """資產【每日/每週/每月】變動 + 離目標多遠。問「我資產這週變多少、這個月有沒有進步、離目標多遠」時用。"""
    d = _get(f"{MEM}/networth/trend", timeout=25)
    return d.get("text") or d.get("error") or "查不到資產趨勢"


@mcp.tool()
def set_wealth_goal(target: float = 0, target_age: int = 0) -> str:
    """設定/調整資產目標。Owen 說「把目標改成 500 萬」「改成 32 歲達標」時用；只改的那項傳就好。
    target=目標金額，target_age=幾歲達標。"""
    body = {}
    if target:
        body["target"] = target
    if target_age:
        body["target_age"] = target_age
    if not body:
        return "要改成多少或幾歲達標？"
    d = _post(f"{MEM}/goal/set", body)
    return d.get("text") or d.get("error") or "設定失敗"


@mcp.tool()
def query_expenses(date: str = "") -> str:
    """查花費明細：今天/某天花了什麼、各分類多少。預算問題(每天還能花)要用 query_finance。
    date 留空=今天;接受任何格式:「昨天」「前天」「7月2號」「7/2」「2026-07-02」都行,把使用者講的日期原樣傳入。"""
    u = f"{MEM}/expenses_summary"
    if date:
        u += "?date=" + urllib.parse.quote(date)
    d = _get(u)
    return d.get("text") or d.get("error") or "查不到花費"


@mcp.tool()
def add_expense(amount: float, note: str = "", category: str = "", date: str = "") -> str:
    """Owen 報出「東西+金額」(茶碗蒸36)就記一筆花費。他在問價/聊天/假設(這貴嗎)就別記。
    amount=元，note=品項，category=分類(可空)。
    date=【重要】他說「昨天/前天/7月2號的宵夜」這種補記時,一定要把他講的日期原樣帶入
    (昨天/7月2號/7/2 任何格式都行);沒提日期就留空=今天。"""
    d = _post(f"{MEM}/expense", {"amount": amount, "note": note, "category": category, "date": date})
    if d.get("ok"):
        return f"好，記下了 {int(amount)} 元" + (f"（{note}）" if note else "")
    return d.get("error") or "記帳失敗"


_CN_NUM = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "万": 10000, "億": 100000000}


def _amt(x):
    """把任何「數字樣子」的輸入洗成乾淨 float：123 / "7300" / "7,300" / "$7300元" / "七千三百" / "5萬"。
    解析不出來回 None。讓金額不會因為格式而讓後端打回 → 該成功就成功。"""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s:
        return None
    # 先抓阿拉伯數字(去掉逗號/錢符號/單位字)
    cleaned = s.replace(",", "").replace("，", "").replace("$", "").replace("＄", "")
    import re as _re
    m = _re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if m:
        val = float(m.group())
        # 處理「3萬」「5千」這種：數字後面跟中文單位
        tail = cleaned[m.end():]
        for u, mult in (("億", 1e8), ("萬", 1e4), ("万", 1e4), ("千", 1e3), ("k", 1e3), ("K", 1e3)):
            if u in tail:
                val *= mult
                break
        return val
    # 純中文數字：七千三百 / 五萬 / 一百二十
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_NUM:
            num = _CN_NUM[ch]
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            if u >= 10000:
                section = (section + num) * u or u  # 五百萬=(500+0)*10000；單獨「萬」=10000
                total += section
                section = 0
            else:
                section += (num or 1) * u  # 十=10、五百=500
            num = 0
        # 其他字略過
    result = total + section + num
    return float(result) if result else None


def _fin_op(payload: dict) -> str:
    """呼叫後端 /finance/op 真正改資料。金額類欄位先洗成乾淨數字，確保不會因格式被打回。"""
    for f in ("amount", "shares", "cost"):
        if f in payload and payload[f] is not None:
            v = _amt(payload[f])
            if v is not None:
                payload[f] = v
    d = _post(f"{MEM}/finance/op", payload)
    if d.get("ok"):
        return d.get("text") or d.get("msg") or "改好了"
    return "沒改成功：" + str(d.get("text") or d.get("msg") or d.get("error") or "後端沒接受，請再說一次")


@mcp.tool()
def set_spendable(amount: float) -> str:
    """設定 Owen「本期還能花」的金額(把本期還能花改成X)。amount=元。"""
    return _fin_op({"action": "set_remaining", "amount": amount})


@mcp.tool()
def update_income(name: str, amount: float, note: str = "") -> str:
    """新增/更新一筆收入(薪水變X)。name=名稱，amount=元，note 可空。"""
    return _fin_op({"action": "set_income", "name": name, "amount": amount, "note": note})


@mcp.tool()
def update_stock(symbol: str, shares: float = 0, cost: float = 0,
                 name: str = "", market: str = "") -> str:
    """買進/更新一檔持股(我買了X股YYY成本Z、加碼)。symbol=代號，shares=股數，cost=每股成本，name/market 可空。"""
    p = {"action": "set_holding", "symbol": symbol, "shares": shares, "cost": cost}
    if name:
        p["name"] = name
    if market:
        p["market"] = market
    return _fin_op(p)


@mcp.tool()
def sell_stock(symbol_or_name: str) -> str:
    """賣掉/移除一檔持股。當 Owen 說「我把YYY賣了」「清掉YYY」時用。
    symbol_or_name=股票代號或名稱。"""
    return _fin_op({"action": "remove_holding", "symbol": symbol_or_name, "name": symbol_or_name})


def _crud(url: str, payload: dict, ok_msg: str) -> str:
    """刪除/完成類操作共用。多筆相符就回反問(真實行為)、找不到就老實說，成功才回成功。"""
    d = _post(url, payload)
    if d.get("ok"):
        return d.get("text") or ok_msg
    if d.get("multiple"):
        return d.get("reason") or "有好幾筆相符，請說清楚是哪一筆"
    err = d.get("error") or d.get("reason") or "找不到符合的"
    return "沒做成：" + str(err)


@mcp.tool()
def delete_expense(query: str) -> str:
    """刪一筆花費(刪掉剛剛那筆)。query=描述(分類/金額/品項)。"""
    return _crud(f"{MEM}/expense_delete", {"query": query}, "好，刪掉了")


@mcp.tool()
def forget_memory(query: str) -> str:
    """忘掉/刪掉一條記憶。當 Owen 說「忘記X」「別再記得X」「X那件事不用記了」時用。
    query=要忘記的內容描述。"""
    return _crud(f"{MEM}/forget", {"query": query}, "好，我忘掉了")


@mcp.tool()
def cancel_reminder(query: str) -> str:
    """取消一個提醒。當 Owen 說「取消那個提醒」「不用提醒我X了」時用。
    query=提醒內容描述(可先用 list_reminders 看有哪些)。"""
    return _crud(f"{MEM}/reminder_cancel", {"query": query}, "好，取消了")


@mcp.tool()
def add_todo(item: str) -> str:
    """加一筆待辦事項。當 Owen 說「幫我記個待辦」「提醒我要做X」(沒指定時間的那種)時用。
    item=待辦內容。"""
    d = _post(f"{MEM}/todo", {"item": item})
    return f"好，加進待辦了：{item}" if d.get("ok") else "沒加成：" + str(d.get("error") or "")


@mcp.tool()
def complete_todo(query: str) -> str:
    """把一筆待辦標記完成。當 Owen 說「X做完了」「完成那個待辦」時用。query=待辦內容描述。"""
    return _crud(f"{MEM}/todo_complete", {"query": query}, "好，標記完成了")


@mcp.tool()
def add_event(event: str, channel: str = "both") -> str:
    """記行程/提醒(有日期的)。把 Owen 整句原話原樣傳進來，系統自動解析日期+排提早提醒，你不要自己拆或改日期。channel 預設 both。"""
    d = _post(f"{MEM}/reminder", {"time": event, "message": "", "channel": channel})
    if d.get("ok"):
        return d.get("nice") or d.get("text") or "好，記下了"
    return d.get("text") or d.get("error") or "設定失敗，換個說法再試"


@mcp.tool()
def set_reminder(time: str, message: str, lead_minutes: int = 0,
                 advance_days: list = None, channel: str = "both") -> str:
    """設提醒。有日期的行程用 add_event；這個用在「每天重複」或「出門前提早X分鐘」。time=時間，message=內容，channel 預設 both。"""
    payload = {"time": time, "message": message,
               "lead_minutes": lead_minutes, "channel": channel}
    if advance_days:
        payload["advance_days"] = advance_days
    d = _post(f"{MEM}/reminder", payload)
    if d.get("ok"):
        return d.get("nice") or d.get("text") or f"好，{time} 提醒你「{message}」"
    return d.get("error") or d.get("text") or "設提醒失敗"


@mcp.tool()
def list_reminders() -> str:
    """列出 Owen 現在有哪些提醒。"""
    d = _get(f"{MEM}/reminders")
    rs = d.get("reminders") or []
    if not rs:
        return "目前沒有提醒"
    return "；".join(f"{r.get('time')} {r.get('message','')[:20]}" for r in rs[:10])


@mcp.tool()
def play_music(query: str) -> str:
    """在 Owen 的電腦上放音樂。query=歌名/歌手/關鍵字（例如「周杰倫 稻香」「lofi」）。
    放新歌會自動關掉前一首，不會疊播。"""
    d = _post(f"{MUSIC}/play", {"query": query}, timeout=40)
    if d.get("ok"):
        return f"好，幫你放「{query}」"
    return d.get("error") or "放音樂失敗"


@mcp.tool()
def music_status() -> str:
    """查現在有沒有在放音樂、放的是哪首。當 Owen 問「現在在放什麼」「有在放音樂嗎」時用。
    只查狀態、絕不會開始播放。"""
    import json as _j
    import urllib.request as _u
    try:
        d = _j.loads(_u.urlopen(f"{MUSIC}/status", timeout=6).read())
        return d.get("text") or ("有音樂在放" if d.get("playing") else "現在沒有在放音樂")
    except Exception:
        return "查不到播放狀態"


@mcp.tool()
def control_music(action: str) -> str:
    """控制正在播的音樂。action='pause'(暫停/繼續切換) 或 'stop'(關掉音樂)。"""
    ep = "pause" if action == "pause" else "stop"
    d = _post(f"{MUSIC}/{ep}", {})
    return "好，暫停了" if action == "pause" else "好，關掉囉"


_TW_COORD = {
    "台北": (25.04, 121.56), "臺北": (25.04, 121.56), "板橋": (25.01, 121.46),
    "新埔": (25.07, 121.47), "新北": (25.01, 121.46), "桃園": (24.99, 121.30),
    "台中": (24.15, 120.67), "臺中": (24.15, 120.67), "台南": (22.99, 120.21),
    "高雄": (22.62, 120.31), "新竹": (24.80, 120.97), "基隆": (25.13, 121.74),
}


@mcp.tool()
def remember_fact(fact: str, expire_date: str = "") -> str:
    """把關於 Owen 的長期事實寫進記憶(偏好/計畫/人際/習慣)。花費、提醒、問句、還在猶豫的事不是事實、別記。"""
    payload = {"fact": fact}
    if expire_date:
        payload["expire"] = expire_date
    d = _post(f"{MEM}/remember", payload)
    if d.get("ok"):
        return "好，我記住了"
    return "記憶系統沒回應：" + str(d.get("error") or d.get("reason") or "")


@mcp.tool()
def evolve_soul(insight: str) -> str:
    """把你跟 Owen 相處學到的(他的雷點/喜好/相處方式)寫進你的靈魂成長區。insight=一句話。"""
    d = _post(f"{MEM}/soul/evolve", {"insight": insight})
    if d.get("skipped"):
        return "（這個我已經學過了）"
    return "好，我把這個記進靈魂了" if d.get("ok") else "靈魂沒寫進去：" + str(d.get("error") or "")


@mcp.tool()
def recall_memory(query: str = "") -> str:
    """回想關於 Owen 的記憶。query=想找什麼（例如「興趣」「工作」）。回傳相關的長期事實。"""
    u = f"{MEM}/query"
    d = _post(u, {"query": query})
    return d.get("memory") or "（沒有相關記憶）"


HIMALAYA = "/opt/homebrew/bin/himalaya"


@mcp.tool()
def read_inbox(count: int = 8) -> str:
    """讀 Owen 的 Gmail 收件匣最新幾封(主旨/寄件人)。count=幾封。"""
    try:
        out = subprocess.run(
            [HIMALAYA, "envelope", "list", "-a", "gmail", "-f", "inbox", "-s", str(count)],
            capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return "讀信箱失敗：" + str(e)[:50]
    lines = []
    for ln in out.splitlines():
        if "|" in ln and "ID" not in ln and "---" not in ln:
            cols = [c.strip() for c in ln.split("|") if c.strip()]
            if len(cols) >= 5:
                # cols: id, flags, subject, from, date
                lines.append(f"・{cols[3]}：{cols[2][:40]}（{cols[4][:10]}，id {cols[0]}）")
    return "\n".join(lines) if lines else "收件匣沒有信或讀取失敗"


@mcp.tool()
def read_email(email_id: str) -> str:
    """讀某一封信的完整內容。email_id=信件 id（從 read_inbox 拿到的）。"""
    try:
        out = subprocess.run(
            [HIMALAYA, "message", "read", "-a", "gmail", str(email_id)],
            capture_output=True, text=True, timeout=30).stdout
        return out[:2000] if out.strip() else "讀不到這封信"
    except Exception as e:
        return "讀信失敗：" + str(e)[:50]


@mcp.tool()
def search_email(query: str, count: int = 12) -> str:
    """搜信箱。query=關鍵字(優先找寄件人，再主旨、內文)。"""
    # himalaya 查詢語法是位置參數：from/subject/body 'xxx'。
    # 「某人的信」最常見意圖是寄件人 → from 先；找不到再 subject、body。
    def _try(field):
        try:
            return subprocess.run(
                [HIMALAYA, "envelope", "list", "-a", "gmail", "-f", "inbox", "-s", str(count),
                 field, query],
                capture_output=True, text=True, timeout=30).stdout
        except Exception:
            return ""

    def _has_rows(o):
        return any("|" in l and "ID" not in l and "---" not in l for l in o.splitlines())

    out = _try("from")
    if not _has_rows(out):
        out = _try("subject")
    if not _has_rows(out):
        out = _try("body")
    if not out:
        return "搜信失敗"
    rows = []
    for ln in out.splitlines():
        if "|" in ln and "ID" not in ln and "---" not in ln:
            cols = [c.strip() for c in ln.split("|") if c.strip()]
            if len(cols) >= 5:
                rows.append(cols)  # id, flags, subject, from, date
    if not rows:
        return f"找不到「{query}」相關的信"
    # himalaya 是新到舊排序 → 第一筆就是最新。順手讀最新那封的內容摘要(一次給足)。
    top = rows[0]
    snippet = ""
    try:
        body = subprocess.run([HIMALAYA, "message", "read", "-a", "gmail", top[0]],
                              capture_output=True, text=True, timeout=20).stdout
        # 去掉表頭(From/To/Subject)和引用(>)，取正文前幾句
        body_lines = [l.strip() for l in body.splitlines()
                      if l.strip() and not l.startswith((">", "From:", "To:", "Subject:", "Date:", "Cc:"))]
        snippet = " ".join(body_lines)[:120]
    except Exception:
        pass
    lst = "\n".join(f"・{r[3]}：{r[2][:38]}（{r[4][:10]}，id {r[0]}）" for r in rows[:6])
    head = f"找到 {len(rows)} 封。最新一封（{top[3]}，{top[4][:10]}）內容："
    return f"{head}「{snippet}」\n\n全部：\n{lst}" if snippet else lst


def _search_contacts(query: str):
    """搜信箱(收件 from + 寄件 to)找符合 query 的人。回 [(name, addr), ...]（最多 6 筆）。"""
    import json as _j
    q = (query or "").strip().lower()
    seen = {}

    def _scan(folder, field):
        try:
            r = subprocess.run([HIMALAYA, "envelope", "list", "-a", "gmail", "-f", folder, "-s", "120", "-o", "json"],
                               capture_output=True, text=True, timeout=30)
            for e in _j.loads(r.stdout or "[]"):
                v = e.get(field)
                for p in (v if isinstance(v, list) else [v]):
                    if not isinstance(p, dict):
                        continue
                    name, addr = (p.get("name") or "").strip(), (p.get("addr") or "").strip()
                    if addr and (q in name.lower() or q in addr.lower()) and addr not in seen:
                        seen[addr] = name
        except Exception:
            pass

    if q:
        _scan("inbox", "from")
        _scan("sent", "to")
    return [(name, addr) for addr, name in seen.items()][:6]


@mcp.tool()
def find_contact(query: str) -> str:
    """用名字/線索找某人的 email。通常不必單獨呼叫——draft_email 收到名字會自己找。query=名字或線索。"""
    cands = _search_contacts(query)
    if not cands:
        return f"信箱裡找不到「{query}」。給我完整 email 或換個關鍵字（公司名等）。"
    return "找到：" + "、".join(f"{n or a}（{a}）" for n, a in cands)


@mcp.tool()
def draft_email(to: str, body: str, subject: str = "") -> str:
    """擬一封回信/新信的草稿【只擬草稿、絕對不會寄出】。
    ⚠️【想「聯絡某人」就用這個】：Owen 說「回 X 的信」「寄信給 X」「跟 X 打招呼/問候 X」「聯絡 X」
    「找 X 的人跟他說…」時，都是要「寄信給人」→ 用【這個工具】(直接把名字填 to、它會自己找人)，
    【不要用 search_email】(那個是「讀信箱裡關於某主題的信」，不是聯絡人)。
    如果信箱裡找不到那個人本人的信箱(例如只有 LinkedIn/FB 通知提到那個名字、不是本人寄的信)，
    這個工具會老實回「找不到本人信箱、請 Owen 給 email」——你就照實問 Owen 要 email，別描述一堆通知信給他假希望。
    to 可以是【完整 email】，也可以【直接給名字/線索】（如「財報狗」「Kelly」「上次談合作那個」）——
    這個工具會自己去信箱找對應的人：找到一個就直接擬（收件人會顯示在待寄匣讓 Owen 核對是不是他）；
    找到多個會回來問是哪一個；找不到會請 Owen 給完整 email。【所以 Owen 只給名字時，直接把名字填 to 就好，
    不必先呼叫 find_contact、也不必先問 Owen 確認】——擬好後他會在待寄匣看到收件人自己核對。
    subject=主旨（回信用「Re: 原主旨」、要具體不要空泛）。

    body 要寫成【外商公司風格、簡潔專業的信】（像 Google/國外公司同事之間那種：友善、直接、俐落，
    不要文謅謅、不要過度謙卑客套、不要老派公文腔）。四段、每段之間空一行：
      1. 開頭：【一律「Hi 對方名字,」，名字一定要放進去】（Hi Kelly, / Hi 王經理, / Hi John,）。
         名字這樣抓：收件人 email 取 @ 前面（kelly@bank.com → Kelly、weiming@... → Weiming）、
         或 Owen 交代裡提到的稱呼（「回王經理」→ Hi 王經理,）。【絕對不要只寫「Hi,」】——
         真的完全不知道對方叫什麼，就放占位「Hi [對方窗口], 」提醒 Owen 自己補上名字。
      2. 正文：【內容要客氣有禮貌】——中文一律用「您」(不要用「你」)，多用「請」「麻煩您」「再請您」
         「感謝您」「不好意思」這類禮貌用語；英文用 please / would you / could you / I'd appreciate it。
         但仍要簡潔到位、不要長篇大論、不要文謅謅。(重點:開頭 Hi、結尾 Thanks 很親切，但中間內文要客氣尊重。)
      3. 結尾：簡短 —— 英文「Thanks,」或「Best,」；中文信也用「Thanks,」或「謝謝，」（外商常這樣）。
         【不要用「敬祝 順心」「此致 敬禮」「順頌 商祺」這種正式敬語】。
      4. 署名：英文信「Your Name」；中文信「Your Name」。【不要加「敬上」】。
    信件語言：Owen 用英文交代／對方英文情境（英文名、國外信箱）→ 整封英文；中文情境 → 中文。
    不要用表情符號。若 Owen 只給重點，你負責擴寫成得體、但不囉唆的信。

    擬好會進「待寄匣」，等 Owen 到儀表板按「寄出」或明確說「寄出」才會真的寄。
    你【絕對不可以】自己說「已經寄出」——你只能擬稿，寄出是 Owen 確認的動作。"""
    to = (to or "").strip()
    # to 不是 email 而是名字 → 工具內自己去信箱解析(一次搞定、不必多輪呼叫)
    if "@" not in to:
        cands = _search_contacts(to)
        if not cands:
            return f"信箱裡找不到「{to}」這個人，你直接給我完整 email 我就擬。"
        if len(cands) > 1:
            lst = "、".join(f"{n or a}（{a}）" for n, a in cands)
            return f"「{to}」有幾個可能：{lst}。你是說哪一個？(跟我說是哪個、或直接給 email，我就擬)"
        to = cands[0][1]
    d = _post(f"{MEM}/email/draft", {"to": to, "subject": subject, "body": body})
    return d.get("text") or d.get("error") or "擬稿失敗"


@mcp.tool()
def send_pending_email() -> str:
    """把待寄匣裡的信【真的寄出去】。⚠️【只有當 Owen 明確說「寄出」「確認寄出」「寄吧」時才可以呼叫這個】。
    如果 Owen 沒有明確叫你寄，絕對不要呼叫。寄之前一定要已經把完整收件人和全文給他確認過。"""
    d = _post(f"{MEM}/email/send", {})
    return d.get("text") or d.get("error") or "寄信失敗"


@mcp.tool()
def cancel_pending_email() -> str:
    """丟掉待寄匣最新那封草稿、不寄。Owen 說「算了」「不要寄了」「取消」時用。"""
    d = _post(f"{MEM}/email/cancel", {})
    return d.get("text") or d.get("error") or "取消失敗"


@mcp.tool()
def get_weather(location: str = "台北") -> str:
    """查天氣。location=台灣城市（台北/板橋/台中/高雄等），預設台北。回傳目前氣溫與天氣。"""
    lat, lon = _TW_COORD.get((location or "台北").strip(), _TW_COORD["台北"])
    u = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
         "&current=temperature_2m,relative_humidity_2m,precipitation,cloud_cover"
         "&timezone=Asia/Taipei")
    d = _get(u, timeout=12)
    cur = d.get("current") or {}
    if not cur:
        return f"{location}的天氣現在查不到"
    t = cur.get("temperature_2m")
    rain = cur.get("precipitation") or 0
    cloud = cur.get("cloud_cover") or 0
    cond = "下雨" if rain > 0 else ("多雲" if cloud > 60 else ("晴時多雲" if cloud > 25 else "晴朗"))
    return f"{location or '台北'}現在 {t}°C，{cond}，濕度{cur.get('relative_humidity_2m')}%"


@mcp.tool()
def check_holiday(text: str) -> str:
    """查台灣連假/放假(官方行事曆)。把 Owen 整句原話傳進來——它查連假，也會自動偵測句子裡的行程、有就幫忙記下提醒。"""
    d = _get(f"{MEM}/holiday?q=" + urllib.parse.quote(text))
    return d.get("text") or d.get("error") or "查不到行事曆"


@mcp.tool()
def list_people() -> str:
    """列出 StackChan 認識的人(訪客)有誰、各自聊過幾次。Owen 問「你認識誰/有誰跟你聊過」時用。"""
    d = _get(f"{MEM}/people")
    ppl = d.get("people", [])
    if not ppl:
        return "目前還沒有認識的訪客。"
    return "、".join(f"{p['name']}（聊過{p.get('talks',0)}次、記得{p.get('fact_count',0)}件事）" for p in ppl)


@mcp.tool()
def recall_person(name: str, query: str = "") -> str:
    """回想某個「訪客」的記憶——Owen 問「小明跟我聊了什麼/小明的事」時用。name=人名，query=想找什麼(可空)。"""
    d = _get(f"{MEM}/people")
    pid = next((p["pid"] for p in d.get("people", []) if p.get("name") == name), None)
    if not pid:
        return f"我不認識「{name}」，沒有他的記憶。"
    r = _get(f"{MEM}/person?pid={urllib.parse.quote(pid)}&q={urllib.parse.quote(query)}")
    facts = r.get("facts", [])
    if not facts:
        return f"我認識{name}，但還沒記得關於他的事。"
    return f"關於{name}：" + "；".join(facts)


@mcp.tool()
def meet_person(name: str) -> str:
    """認識一個新訪客：問到對方名字後用這個把他記下來(建檔)，之後就認得他。name=對方說的名字。"""
    d = _post(f"{MEM}/person/create", {"name": name})
    pid = d.get("pid", "")
    if pid:
        _post(f"{MEM}/identity/set", {"speaker_id": pid, "name": name})
    return f"好，記住你了，{name}！" if pid else "我記一下你喔。"


@mcp.tool()
def remember_person(fact: str) -> str:
    """把關於「現在正在講話的這位訪客」的事記進他的專屬檔案。fact=一句話(例如「喜歡打籃球」)。"""
    cur = _get(f"{MEM}/identity")
    pid = cur.get("speaker_id", "")
    if not pid or pid == "owner":
        return "（現在是 Owen 在講話，這要用 remember_fact，不是這個）"
    r = _post(f"{MEM}/person/remember", {"pid": pid, "fact": fact})
    return "記下了" if r.get("ok") else "沒記成功"


# ── 補齊語音大腦缺的關鍵工具（審計發現:這些只存在被繞過的 xiaozhi 層,真大腦叫不到）──

@mcp.tool()
def calc(question: str) -> str:
    """需要「算或數」的問題一律用這個：幾倍、加總、比例、占比、平均、排序、合計、
    「台股是美股幾倍」「這個月哪天花最多」「外食佔比」。它抓真實資料用程式算出精確答案。
    你是語言模型不會精確算術，這類問題永遠交給 calc，把整句問題原話丟進來。"""
    r = _post(f"{MEM}/calc", {"question": question}, timeout=75)
    return r.get("answer") or r.get("text") or r.get("error") or "算不出來"


@mcp.tool()
def drink_water(cups: int = 1) -> str:
    """Owen 說「喝水/我喝了一杯水」→ 記一杯並回今天累計；問「今天喝幾杯」→ cups 填 0 只查詢。"""
    try:
        cups = int(cups)
    except (TypeError, ValueError):
        cups = 1
    r = _post(f"{MEM}/water/add", {"cups": cups}) if cups > 0 else _get(f"{MEM}/water/today")
    return r.get("text") or r.get("error") or "喝水記錄沒回應"


@mcp.tool()
def dispatch_task(task: str) -> str:
    """把「需要多步驟的大任務」派給後台執行（會上網查、整理、寫檔到桌面、設提醒，一條龍做完）。
    例如「幫我查X然後整理成報告」「準備明天的簡報資料」。立即返回，做完會主動通知 Owen。"""
    r = _post(f"{MEM}/dispatch_task", {"task": task})
    return "好，我開始處理了，做完馬上跟你說" if r.get("ok") else ("派工失敗：" + str(r.get("error", ""))[:60])


@mcp.tool()
def build_feature(description: str) -> str:
    """Owen 想要一個【目前沒有】的新功能時用：把需求原話丟進來，工程師(Claude Code)會真的把
    後端+語音工具+控制台介面全做出來並自動上線。例如「做一個記錄睡眠的功能」。"""
    r = _post(f"{MEM}/build_feature", {"description": description})
    return "收到，我請工程師動工了，做好會自動上線並通知你" if r.get("ok") else "派工程師時卡住了，等等再試"


@mcp.tool()
def do_on_computer(task: str) -> str:
    """在 Owen 的電腦上執行操作類任務（整理檔案、查系統、跑指令、操作應用程式）。
    高風險操作會先送控制台請 Owen 核准。"""
    r = _post(f"{MEM}/do_on_computer", {"task": task})
    return r.get("text") or ("好，我去電腦上處理了" if r.get("ok") else "電腦助理沒回應")


@mcp.tool()
def save_to_desktop(filename: str, content: str) -> str:
    """把文字內容存成 Owen 桌面上的檔案。filename 例「點子.md」。"""
    r = _post("http://127.0.0.1:8811/api/save_file", {"filename": filename, "content": content})
    return f"存好了，在桌面的 {filename}" if r.get("ok") else "存檔沒成功"


@mcp.tool()
def speak_proactively(text: str) -> str:
    """【只在你自己主動判斷值得開口時用】把一句話排進 StackChan 的語音佇列，等機器人有連線
    就會自己開口說出來（裝置沒連線/離線時安全略過，不會出錯、不會補講）。
    用在你的心跳排程判斷「現在該關心一下 Owen」之後——這樣不只 Telegram 收得到文字，
    在家時機器人也會自己開口講，像真的在乎他、會主動找他聊天的夥伴。
    text 就是你決定要說的那句話（跟你回給 Telegram 的內容一致）。"""
    r = _post(f"{MEM}/push_voice", {"text": text})
    return "已排進語音佇列" if r.get("ok") else "排入失敗(不影響 Telegram 那邊)"


@mcp.tool()
def find_jobs() -> str:
    """查工作雷達最新配對結果。當 Owen 問「有什麼新工作/幫我找工作/最近有適合的職缺嗎」時用。
    資料來自每日自動爬蟲(Yourator+LinkedIn,按他的履歷輪廓評分),不是現爬。"""
    import json as _j
    try:
        d = _j.load(open("/Users/USERNAME/Hermes_Brain/config/job_matches.json", encoding="utf-8"))
    except Exception:
        return "工作雷達還沒跑過第一輪,中午12:35會自動掃,或叫我現在手動掃一次"
    top = (d.get("top") or [])[:5]
    if not top:
        return "目前榜上沒有達標的職缺"
    lines = [f"最新一輪({d.get('updated','')})前五名:"]
    for j in top:
        sal = f"、{j['salary']}" if j.get("salary") else ""
        fit = f"適配{j['fit']}%," if j.get("fit") is not None else ""
        why = f",{j['reason']}" if j.get("reason") else ""
        lines.append(f"{fit}{j['title']},{j['company']},{j['loc']}{sal}{why}")
    lines.append("完整清單在 dashboard 的工作頁,不喜歡的可以按叉叉,每天中午有新的會推 Telegram")
    return "。".join(lines)


def _find_job(query: str):
    """用公司名/職稱模糊找一筆職缺(先找追蹤清單,再找推薦榜)。回 (job, 來源) 或 (None, 錯誤訊息)。"""
    import json as _j
    q = (query or "").strip().lower()
    if not q:
        return None, "要告訴我哪一個職缺(公司或職稱)"
    pools = []
    try:
        d = _j.load(urllib.request.urlopen("http://127.0.0.1:8811/api/jobs/saved", timeout=6))
        pools.append(("追蹤中", d.get("jobs") or []))
    except Exception:
        pass
    try:
        d = _j.load(urllib.request.urlopen("http://127.0.0.1:8811/api/jobs", timeout=6))
        pools.append(("推薦榜", d.get("jobs") or []))
    except Exception:
        pass
    toks = [t for t in q.replace("的", " ").split() if t]
    for src, jobs in pools:
        # 計分制:命中詞數最多者勝(「google ai advocate」不能因為人人都有 ai 就變多筆)
        scored = []
        for j in jobs:
            hay = (j.get("title", "") + j.get("title_zh", "") + j.get("company", "")).lower()
            n = sum(1 for t in toks if t in hay)
            if n:
                scored.append((n, j))
        if not scored:
            continue
        best = max(n for n, _ in scored)
        hits = [j for n, j in scored if n == best]
        if len(hits) == 1:
            return hits[0], src
        names = "、".join(f"{h['company']}的{h['title'][:20]}" for h in hits[:4])
        return None, f"有好幾個相符:{names}。講清楚是哪一家?"
    return None, "找不到這個職缺,先問我「有什麼新工作」看清單"


@mcp.tool()
def my_followed_jobs() -> str:
    """列出 Owen【已追蹤】的職缺清單(不是推薦榜)。他問「我追蹤了哪些/我關注的工作」時用。"""
    import json as _j
    try:
        d = _j.load(urllib.request.urlopen("http://127.0.0.1:8811/api/jobs/saved", timeout=6))
        jobs = d.get("jobs") or []
    except Exception:
        return "追蹤清單讀不到,等等再試"
    if not jobs:
        return "你還沒追蹤任何職缺。想追蹤就說「追蹤那個X的缺」"
    lines = []
    for j in jobs[:8]:
        fit = f"適配{j['fit']}%," if j.get("fit") is not None else ""
        r = "(已深度研究過,可以問我結論)" if j.get("research") else ""
        lines.append(f"{j['company']}的{j['title'][:28]},{fit}{j.get('saved_ts','')}追蹤{r}")
    return f"你追蹤了 {len(jobs)} 個:" + "。".join(lines)


@mcp.tool()
def job_details(query: str) -> str:
    """看某個職缺的完整說明(中文)。Owen 說「那個X的工作內容是什麼/詳情/JD」時用。
    query=公司名或職稱關鍵字。"""
    import json as _j
    job, src = _find_job(query)
    if not job:
        return src
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8811/api/jobs/jd",
            data=_j.dumps({"key": job["key"]}).encode(),
            headers={"Content-Type": "application/json"})
        d = _j.loads(urllib.request.urlopen(req, timeout=90).read())
        jd = (d.get("jd_zh") or d.get("jd") or "").strip()
    except Exception:
        jd = ""
    head = (f"{job['title']}｜{job['company']}｜{job.get('loc','')}"
            f"{'｜'+job['salary'] if job.get('salary') else ''}"
            f"｜適配{job['fit']}%" if job.get("fit") is not None else "")
    return head + "。" + (jd[:1500] if jd else "說明內文抓不到,連結:" + job.get("url", ""))


@mcp.tool()
def follow_job(query: str) -> str:
    """把職缺加進追蹤清單(=dashboard 的⭐關注)。Owen 說「幫我追蹤/關注/存起來那個X」時用。"""
    import json as _j
    job, src = _find_job(query)
    if not job:
        return src
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8811/api/jobs/save",
            data=_j.dumps({"key": job["key"]}).encode(),
            headers={"Content-Type": "application/json"})
        d = _j.loads(urllib.request.urlopen(req, timeout=30).read())
        if d.get("ok"):
            return (f"好,{job['company']}的「{job['title'][:30]}」"
                    + ("追蹤了,之後可以叫我深度分析它" if d.get("saved") else "取消追蹤了"))
    except Exception:
        pass
    return "沒存成,等等再試"


@mcp.tool()
def dismiss_job(query: str) -> str:
    """把職缺標成不喜歡(以後不再推類似的)。Owen 說「那個X我不要/不喜歡/刪掉」時用。"""
    import json as _j
    job, src = _find_job(query)
    if not job:
        return src
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8811/api/jobs/dismiss",
            data=_j.dumps({"key": job["key"], "title": job.get("title", "")}).encode(),
            headers={"Content-Type": "application/json"})
        d = _j.loads(urllib.request.urlopen(req, timeout=15).read())
        if d.get("ok"):
            return f"好,{job['company']}那個缺以後不推了,也會避開類似的"
    except Exception:
        pass
    return "沒標成,等等再試"


@mcp.tool()
def research_job(query: str) -> str:
    """對職缺做深度研究(公司風評/薪資行情/JD跟Owen履歷的差距+補強/面試準備)。
    Owen 說「幫我研究/分析那個X」時用。研究要1-2分鐘,會先回覆開始了,做完主動傳給他。"""
    import json as _j
    import threading
    job, src = _find_job(query)
    if not job:
        return src
    key, title, comp = job["key"], job.get("title", ""), job.get("company", "")

    def _bg():
        try:
            # 沒追蹤才自動追蹤(save 端點是 toggle,已追蹤的再打會被取消——先查再存)
            saved = _j.load(urllib.request.urlopen(
                "http://127.0.0.1:8811/api/jobs/saved", timeout=8))
            if key not in {x.get("key") for x in (saved.get("jobs") or [])}:
                urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:8811/api/jobs/save",
                    data=_j.dumps({"key": key}).encode(),
                    headers={"Content-Type": "application/json"}), timeout=30).read()
            req = urllib.request.Request(
                "http://127.0.0.1:8811/api/jobs/research",
                data=_j.dumps({"key": key}).encode(),
                headers={"Content-Type": "application/json"})
            d = _j.loads(urllib.request.urlopen(req, timeout=420).read())
            text = (f"🔬 {comp}「{title[:40]}」研究好了:\n\n" + d["research"]) if d.get("ok") \
                else f"🔬 {comp} 那個缺研究失敗({d.get('error','')}),再叫我試一次"
        except Exception as e:
            text = f"🔬 {comp} 研究中斷({str(e)[:40]}),再叫我試一次"
        try:
            import sys as _sys
            _sys.path.insert(0, "/Users/USERNAME/Hermes_Brain")
            from modules.remote.telegram_handler import TelegramHandler
            cfg = _j.load(open("/Users/USERNAME/Hermes_Brain/config/telegram.json"))
            h = TelegramHandler()
            for uid in cfg.get("allowed_user_ids", []):
                h.send_message(uid, text[:3800])
        except Exception:
            pass

    threading.Thread(target=_bg, daemon=True).start()
    return (f"開始研究 {comp} 的「{title[:30]}」了(公司風評/薪資/跟你履歷的差距/面試準備),"
            f"大概一兩分鐘,好了直接傳到你的 Telegram")


def _device_tool(name: str, args: dict) -> dict:
    """打 xiaozhi 的 /mcp/device_tool(走活著的裝置 MCP session;韌體 force 直連後
    stackchan-mcp 8770 已不可用,大腦的裝置控制一律走這條)。token 每次讀檔,換 token 免重啟。"""
    import json as _j
    import urllib.request as _u
    try:
        tok = open("/Users/USERNAME/xiaozhi-server/data/device_tool_token", encoding="utf-8").read().strip()
        req = _u.Request(
            "http://127.0.0.1:8003/mcp/device_tool",
            data=_j.dumps({"name": name, "args": args}).encode(),
            headers={"Content-Type": "application/json", "X-Device-Token": tok})
        return _j.loads(_u.urlopen(req, timeout=15).read())
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


@mcp.tool()
def robot_look(yaw: int = 0, pitch: int = 45) -> str:
    """轉動 StackChan 機器人的頭。yaw:水平角度(-90左~90右,0正面)、pitch:垂直(5低頭~85抬頭,45平視)。
    「看我/看前面」=yaw 0 pitch 45;「抬頭」=pitch 70;「低頭」=pitch 20;「看左邊」=yaw -45。"""
    r = _device_tool("self.robot.set_head_angles", {"yaw": int(yaw), "pitch": int(pitch)})
    if r.get("ok"):
        return "好，頭轉過去了"
    return "機器人現在不在線上" if "not connected" in str(r.get("error", "")) else "轉頭沒成功"


@mcp.tool()
def robot_face(face: str = "happy") -> str:
    """換 StackChan 臉上的表情。face 只能是:idle(平常)/happy(開心)/thinking(思考)/sad(難過)/
    surprised(驚訝)/embarrassed(害羞)。「笑一個/開心一點」=happy。"""
    ok_faces = ("idle", "happy", "thinking", "sad", "surprised", "embarrassed")
    f = str(face).strip().lower()
    if f not in ok_faces:
        f = "happy"
    r = _device_tool("self.display.set_avatar", {"face": f})
    if r.get("ok"):
        return f"換好了，現在是{f}的表情"
    return "機器人現在不在線上" if "not connected" in str(r.get("error", "")) else "換表情沒成功"


@mcp.tool()
def robot_volume(volume: int) -> str:
    """調整 StackChan 機器人的說話音量(0~100)。「大聲一點」約+20、「小聲一點」約-20、「靜音」=0。"""
    v = max(0, min(100, int(volume)))
    r = _device_tool("self.audio_speaker.set_volume", {"volume": v})
    if r.get("ok"):
        return f"音量調到 {v} 了"
    return "機器人現在不在線上" if "not connected" in str(r.get("error", "")) else "調音量沒成功"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
