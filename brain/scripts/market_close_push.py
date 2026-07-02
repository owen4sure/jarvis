#!/usr/bin/env python3
"""收盤推播：台股/美股收盤後推一則 Telegram 報投資現況。
用法： python market_close_push.py tw   # 台股收盤
       python market_close_push.py us   # 美股收盤
只在「該市場今天真的有開盤成交」時才推（週末/假日自動不推）。
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.finance import wealth  # noqa: E402

MARKET = (sys.argv[1] if len(sys.argv) > 1 else "tw").upper()
TITLE = "台股" if MARKET == "TW" else "美股"


def _fmt(n):
    return f"{round(n):,}"


def _arrow(n):
    return "🔺" if n > 0 else ("🔻" if n < 0 else "▪️")


def build_message():
    pf = wealth.portfolio()
    items = [it for it in pf.get("items", []) if (it.get("market") or "").upper() == MARKET]
    if not items:
        return None  # 沒有這個市場的持股
    # 該市場今天有沒有成交（任一檔 traded_today）
    traded = any(it.get("traded_today") for it in items)
    if not traded:
        return None  # 今天沒開盤，不推

    rate = pf.get("usd_twd", 31.5)
    fx = rate if MARKET == "US" else 1.0
    cur = "美元" if MARKET == "US" else "元"
    mkt_value = sum((it["price"] or 0) * it["shares"] for it in items)
    mkt_cost = sum(it["cost"] * it["shares"] for it in items)
    mkt_today = sum(it["today"] for it in items)
    mkt_ret = mkt_value - mkt_cost
    retpct = (mkt_ret / mkt_cost * 100) if mkt_cost else 0
    todaypct = (mkt_today / (mkt_value - mkt_today) * 100) if (mkt_value - mkt_today) else 0

    from datetime import datetime
    import zoneinfo
    now = datetime.now(zoneinfo.ZoneInfo("Asia/Taipei"))

    lines = [f"📊 *{TITLE}收盤快報*  {now.strftime('%m/%d %H:%M')}", ""]
    # 今日損益（重點）
    lines.append(f"{_arrow(mkt_today)} 今日損益：*{'+' if mkt_today>=0 else ''}{_fmt(mkt_today)} {cur}*（{todaypct:+.2f}%）")
    lines.append(f"💼 {TITLE}市值：{_fmt(mkt_value)} {cur}")
    lines.append(f"📈 {TITLE}總報酬：{'+' if mkt_ret>=0 else ''}{_fmt(mkt_ret)} {cur}（{retpct:+.2f}%）")
    lines.append("")
    lines.append("*各檔今日：*")
    for it in sorted(items, key=lambda x: x.get("todaypct", 0), reverse=True):
        nm = it.get("name") or it.get("symbol")
        flame = " 🔥" if abs(it.get("todaypct", 0)) >= 3 else ""  # 大動作標火
        lines.append(f"{_arrow(it['today'])} {nm}（{it['symbol']}）{it['todaypct']:+.2f}%　{'+' if it['today']>=0 else ''}{_fmt(it['today'])}{cur}{flame}")

    # 今日最強/最弱亮點
    if len(items) > 1:
        best = max(items, key=lambda x: x.get("todaypct", 0))
        worst = min(items, key=lambda x: x.get("todaypct", 0))
        lines.append("")
        lines.append(f"🏆 今日最強：{best.get('name') or best['symbol']} {best['todaypct']:+.2f}%")
        if worst is not best:
            lines.append(f"🥶 今日最弱：{worst.get('name') or worst['symbol']} {worst['todaypct']:+.2f}%")

    # 全組合淨資產（兩市場合計，台幣）
    lines.append("")
    lines.append(f"🏦 投資組合總市值：NT${_fmt(pf['total_value'])}（總報酬 {pf['total_retpct']:+.2f}%）")
    if MARKET == "US":
        lines.append(f"（匯率 USDTWD {rate}）")
    return "\n".join(lines)


def main():
    # 每個交易日收盤都記一筆淨資產快照（確保趨勢圖每天有點，不只開 dashboard 時）
    try:
        wealth.overview()
    except Exception:
        pass
    try:
        msg = build_message()
    except Exception as e:
        print(f"[{MARKET}] 產生訊息失敗（可能抓報價逾時）：{e}")
        return
    if not msg:
        print(f"[{MARKET}] 今天沒開盤或無持股，不推播")
        return
    try:
        from modules.remote.telegram_handler import TelegramHandler
        cfg = json.load(open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "telegram.json")))
        handler = TelegramHandler()
        sent = 0
        for uid in cfg.get("allowed_user_ids", []):
            try:
                ok = handler.send_message(uid, msg, parse_mode="Markdown")
                if not ok:  # Markdown 解析失敗 → 退回純文字（去掉星號）重送，確保一定收得到
                    ok = handler.send_message(uid, msg.replace("*", ""))
                if ok:
                    sent += 1
            except Exception as e:
                print(f"⚠️ 推給 {uid} 失敗：{e}")
        print(f"[{MARKET}] 已推播給 {sent} 人")
    except Exception as e:
        print(f"❌ 推播失敗：{e}")


if __name__ == "__main__":
    main()
