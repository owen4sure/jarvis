"""台灣國定假日/連假查詢(用政府辦公日曆官方資料,不靠瞎猜)。

資料來源:TaiwanCalendar(政府行政機關辦公日曆表的 JSON 版),每年一檔。
首次查某年 → 抓下來快取在 config/tw_calendar_YYYY.json,之後讀本地。
"""
import json
import os
import re
import urllib.request
from datetime import datetime, date, timedelta
import zoneinfo

TZ = zoneinfo.ZoneInfo("Asia/Taipei")
CACHE_DIR = "/Users/USERNAME/Hermes_Brain/config"
SRC = "https://raw.githubusercontent.com/ruyut/TaiwanCalendar/master/data/{year}.json"


def _load_year(year):
    """回 {YYYYMMDD: {isHoliday, description, week}}。本地沒有就抓。"""
    path = os.path.join(CACHE_DIR, f"tw_calendar_{year}.json")
    data = None
    if os.path.exists(path):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            data = None
    if data is None:
        try:
            with urllib.request.urlopen(SRC.format(year=year), timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            return {}
    return {row["date"]: row for row in data}


def _this_year():
    return datetime.now(TZ).year


def _parse_dates(text):
    """把使用者講的日期/範圍轉成 [date,...]。支援:
    2026-10-23 / 10/23 / 10/23-26 / 10/23~10/26 / 10月23日 等。年份沒給就用今年(過了就明年)。"""
    s = str(text).strip()
    today = datetime.now(TZ).date()
    yr = today.year

    # 先抓所有 (月,日) 配對
    pairs = []
    # YYYY-MM-DD
    for m in re.finditer(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s):
        pairs.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if not pairs:
        # M/D 或 M月D日，含範圍 M/D-D / M/D-M/D
        # 先找出所有 月/日
        md = re.findall(r"(\d{1,2})\s*[/月]\s*(\d{1,2})", s)
        nums_after = re.findall(r"[-~到至]\s*(\d{1,2})(?![/月\d])", s)  # 10/23-26 的 26
        for (mo, da) in md:
            pairs.append((None, int(mo), int(da)))
        # 範圍尾巴只有「日」(例如 10/23-26 的 26)
        if md and nums_after:
            mo = int(md[0][0])
            for d2 in nums_after:
                pairs.append((None, mo, int(d2)))
    if not pairs:
        return []

    def _mk(y, mo, da):
        y = y or yr
        try:
            d = date(y, mo, da)
        except ValueError:
            return None
        # 沒給年份且日期已過 → 視為明年
        if y == yr and d < today - timedelta(days=1):
            try:
                d = date(yr + 1, mo, da)
            except ValueError:
                return None
        return d

    ds = [d for d in (_mk(*p) for p in pairs) if d]
    if not ds:
        return []
    ds = sorted(set(ds))
    # 若是「起-迄」範圍(兩個日期),補滿中間每一天
    if len(ds) >= 2:
        full = []
        cur = ds[0]
        while cur <= ds[-1]:
            full.append(cur)
            cur += timedelta(days=1)
        return full
    return ds


def _month_holidays(month):
    """整月查詢：列那個月的連假(連續2天以上、含國定假日)。給「10月有連假嗎」用。"""
    today = date.today()
    year = today.year if month >= today.month else today.year + 1
    caldata = _load_year(year)
    if not caldata:
        return f"我查不到 {year} 年的官方行事曆(可能還沒公布)，{month}月的連假還不確定。"
    days_off = []
    for day in range(1, 32):
        try:
            d = date(year, month, day)
        except ValueError:
            break
        row = caldata.get(d.strftime("%Y%m%d"))
        is_h = row.get("isHoliday", False) if row else (d.weekday() >= 5)
        desc = (row.get("description", "") if row else "") or ("週末" if d.weekday() >= 5 else "")
        days_off.append((d, is_h, desc))
    runs, cur = [], []
    for d, is_h, desc in days_off:
        if is_h:
            cur.append((d, desc))
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
    if len(cur) >= 2:
        runs.append(cur)
    named = [r for r in runs if any(x[1] and x[1] != "週末" for x in r)]
    if not named:
        return f"{year}年{month}月沒有連假喔，只有一般的週末。"
    out = []
    for r in named:
        nm = next((x[1] for x in r if x[1] and x[1] != "週末"), "連假")
        out.append(f"{r[0][0].month}/{r[0][0].day}～{r[-1][0].day} 放 {len(r)} 天（{nm}）")
    return f"{year}年{month}月有連假：" + "、".join(out) + "。"


def check(text):
    """查某天/某範圍的連假狀況,回一段人話。給 Jarvis 直接用。"""
    days = _parse_dates(text)
    if not days:
        # 整月查詢:「10月有連假嗎」→ 列那個月的連假
        m = re.search(r"(\d{1,2})\s*月(?!\s*\d)", text or "")
        if m and 1 <= int(m.group(1)) <= 12:
            return _month_holidays(int(m.group(1)))
        return "我看不懂你說的日期，可以說「10/23」或「10月23到26號」這樣嗎？"

    cal = {}
    have_year = {}
    for y in {d.year for d in days}:
        ylist = _load_year(y)
        cal.update(ylist)
        have_year[y] = bool(ylist)

    # 沒有官方行事曆的年份(例如還沒公布的未來年) → 老實說查不到，別用「看星期幾」瞎猜
    # (否則 1/1 開國紀念日這種平日國定假日會被誤判成上班)。
    missing_years = sorted(y for y, ok in have_year.items() if not ok)
    if missing_years and not any(have_year.values()):
        ys = "、".join(str(y) for y in missing_years)
        return f"我查不到 {ys} 年的台灣官方行事曆(可能還沒公布)，沒辦法確定那時的連假，你再跟我確認一下。"

    wk = "一二三四五六日"
    parts = []
    holidays = []
    for d in days:
        key = d.strftime("%Y%m%d")
        row = cal.get(key)
        if row is None:
            # 沒資料(可能該年行事曆還沒出) → 用週末粗判
            is_h = d.weekday() >= 5
            desc = "週末" if is_h else ""
        else:
            is_h = row.get("isHoliday", False)
            desc = row.get("description", "") or ("週末" if d.weekday() >= 5 else "")
        tag = f"{d.month}/{d.day}(週{wk[d.weekday()]})"
        if is_h:
            holidays.append(d)
            parts.append(f"{tag} 放假{('・' + desc) if desc and desc != '週末' else ''}")
        else:
            parts.append(f"{tag} 上班")

    # 偵測連假(連續 2 天以上放假)
    streak = []
    best = []
    prev = None
    for d in holidays:
        if prev and (d - prev).days == 1:
            streak.append(d)
        else:
            streak = [d]
        if len(streak) > len(best):
            best = streak[:]
        prev = d

    head = "；".join(parts)
    if len(best) >= 2:
        h0, h1 = best[0], best[-1]
        names = [cal.get(d.strftime("%Y%m%d"), {}).get("description", "") for d in best]
        names = [n for n in names if n and n != "週末"]
        why = ("（" + "、".join(names) + "）") if names else ""
        return (f"你問的這幾天：{head}。"
                f"\n👉 有連假！{h0.month}/{h0.day}～{h1.month}/{h1.day} 連放 {len(best)} 天{why}。")
    elif holidays:
        return f"你問的這幾天：{head}。只有單天放假、沒有連在一起的連假。"
    return f"你問的這幾天：{head}。都是上班日，沒有連假。"


if __name__ == "__main__":
    import sys
    print(check(sys.argv[1] if len(sys.argv) > 1 else "10/23-26"))
