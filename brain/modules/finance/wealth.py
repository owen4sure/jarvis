"""Jarvis 財富管理核心：每月收入/固定開銷/生活開銷/預算/投資組合(即時股價)。

資料：
- config/finance.json  → income / fixed / budget / holdings
- config/expenses.json → 生活開銷(沿用既有 expense_tracker 格式)

幣別：台股以 TWD、美股以 USD 計價；總覽一律換算成 TWD（抓 USDTWD=X 即時匯率）。
"""
import json
import os
import re
import time
import threading
import urllib.request
import datetime
import zoneinfo

_DIR = "/Users/USERNAME/Hermes_Brain/config"
FIN = os.path.join(_DIR, "finance.json")
EXP = os.path.join(_DIR, "expenses.json")
TZ = zoneinfo.ZoneInfo("Asia/Taipei")
_UA = {"User-Agent": "Mozilla/5.0"}
_lock = threading.Lock()
_quote_cache = {}   # symbol -> (ts, {price,prev,currency})
_last_good = {}     # symbol -> (ts, quote)：最後一次成功抓到的報價；Yahoo 掛掉時沿用，絕不把該檔當 0


# ---------- 資料讀寫 ----------
def load():
    try:
        with open(FIN, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("income", [])
    d.setdefault("fixed", [])
    d.setdefault("budget", {"save_goal": 0, "spend_limit": 0})
    d.setdefault("holdings", [])
    d.setdefault("invest_log", [])   # 每月固定投入投資的記錄 [{month, amount}]
    d.setdefault("cash", 0)          # 銀行/現金存款（讓淨資產準確）
    d.setdefault("fire_target", 0)   # 財務自由目標金額（0=自動用年開銷×25估算）
    d.setdefault("fire_annual", 0)   # FIRE 年開銷（手動設；0=自動用固定開銷估）
    d.setdefault("last_milestone", 0)  # 已慶祝過的淨資產里程碑（每 10 萬一關，防重複）
    d.setdefault("payday", 15)          # 發薪日（每月幾號）→ 財務週期以此為準
    d.setdefault("category_budgets", {})
    return d


def _cycle(payday=None):
    """目前發薪週期 (start_str, end_str, label)。預設每月 15 號發薪 → 週期 15號~下月14號。"""
    d = load()
    try:
        pd = int(payday if payday is not None else d.get("payday", 15) or 15)
    except (TypeError, ValueError):
        pd = 15
    pd = max(1, min(28, pd))   # 夾在 1–28，避免 31號/負數讓 replace(day=) 爆掉
    now = datetime.datetime.now(TZ)
    if now.day >= pd:
        start = now.replace(day=pd, hour=0, minute=0, second=0, microsecond=0)
    else:
        first = now.replace(day=1)
        prev_last = first - datetime.timedelta(days=1)
        start = prev_last.replace(day=min(pd, prev_last.day))
    # 下個週期起點 = 下個月的 payday，end = 前一天
    nm = (start.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)
    try:
        nxt = nm.replace(day=pd)
    except ValueError:
        nxt = nm.replace(day=1)
    end = nxt - datetime.timedelta(days=1)
    return (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
            "%d/%d–%d/%d" % (start.month, start.day, end.month, end.day))


def save(d):
    with _lock:
        os.makedirs(_DIR, exist_ok=True)
        tmp = FIN + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FIN)


def _next_id(items):
    return (max([x.get("id", 0) for x in items], default=0) + 1)


def set_list(key, items):
    """整批覆寫 income / fixed / holdings（dashboard 編輯後送完整清單）。"""
    d = load()
    clean = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        it = dict(it)
        it.setdefault("id", _next_id(clean))
        if not it.get("id"):
            it["id"] = _next_id(clean)
        clean.append(it)
    d[key] = clean
    save(d)
    return clean


def set_budget(save_goal, spend_limit):
    d = load()
    d["budget"] = {"save_goal": _num(save_goal), "spend_limit": _num(spend_limit)}
    save(d)
    return d["budget"]


