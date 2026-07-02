"""記帳分析核心：智慧歸類、月分析、消費趨勢、洞察、常用項目。
讀 config/expenses.json（生活開銷）+ config/finance.json（分類預算）。
"""
import json
import os
import datetime
import zoneinfo
from collections import defaultdict

_DIR = "/Users/chenyouwei/Hermes_Brain/config"
EXP = os.path.join(_DIR, "expenses.json")
FIN = os.path.join(_DIR, "finance.json")
TZ = zoneinfo.ZoneInfo("Asia/Taipei")

# 標準分類（固定順序，給前端配色）
STD_CATEGORIES = ["餐飲", "交通", "購物", "娛樂", "居家", "醫療", "學習", "訂閱", "人情", "其他"]

# 關鍵字 → 標準分類（智慧歸類用）
CATEGORY_KEYWORDS = {
    "餐飲": ["餐", "吃", "飯", "午餐", "晚餐", "早餐", "早午餐", "咖啡", "飲", "茶", "食",
             "便當", "小吃", "宵夜", "聚餐", "外食", "麥當勞", "星巴克", "手搖", "零食",
             "水果", "麵", "火鍋", "燒烤", "拉麵", "壽司", "披薩", "炸", "鍋", "甜點", "蛋糕"],
    "交通": ["車", "捷運", "公車", "計程車", "uber", "油", "加油", "停車", "高鐵", "火車",
             "機票", "交通", "ubike", "客運", "悠遊卡", "過路費", "etag"],
    "購物": ["買", "購", "衣服", "鞋", "包包", "網購", "蝦皮", "momo", "家樂福", "全聯",
             "超市", "日用", "電器", "3c", "家電", "化妝", "保養", "美妝", "生活用品"],
    "娛樂": ["電影", "遊戲", "ktv", "唱歌", "旅遊", "玩", "酒", "展", "演唱會", "娛樂",
             "門票", "出去玩", "夜衝", "桌遊", "劇"],
    "居家": ["房租", "水電", "瓦斯", "電費", "水費", "管理費", "家具", "居家", "房貸", "裝潢"],
    "醫療": ["醫", "藥", "看病", "健保", "掛號", "牙", "診所", "醫院", "保健", "看醫生", "復健"],
    "學習": ["書", "課", "學費", "補習", "教材", "學習", "線上課", "證照", "報名費"],
    "訂閱": ["訂閱", "netflix", "spotify", "youtube", "會員", "月費", "icloud", "chatgpt",
             "disney", "訂閱制", "premium"],
    "人情": ["禮", "紅包", "送", "請客", "捐", "人情", "婚禮", "包紅包", "禮物", "份子"],
    "網路": ["網路", "電話費", "手機費", "通訊"],   # 歸到居家
}
# 網路 併入 居家
_MERGE = {"網路": "居家"}


def _load_expenses():
    try:
        d = json.load(open(EXP, encoding="utf-8"))
        return d.get("expenses", []) if isinstance(d, dict) else (d or [])
    except Exception:
        return []


def _load_fin():
    try:
        return json.load(open(FIN, encoding="utf-8"))
    except Exception:
        return {}


def categorize(category="", note=""):
    """把自由文字（類別＋備註）歸到標準分類。"""
    t = (str(category or "") + " " + str(note or "")).lower()
    raw = str(category or "")
    for cat in STD_CATEGORIES:        # 已經是標準分類就直接用
        if cat in raw:
            return cat
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in t:
                return _MERGE.get(cat, cat)
    return "其他"


def _ym(d=None):
    return (d or datetime.datetime.now(TZ)).strftime("%Y-%m")


def _today():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def _num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


# ---------- 月分析 ----------
def monthly_totals(months=6):
    """近 N 個月每月總花費 [{month, total}]（舊→新）。"""
    exps = _load_expenses()
    now = datetime.datetime.now(TZ)
    out = []
    for i in range(months - 1, -1, -1):
        m = (now.replace(day=1) - datetime.timedelta(days=1) * 0)
        # 算 i 個月前的 YYYY-MM
        y, mo = now.year, now.month - i
        while mo <= 0:
            mo += 12
            y -= 1
        ym = "%04d-%02d" % (y, mo)
        total = sum(_num(e.get("amount")) for e in exps if str(e.get("date", "")).startswith(ym))
        out.append({"month": ym, "total": round(total)})
    return out


def _cycle_start():
    """目前發薪週期起始日（YYYY-MM-DD）。失敗回本月1號。"""
    try:
        from modules.finance import wealth
        return wealth._cycle()[0]
    except Exception:
        return _ym() + "-01"


