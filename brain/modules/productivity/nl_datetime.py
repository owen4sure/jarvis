"""中文自然語言時間解析。把「這禮拜五早上11:00」「明天下午3點半」「30分鐘後」
等口語，解析成確切的 datetime + 剩下的訊息。解析不出來回 (None, 原文)，
讓呼叫端優雅處理，絕不拋例外給使用者。
"""
import re
import datetime
import zoneinfo

TZ = zoneinfo.ZoneInfo("Asia/Taipei")
_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_CN_NUM = {"零": 0, "一": 1, "兩": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _cn2int(s):
    """簡單中文數字 → int（支援 一~十二、十、十一…）。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    if s.startswith("十") and len(s) == 2:
        return 10 + _CN_NUM.get(s[1], 0)
    if s.endswith("十") and len(s) == 2:
        return _CN_NUM.get(s[0], 0) * 10
    if "十" in s and len(s) == 3:  # 二十三
        a, b = s.split("十")
        return _CN_NUM.get(a, 1) * 10 + _CN_NUM.get(b, 0)
    return None


def _clean(msg):
    msg = re.sub(r"^[\s,，、。:：的要在於]+", "", msg)
    msg = re.sub(r"[\s,，、]+$", "", msg)
    # 去掉殘留的「要」「想」「幫我」「提醒我」等
    msg = re.sub(r"^(要|想|請|幫我|提醒我|記得|叫我|跟我說)+", "", msg).strip()
    return msg.strip()


def parse_when(text, now=None):
    """回 (datetime 或 None, 剩餘訊息)。"""
    now = now or datetime.datetime.now(TZ)
    t = str(text or "").strip()
    if not t:
        return None, ""
    msg = t
    base = now
    date_set = False
    hour = None
    minute = 0

    # 0a) 半小時後 / 半個鐘頭後 → 30 分鐘
    mhalf = re.search(r"半\s*(個)?\s*(小時|鐘頭|鐘)\s*[後后]", t)
    if mhalf:
        fire = (now + datetime.timedelta(minutes=30)).replace(second=0, microsecond=0)
        return fire, _clean(t.replace(mhalf.group(0), ""))

    # 0) N 分鐘/小時後（相對）
    mrel = re.search(r"(\d+)\s*(個)?\s*(分鐘|分|小時|鐘頭|hr|hour|min)\s*[後后]", t, re.I)
    if mrel:
        n = int(mrel.group(1))
        unit = mrel.group(3)
        delta = datetime.timedelta(minutes=n) if ("分" in unit or "min" in unit.lower()) \
            else datetime.timedelta(hours=n)
        fire = (now + delta).replace(second=0, microsecond=0)
        return fire, _clean(t.replace(mrel.group(0), ""))

    # 1) 相對日
    for k, v in (("大後天", 3), ("大后天", 3), ("後天", 2), ("后天", 2),
                 ("明天", 1), ("明日", 1), ("今天", 0), ("今日", 0), ("今晚", 0)):
        if k in t:
            base = now + datetime.timedelta(days=v)
            date_set = True
            msg = msg.replace(k, "")
            break

    # 2) 禮拜X / 星期X / 週X（這/本/下/下下）
    m = re.search(r"(這|本|下下|下個|下)?\s*(?:禮拜|星期|週|周)\s*([一二三四五六日天])", t)
    if m and not date_set:
        which = m.group(1) or ""
        wd = _WD[m.group(2)]
        days = (wd - now.weekday()) % 7
        if "下下" in which:
            days += 14 if days else 14
        elif which in ("下", "下個"):
            days = days + 7 if days == 0 else days + 7
        base = now + datetime.timedelta(days=days)
        date_set = True
        msg = msg.replace(m.group(0), "")

    # 2b) 下個月 X 號 / 這個月 X 號
    mnm = re.search(r"(這|本|下)\s*個?\s*月\s*(\d{1,2})\s*[號号日]", t)
    if mnm and not date_set:
        dd = int(mnm.group(2))
        mo = now.month + (1 if mnm.group(1) == "下" else 0)
        yr = now.year + (1 if mo > 12 else 0)
        mo = mo - 12 if mo > 12 else mo
        try:
            base = now.replace(year=yr, month=mo, day=dd,
                               hour=0, minute=0, second=0, microsecond=0)
            date_set = True
            msg = msg.replace(mnm.group(0), "")
        except ValueError:
            pass

    # 3) X月X日 / X/X
    m2 = re.search(r"(\d{1,2})\s*[月/]\s*(\d{1,2})\s*[日號]?", t)
    if m2:
        mo, dd = int(m2.group(1)), int(m2.group(2))
        try:
            cand = now.replace(month=mo, day=dd, hour=0, minute=0, second=0, microsecond=0)
            if cand.date() < now.date():
                cand = cand.replace(year=now.year + 1)
            base = cand
            date_set = True
            msg = msg.replace(m2.group(0), "")
        except ValueError:
            pass

    # 4) 時段詞
    period = None
    for p in ("凌晨", "清晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "半夜", "晚"):
        if p in t:
            period = p
            msg = msg.replace(p, "")
            break

    # 5) 時間 HH:MM
    m3 = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", t)
    if m3:
        hour = int(m3.group(1))
        minute = int(m3.group(2))
        msg = msg.replace(m3.group(0), "")
    else:
        # N點(半/N分) — 支援中文數字
        m4 = re.search(r"([0-9]{1,2}|[一二兩三四五六七八九十]{1,3})\s*點\s*(半|[0-9]{1,2}|[一二三四五六七八九十]{1,3})?\s*分?", t)
        if m4:
            hh = _cn2int(m4.group(1))
            if hh is not None:
                hour = hh
                g2 = m4.group(2)
                if g2 == "半":
                    minute = 30
                elif g2:
                    minute = _cn2int(g2) or 0
                msg = msg.replace(m4.group(0), "")

    # 6) 時段調整
    if hour is not None and period:
        if period in ("下午", "傍晚", "晚上", "晚") and hour < 12:
            hour += 12
        elif period == "中午":
            hour = 12 if hour in (0, 12) else hour
        elif period in ("半夜", "凌晨") and hour == 12:
            hour = 0
    elif hour is not None and period is None and "晚" in t and hour < 12:
        hour += 12

    if hour is None:
        if period:   # 只說時段沒說幾點 → 用該時段的合理預設
            hour = {"凌晨": 1, "清晨": 6, "早上": 9, "上午": 9, "中午": 12,
                    "下午": 14, "傍晚": 18, "晚上": 20, "晚": 20, "半夜": 0}.get(period, 9)
            minute = 0
        elif date_set:
            hour, minute = 9, 0   # 有日期沒時間 → 預設早上9點
        else:
            return None, t        # 完全沒時間資訊 → 無法定時

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, t

    fire = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if not date_set and fire <= now:
        fire += datetime.timedelta(days=1)   # 今天這時間已過 → 排明天
    return fire, _clean(msg)


def describe(dt, now=None):
    """把 datetime 講成口語：明天下午3點 / 禮拜五早上11點。"""
    now = now or datetime.datetime.now(TZ)
    wd = ["一", "二", "三", "四", "五", "六", "日"][dt.weekday()]
    days = (dt.date() - now.date()).days
    if days == 0:
        day = "今天"
    elif days == 1:
        day = "明天"
    elif days == 2:
        day = "後天"
    elif 0 < days < 7:
        day = "禮拜" + wd
    else:
        day = "%d月%d日" % (dt.month, dt.day)
    h = dt.hour
    period = "凌晨" if h < 6 else "早上" if h < 12 else "中午" if h == 12 else "下午" if h < 18 else "晚上"
    h12 = h if h <= 12 else h - 12
    mm = "" if dt.minute == 0 else "%02d分" % dt.minute
    return "%s%s%d點%s" % (day, period, h12, mm)