# ---------- 精準單項更新（給語音/Telegram 即時改用）----------
def set_budget_field(field, amount):
    """只改 budget 的一個欄位（save_goal / spend_limit），其他不動。"""
    d = load()
    b = dict(d.get("budget", {}) or {})
    b.setdefault("save_goal", 0)
    b.setdefault("spend_limit", 0)
    if field in ("save_goal", "spend_limit"):
        b[field] = _num(amount)
    d["budget"] = b
    save(d)
    return b


def set_top_field(field, amount):
    """設定頂層數字欄位（cash / fire_target / fire_annual / payday 發薪日）。"""
    if field not in ("cash", "fire_target", "fire_annual", "payday"):
        raise ValueError("只能設 cash / fire_target / fire_annual / payday")
    d = load()
    if field == "payday":
        d[field] = int(max(1, min(28, _num(amount) or 15)))
    else:
        d[field] = _num(amount)
    save(d)
    return d[field]


def upsert_named(key, name, amount, note=None):
    """新增或更新 income/fixed 裡名稱相符的項目（語音「薪水改成X」用）。"""
    if key not in ("income", "fixed"):
        raise ValueError("key 只能是 income 或 fixed")
    d = load()
    items = [dict(x) for x in d.get(key, [])]
    n = str(name).strip()
    hit = None
    for it in items:
        if str(it.get("name", "")).strip() == n:
            hit = it
            break
    if hit is None:  # 找不到精確相符就找包含
        for it in items:
            if n and n in str(it.get("name", "")):
                hit = it
                break
    if hit is None:
        hit = {"id": _next_id(items), "name": n, "amount": _num(amount)}
        if note:
            hit["note"] = note
        items.append(hit)
    else:
        hit["amount"] = _num(amount)
        if note is not None:
            hit["note"] = note
    d[key] = items
    save(d)
    return hit


def remove_named(key, name):
    """刪掉 income/fixed 裡名稱包含 name 的項目。回傳刪了幾筆。"""
    d = load()
    n = str(name).strip()
    before = d.get(key, [])
    after = [it for it in before if n and n not in str(it.get("name", ""))]
    d[key] = after
    save(d)
    return len(before) - len(after)


def upsert_holding(symbol=None, market=None, name=None, shares=None, cost=None):
    """新增或更新一檔持股。優先用代號比對，沒帶代號時用名稱比對（語音「台積電改成150股」）。
    只帶到的欄位才改。新增一定要有代號（否則抓不到報價）。"""
    d = load()
    hs = [dict(x) for x in d.get("holdings", [])]
    sym = str(symbol or "").upper().strip()
    nm = str(name or "").strip()
    hit = None
    if sym:
        for h in hs:
            if str(h.get("symbol", "")).upper().strip() == sym:
                hit = h
                break
    # 只有「完全沒給代號」時才用名稱比對（語音「台積電改成150股」）。
    # 若有給代號卻找不到，代表是新持股，要新建，不可用名稱亂比對（避免「元大台灣50」誤中「元大台灣50正2」）。
    if hit is None and nm and not sym:
        for h in hs:
            if str(h.get("name", "")).strip() == nm:   # 先精確相符
                hit = h
                break
        if hit is None:
            for h in hs:                               # 再包含相符
                if nm in str(h.get("name", "")):
                    hit = h
                    break
    if hit is None:
        if not sym:
            raise ValueError("要新增持股請給股票代號（例如台積電是 2330）")
        hit = {"id": _next_id(hs), "symbol": sym, "market": (market or "TW").upper(),
               "name": name or "", "shares": _num(shares), "cost": _num(cost)}
        hs.append(hit)
    else:
        if market is not None:
            hit["market"] = str(market).upper()
        if name is not None:
            hit["name"] = name
        if shares is not None:
            hit["shares"] = _num(shares)
        if cost is not None:
            hit["cost"] = _num(cost)
    d["holdings"] = hs
    save(d)
    return hit


def remove_holding(match):
    """刪掉代號或名稱包含 match 的持股。回傳刪了幾筆。"""
    d = load()
    m = str(match).upper().strip()
    before = d.get("holdings", [])
    after = [h for h in before
             if m and m not in str(h.get("symbol", "")).upper()
             and m not in str(h.get("name", "")).upper()]
    d["holdings"] = after
    save(d)
    return len(before) - len(after)


def _num(v, default=0.0):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else default
    except Exception:
        return default