def category_breakdown(since=None):
    """本期（發薪週期起算）的分類花費 {標準分類: 金額}。"""
    since = since or _cycle_start()
    exps = _load_expenses()
    by = defaultdict(float)
    for e in exps:
        if str(e.get("date", "")) >= since:
            by[categorize(e.get("category"), e.get("note"))] += _num(e.get("amount"))
    return {k: round(v) for k, v in sorted(by.items(), key=lambda x: -x[1])}


def daily_series(since=None):
    """本期（發薪週期起算）每日花費 [{date, amount}]。"""
    since = since or _cycle_start()
    exps = _load_expenses()
    by = defaultdict(float)
    for e in exps:
        d = str(e.get("date", ""))
        if d >= since:
            by[d] += _num(e.get("amount"))
    return [{"date": k, "amount": round(v)} for k, v in sorted(by.items())]


def frequent_items(limit=6):
    """常用記帳項目（依 類別+金額 出現次數）。給快速記帳。"""
    exps = _load_expenses()
    cnt = defaultdict(int)
    last = {}
    for e in exps:
        key = (str(e.get("category", "")).strip(), round(_num(e.get("amount"))))
        if key[0] and key[1] > 0:
            cnt[key] += 1
            last[key] = e.get("note", "")
    items = sorted(cnt.items(), key=lambda x: -x[1])
    return [{"category": k[0], "amount": k[1], "count": c, "note": last.get(k, "")}
            for k, c in items[:limit] if c >= 1]


# ---------- 分類預算 ----------
def get_category_budgets():
    return _load_fin().get("category_budgets", {})


def set_category_budget(category, amount):
    d = _load_fin()
    cb = dict(d.get("category_budgets", {}))
    if _num(amount) > 0:
        cb[category] = round(_num(amount))
    else:
        cb.pop(category, None)
    d["category_budgets"] = cb
    tmp = FIN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FIN)
    return cb


def category_status(ym=None):
    """各分類本月花費 vs 預算 [{category, spent, budget, pct}]。"""
    bd = category_breakdown(ym)
    budgets = get_category_budgets()
    cats = set(bd) | set(budgets)
    out = []
    for c in cats:
        spent = bd.get(c, 0)
        budget = round(_num(budgets.get(c)))
        out.append({"category": c, "spent": round(spent), "budget": budget,
                    "pct": round(spent / budget * 100) if budget else 0})
    return sorted(out, key=lambda x: -x["spent"])


# ---------- 洞察 ----------
def insights():
    """產生本期消費洞察（口語短句清單）。本期＝發薪週期。"""
    out = []
    bd = category_breakdown()
    period_total = round(sum(bd.values()))
    if period_total:
        out.append("本期生活花費 %s 元" % period_total)
        top = list(bd.items())[0]
        out.append("最大宗是「%s」，花了 %s 元" % (top[0], top[1]))
    # 與上個日曆月比較（趨勢參考）
    mt = monthly_totals(2)
    this_m = mt[-1]["total"] if mt else 0
    last_m = mt[-2]["total"] if len(mt) >= 2 else 0
    if this_m and last_m:
        diff = this_m - last_m
        pct = round(diff / last_m * 100) if last_m else 0
        if diff > 0:
            out.append("本月（日曆）比上月多花 %s 元（+%s%%）" % (diff, pct))
        elif diff < 0:
            out.append("本月（日曆）比上月省了 %s 元（%s%%）👍" % (-diff, pct))
    # 超出分類預算
    for cs in category_status():
        if cs["budget"] and cs["spent"] > cs["budget"]:
            out.append("⚠️「%s」已花 %s，超出預算 %s" % (cs["category"], cs["spent"], cs["budget"]))
        elif cs["budget"] and cs["pct"] >= 80:
            out.append("「%s」已用掉預算 %s%%，要注意" % (cs["category"], cs["pct"]))
    return out


def analysis():
    """給 dashboard 的完整分析資料。本期數字＝發薪週期，趨勢柱＝近6日曆月。"""
    mt = monthly_totals(6)
    bd = category_breakdown()
    period_total = round(sum(bd.values()))   # 本期（發薪週期）生活花費
    last_m = mt[-2]["total"] if len(mt) >= 2 else 0
    cal_this = mt[-1]["total"] if mt else 0
    clabel = ""
    try:
        from modules.finance import wealth
        clabel = wealth._cycle()[2]
    except Exception:
        pass
    return {
        "month": _ym(),
        "cycle_label": clabel,
        "this_month": period_total,          # 本期花費（發薪週期）
        "last_month": last_m,
        "mom_diff": cal_this - last_m,
        "mom_pct": round((cal_this - last_m) / last_m * 100) if last_m else 0,
        "monthly": mt,
        "by_category": bd,
        "daily": daily_series(),
        "category_status": category_status(),
        "frequent": frequent_items(),
        "insights": insights(),
        "categories": STD_CATEGORIES,
    }