# ---------- 生活開銷（可編輯/刪除）----------
def load_expenses():
    try:
        with open(EXP, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("expenses", []) if isinstance(d, dict) else (d or [])
    except Exception:
        return []


def set_expenses(items):
    """整批覆寫生活開銷（讓使用者能刪掉亂寫的）。"""
    with _lock:
        try:
            with open(EXP, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {"expenses": [], "next_id": 1}
        if not isinstance(d, dict):
            d = {"expenses": [], "next_id": 1}
        clean = []
        for e in (items or []):
            if not isinstance(e, dict):
                continue
            amt = _num(e.get("amount"))
            if amt <= 0:
                continue
            clean.append({"amount": amt, "category": str(e.get("category", "其他"))[:30],
                          "note": str(e.get("note", ""))[:80], "date": e.get("date") or _today()})
        d["expenses"] = clean
        os.makedirs(_DIR, exist_ok=True)
        tmp = EXP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, EXP)
        return clean


def _today():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


# ---------- 即時股價 ----------
def _yahoo_symbol(h):
    s = str(h.get("symbol", "")).upper().strip()
    mk = (h.get("market") or "").upper()
    if mk == "TW" and not s.endswith(".TW") and not s.endswith(".TWO"):
        s += ".TW"
    return s


_QUOTE_TTL = 25   # 報價快取秒數（越短越即時，但別狂打）
_twse_cache = {}  # code -> (ts, {price,prev,traded_today,currency,name})


def _first_num(s):
    """證交所欄位像 '107.25_107.20_...'，取第一個有效數字。"""
    try:
        for part in str(s).split("_"):
            if part and part != "-":
                return float(part)
    except Exception:
        pass
    return None


def _twse_quotes(codes):
    """證交所 MIS 即時報價（台股，幾乎零延遲，免費）。回 {code:{price,prev,...}}。
    先試上市 tse_，找不到的再試上櫃 otc_。有 25 秒快取。"""
    out = {}
    now = time.time()
    need = []
    for c in codes:
        cc = _twse_cache.get(c)
        if cc and now - cc[0] < _QUOTE_TTL:
            out[c] = cc[1]
        else:
            need.append(c)
    if not need:
        return out

    def query(prefix, syms):
        ch = "|".join("%s_%s.tw" % (prefix, s) for s in syms)
        url = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=" + ch
               + "&json=1&delay=0&_=" + str(int(now * 1000)))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp"})
        d = json.load(urllib.request.urlopen(req, timeout=8))
        today = datetime.datetime.now(TZ).strftime("%Y%m%d")
        for s in d.get("msgArray", []):
            code = s.get("c")
            # 現價：成交價 z → 最佳賣價 a → 最佳買價 b → 開盤 o
            price = _first_num(s.get("z"))
            if price is None:
                price = _first_num(s.get("a")) or _first_num(s.get("b")) or _first_num(s.get("o"))
            prev = _first_num(s.get("y"))
            traded = (s.get("d") == today)   # 最近交易日是今天 = 今天有開盤
            if code and price is not None:
                rec = {"price": price, "prev": prev, "currency": "TWD",
                       "traded_today": traded, "name": s.get("n", "")}
                out[code] = rec
                _twse_cache[code] = (now, rec)

    try:
        query("tse", need)
    except Exception:
        pass
    miss = [c for c in need if c not in out]
    if miss:
        try:
            query("otc", miss)
        except Exception:
            pass
    return out


def _quote(sym):
    if not sym:
        return {"price": None, "prev": None, "currency": None, "traded_today": False}
    now = time.time()
    c = _quote_cache.get(sym)
    if c and now - c[0] < _QUOTE_TTL:
        return c[1]
    out = {"price": None, "prev": None, "currency": None, "traded_today": False, "name": ""}
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + sym
               + "?interval=1d&range=5d")
        req = urllib.request.Request(url, headers=_UA)
        d = json.load(urllib.request.urlopen(req, timeout=10))
        m = d["chart"]["result"][0]["meta"]
        price = m.get("regularMarketPrice")
        # 昨收：優先用 Yahoo 官方 previousClose；沒有就用【日線倒數第二根收盤】= 真正昨天收盤。
        # ★絕對不要退回 chartPreviousClose★——那是「查詢範圍最前面那天」的收盤(range=5d 就是5天前)，
        # 拿它當昨收會把今日漲跌算成整段區間的漲跌(TSLL 曾被灌成 +27%，實際只有 +2.4%)。
        prev = m.get("previousClose")
        if prev is None:
            try:
                closes = [c for c in d["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
                if len(closes) >= 2:
                    prev = closes[-2]   # 倒數第二根 = 昨收（最後一根是今天）
            except Exception:
                pass
        # 判斷「最後成交」是不是今天（用交易所自己的時區）→ 否則沒開盤，今日漲跌應為 0
        traded_today = False
        mt = m.get("regularMarketTime")
        gmt = m.get("gmtoffset") or 0
        if mt:
            try:
                market_date = datetime.datetime.utcfromtimestamp(mt + gmt).date()
                now_date = datetime.datetime.utcfromtimestamp(now + gmt).date()
                traded_today = (market_date == now_date)
            except Exception:
                pass
        out = {"price": price, "prev": prev, "currency": m.get("currency"),
               "traded_today": traded_today,
               "name": m.get("shortName") or m.get("longName") or ""}
    except Exception:
        pass
    if out.get("price") is not None:
        # 抓到 → 更新 TTL 快取 + 記錄「最後成功價」
        _quote_cache[sym] = (now, out)
        _last_good[sym] = (now, out)
    else:
        # 抓不到(Yahoo 限流/斷網) → 沿用最後成功價、標記 stale，【絕不把該檔當 0 拖垮淨資產】。
        # 不寫進 _quote_cache(下次會重試)，也不覆蓋 _last_good 的好資料。
        lg = _last_good.get(sym)
        if lg:
            out = dict(lg[1])
            out["stale"] = True
            out["stale_age_s"] = int(now - lg[0])
    return out


def _usd_twd():
    q = _quote("USDTWD=X")
    return q.get("price") or 31.5


def portfolio():
    """回投資組合即時市值/報酬（總覽換算成 TWD）。
    台股用證交所 MIS 即時報價（零延遲），美股用 Yahoo。"""
    holdings = load().get("holdings", [])
    results = {}
    threads = []

    # 台股：一次批次抓證交所即時報價
    tw_codes = [str(h.get("symbol", "")).upper().strip()
                for h in holdings if (h.get("market") or "TW").upper() == "TW"]
    twse = {}
    try:
        twse = _twse_quotes(tw_codes)
    except Exception:
        twse = {}

    def fetch(h):
        sym = str(h.get("symbol", "")).upper().strip()
        mkt = (h.get("market") or "TW").upper()
        if mkt == "TW" and sym in twse:
            results[id(h)] = twse[sym]           # 證交所即時
        else:
            results[id(h)] = _quote(_yahoo_symbol(h))   # 美股，或台股抓不到時退 Yahoo
    for h in holdings:
        # 台股已有證交所資料的就不開線程；其餘（美股/fallback）並發抓 Yahoo
        sym = str(h.get("symbol", "")).upper().strip()
        if (h.get("market") or "TW").upper() == "TW" and sym in twse:
            results[id(h)] = twse[sym]
            continue
        t = threading.Thread(target=fetch, args=(h,))
        t.start(); threads.append(t)
    for t in threads:
        t.join(timeout=12)
    rate = _usd_twd()
    items = []
    tv = tc = tt = 0.0   # total value / cost / today（皆 TWD）
    any_traded = False
    name_filled = False   # 有沒有自動補了股名（要存回）
    stale_syms = []       # 抓不到即時價、用了最後成功價的持股（要提醒使用者數字可能不是最新）
    for h in holdings:
        q = results.get(id(h), {})
        price, prev = q.get("price"), q.get("prev")
        if q.get("stale"):
            stale_syms.append(str(h.get("name") or h.get("symbol") or "").strip())
        traded = bool(q.get("traded_today"))
        if traded:
            any_traded = True
        # 自動補股名：新增持股沒填名稱時，用 Yahoo 抓到的名稱填上（並存回）
        nm = h.get("name", "")
        if not str(nm).strip() and q.get("name"):
            nm = q["name"]
            h["name"] = nm
            name_filled = True
        shares, cost = _num(h.get("shares")), _num(h.get("cost"))
        cur = q.get("currency") or ("USD" if (h.get("market") or "").upper() == "US" else "TWD")
        fx = rate if cur == "USD" else 1.0
        value = (price or 0) * shares
        costv = cost * shares
        ret = value - costv
        # 只有「今天真的有成交」才算今日漲跌；休市則為 0，避免把上個交易日的漲幅誤當今日
        today = ((price - prev) * shares) if (price and prev and traded) else 0
        todaypct = round((price - prev) / prev * 100, 2) if (price and prev and traded) else 0
        items.append({
            "id": h.get("id"), "symbol": h.get("symbol"), "market": h.get("market"),
            "name": nm, "shares": shares, "cost": cost,
            "price": price, "currency": cur, "traded_today": traded,
            "value": round(value), "ret": round(ret),
            "retpct": round(ret / costv * 100, 2) if costv else 0,
            "today": round(today), "todaypct": todaypct,
        })
        tv += value * fx
        tc += costv * fx
        tt += today * fx
    if name_filled:   # 把自動補上的股名存回 finance.json（只發生一次）
        try:
            d2 = load()
            d2["holdings"] = holdings
            save(d2)
        except Exception:
            pass
    # 分市場小計（台股 TW / 美股 US）：讓 AI 直接唸「台股總報酬率」「美股總報酬率」，
    # 不必自己篩市場、自己加總、自己算（那會漏檔、會列成明細、數字每次不一樣）。全部換算成台幣比較。
    _mk = {}
    for it in items:
        code = (it.get("market") or "TW").upper()
        fx2 = rate if (it.get("currency") == "USD") else 1.0
        g = _mk.setdefault(code, {"value": 0.0, "cost": 0.0, "today": 0.0, "n": 0})
        g["value"] += _num(it.get("value")) * fx2
        g["cost"] += _num(it.get("cost")) * _num(it.get("shares")) * fx2
        g["today"] += _num(it.get("today")) * fx2
        g["n"] += 1
    _LABEL = {"TW": "台股", "US": "美股"}
    by_market = {}
    for code, g in _mk.items():
        v, c, t = g["value"], g["cost"], g["today"]
        by_market[code] = {
            "label": _LABEL.get(code, code), "count": g["n"],
            "value": round(v), "ret": round(v - c),
            "retpct": round((v - c) / c * 100, 2) if c else 0,
            "today": round(t),
            "todaypct": round(t / (v - t) * 100, 2) if (v - t) else 0,
        }
    return {
        "items": items, "usd_twd": round(rate, 3),
        "total_value": round(tv), "total_cost": round(tc),
        "total_ret": round(tv - tc), "total_retpct": round((tv - tc) / tc * 100, 2) if tc else 0,
        "total_today": round(tt),
        "total_todaypct": round(tt / (tv - tt) * 100, 2) if (tv - tt) else 0,
        "by_market": by_market,      # ★分市場小計：台股/美股 各自的市值、總報酬率、今日漲跌
        "market_open": any_traded,   # 今天有沒有任何一檔成交（給前端顯示「休市」）
        "stale": [s for s in stale_syms if s],   # 抓不到即時價、用舊價的持股名（空=全部即時）
    }


# ---------- 現金流總覽 ----------
def _dnorm(s):
    """日期正規化成 YYYY-MM-DD（補前導零）。避免「2026-6-5」字串比較把不該算的開銷算進來。"""
    s = str(s or "").strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def overview(with_portfolio=True):
    """with_portfolio=False：只算「花費/預算」（收入-固定-存款-已花，全部本地），
    不抓即時股價（YFinance 慢又無關）。問「還能花多少」這類純花費問題就傳 False。"""
    d = load()
    income = sum(_num(x.get("amount")) for x in d["income"] if isinstance(x, dict))
    fixed = sum(_num(x.get("amount")) for x in d["fixed"] if isinstance(x, dict))
    cstart, cend, clabel = _cycle()       # 發薪週期（如 6/15–7/14）
    exps = load_expenses()
    _td = _today()
    # 本期生活開銷＝週期起算到現在的開銷（日期正規化後比較，避免格式不一致誤算）
    month_var = sum(_num(e.get("amount")) for e in exps
                    if isinstance(e, dict) and _dnorm(e.get("date")) >= cstart)
    today_var = sum(_num(e.get("amount")) for e in exps
                    if isinstance(e, dict) and _dnorm(e.get("date")) == _td)
    budget = d.get("budget", {})
    spend_limit = _num(budget.get("spend_limit"))
    save_goal = _num(budget.get("save_goal"))
    # 本期可花預算：
    #  - 使用者有手動設「本期能花」(spend_limit > 0) → 就以他設的為準（覆寫自動估算）
    #  - 沒設 → 自動算 = 收入 − 固定開銷 − 每月存款（存的錢在發薪時就先扣掉）
    auto_spendable = income - fixed - save_goal
    spendable = spend_limit if spend_limit > 0 else auto_spendable
    remaining = spendable - month_var          # 還能花
    # 距下次發薪還幾天 + 每天可花
    try:
        cend_d = datetime.datetime.strptime(cend, "%Y-%m-%d").date()
        days_left = max(1, (cend_d - datetime.datetime.now(TZ).date()).days + 1)
    except Exception:
        days_left = 1
    daily_allowance = int(remaining / days_left) if days_left else round(remaining)
    pf = portfolio() if with_portfolio else None   # 只有問投資/淨資產才抓即時股價（慢）
    ov = {
        "income": round(income), "fixed": round(fixed),
        "month_var": round(month_var), "today_var": round(today_var),
        "auto_saved": round(save_goal),        # 發薪時已自動存起（不算可花）
        "spendable": round(spendable),         # 本期可花（扣掉固定+存款後）
        "remaining": round(remaining),         # 還能花
        "days_left": days_left,                # 距下次發薪還幾天
        "daily_allowance": daily_allowance,    # 每天可花
        "spent": round(fixed + month_var),     # 本期總支出（固定+生活）
        "net": round(remaining),               # 相容：net 改為「還能花」
        "cycle_start": cstart, "cycle_end": cend, "cycle_label": clabel,
        "payday": int(d.get("payday", 15) or 15),
        "budget": {"save_goal": round(save_goal), "spend_limit": round(spend_limit)},
        "spend_used_pct": (round(month_var / spendable * 100) if spendable > 0
                           else (100 if month_var > 0 else 0)),  # 入不敷出時顯示已用滿，不掩蓋超支
        "portfolio": (pf or {}),
        "income_list": d["income"], "fixed_list": d["fixed"],
        "holdings": d["holdings"], "expenses": exps[-50:][::-1],
    }
    cash = _num(d.get("cash"))
    ov["cash"] = round(cash)
    # 淨資產 / FIRE 需要投資市值 → 只有抓了 portfolio 才算（純花費問題不需要、也別拖慢）
    if with_portfolio and pf:
        ov["net_worth"] = round(cash + pf["total_value"])   # 淨資產＝銀行現金 + 投資市值
        # 財務自由（FIRE）：年開銷 ×25（4% 法則）。
        # 年開銷優先用手動設的 fire_annual；沒設才自動估：固定×12 +「本期生活開銷以已過天數的日均年化」
        try:
            _elapsed = max(1, (datetime.datetime.now(TZ).date()
                               - datetime.datetime.strptime(cstart, "%Y-%m-%d").date()).days + 1)
        except Exception:
            _elapsed = 30
        _var_annual = month_var / _elapsed * 365
        annual_exp = _num(d.get("fire_annual")) or (fixed * 12 + _var_annual)
        fire_target = _num(d.get("fire_target")) or round(annual_exp * 25)
        ov["fire_target"] = round(fire_target)
        ov["fire_pct"] = round(ov["net_worth"] / fire_target * 100, 1) if fire_target > 0 else 0
        ov["annual_expense"] = round(annual_exp)
        _ic, _icm = invest_committed()
        ov["invest_committed"] = round(_ic)     # 累積已固定投入投資的金額
        ov["invest_months"] = _icm
        record_snapshot(ov)   # 記當天淨資產快照（趨勢用）
    return ov


# ---------- 淨資產歷史（趨勢曲線）----------
HIST = os.path.join(_DIR, "networth_history.json")


def load_history():
    try:
        with open(HIST, encoding="utf-8") as f:
            h = json.load(f)
        return h if isinstance(h, list) else []
    except Exception:
        return []


def record_monthly_invest():
    """每月把『存款目標金額』記成一筆固定投入投資（同月只記一次）。回傳累積總額。"""
    d = load()
    amount = _num(d.get("budget", {}).get("save_goal"))
    month = datetime.datetime.now(TZ).strftime("%Y-%m")
    log = d.get("invest_log", [])
    if amount > 0 and not any(x.get("month") == month for x in log):
        log.append({"month": month, "amount": amount})
        d["invest_log"] = log
        save(d)
    return sum(_num(x.get("amount")) for x in log)


def invest_committed():
    """回傳（累積投入金額, 月數）。"""
    log = load().get("invest_log", [])
    return sum(_num(x.get("amount")) for x in log), len(log)


_MILESTONE_STEP = 100000   # 每 10 萬一個里程碑


def _check_milestone(net_worth):
    """淨資產突破新的 10 萬整數關 → Telegram 恭喜（只慶祝一次、只往上）。
    背景執行緒發送，不拖慢呼叫端。"""
    try:
        d = load()
        last = _num(d.get("last_milestone"))
        floor = int(net_worth // _MILESTONE_STEP * _MILESTONE_STEP)
        if floor <= 0:
            return
        if last == 0:
            # 首次：設基準，不慶祝
            d["last_milestone"] = floor
            save(d)
            return
        if floor > last:
            d["last_milestone"] = floor
            save(d)

            def _push():
                try:
                    import json as _j
                    from modules.remote.telegram_handler import TelegramHandler
                    # _DIR 就是 .../Hermes_Brain/config，telegram.json 在這裡
                    cfg = _j.load(open(os.path.join(_DIR, "telegram.json")))
                    h = TelegramHandler()
                    msg = (f"🎉 *淨資產里程碑！*\n你的淨資產突破 *NT${floor:,}* 了！\n"
                           f"目前 NT${int(net_worth):,}，繼續朝財務自由前進 💪🔥")
                    for uid in cfg.get("allowed_user_ids", []):
                        try:
                            h.send_message(uid, msg, parse_mode="Markdown")
                        except Exception:
                            pass
                except Exception:
                    pass
            threading.Thread(target=_push, daemon=True).start()
    except Exception:
        pass


def networth_trend(current_nw=None):
    """從淨資產歷史算出【每日 / 每週 / 每月】變動。給語音「我資產這週/這個月變多少」用。
    current_nw 有給就用當下即時淨資產當最新點(比昨天存的快照更即時)。"""
    hist = sorted(load_history(), key=lambda r: r.get("date", ""))
    if not hist:
        return None
    latest = hist[-1]
    nw = current_nw if current_nw is not None else latest.get("net_worth", 0)
    ldate = latest.get("date")

    def _on_or_before(days_ago):
        try:
            target = (datetime.datetime.strptime(ldate, "%Y-%m-%d")
                      - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")
        except Exception:
            return hist[0]
        prev = [r for r in hist if r.get("date", "") <= target]
        return prev[-1] if prev else hist[0]

    def _delta(base):
        if not base:
            return None
        b = base.get("net_worth", 0)
        return {"from": b, "from_date": base.get("date"),
                "change": round(nw - b),
                "pct": round((nw - b) / b * 100, 2) if b else 0}

    return {
        "net_worth": round(nw), "date": ldate, "days_tracked": len(hist),
        "daily": _delta(hist[-2] if len(hist) >= 2 else None),
        "weekly": _delta(_on_or_before(7)),
        "monthly": _delta(_on_or_before(30)),
    }


def record_snapshot(ov):
    """每天記一筆淨資產快照（同一天覆寫成最新值），給 dashboard 畫成長曲線。"""
    try:
        pf = ov.get("portfolio", {}) or {}
        today = _today()
        snap = {"date": today,
                "net_worth": ov.get("net_worth", 0),
                "portfolio": pf.get("total_value", 0),
                "total_ret": pf.get("total_ret", 0),
                "net": ov.get("net", 0)}
        hist = [h for h in load_history() if h.get("date") != today]
        hist.append(snap)
        hist = hist[-400:]
        with _lock:
            os.makedirs(_DIR, exist_ok=True)
            tmp = HIST + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False)
            os.replace(tmp, HIST)
    except Exception:
        pass
    _check_milestone(_num(ov.get("net_worth")))
