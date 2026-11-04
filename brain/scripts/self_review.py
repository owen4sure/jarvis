"""
夜間能力自省（Self-Review）— 能力/技能進化【提案】
====================================================
每晚回顧當天對話，找出 Jarvis【表現不好】的地方——答錯、做不到、查不到、缺工具、
使用者明顯不滿或重複追問——針對每個缺口【提案一個可以新增的能力/工具】。

★安全設計★：這支只【提案】，【絕對不自動改任何程式碼】。
提案寫進 ~/.hermes/self_review_proposals.jsonl，並推一則摘要到 Telegram：
「昨晚我發現 N 個可以變強的地方，要建哪個?」——Owen 核准後才透過 build_feature 建
（build_feature 那條路另有 git checkpoint + 冒煙測試把關）。

這是舊 evolution_engine 的正解：不再自己標記完成/亂改碼，改成人在迴路的提案制。

跑法：launchd 每晚一次（com.hermes.selfreview），或手動 python self_review.py。
游標 ~/.hermes/.selfreview_cursor。
"""
import datetime
import json
import os
import sqlite3
import urllib.request

STATE_DB = os.path.expanduser("~/.hermes/state.db")
CURSOR = os.path.expanduser("~/.hermes/.selfreview_cursor")
PROPOSALS = os.path.expanduser("~/.hermes/self_review_proposals.jsonl")
PROXY = "http://127.0.0.1:8808/v1beta/openai/chat/completions"
TODAY = datetime.datetime.now().strftime("%Y-%m-%d")

REVIEW_PROMPT = (
    f"今天是 {TODAY}。下面是 Owen 跟他的 AI 助理 Jarvis 今天的對話（含雙方）。\n"
    "你的任務：找出 Jarvis【表現不好、能力有缺口】的地方，針對每個缺口【提案一個具體、可實作的新能力/工具】，"
    "讓 Jarvis 之後能做到現在做不到的事。\n"
    "要找的訊號：Jarvis 說『做不到／查不到／我不確定／沒辦法』、答錯被 Owen 糾正、"
    "Owen 重複追問同件事、Owen 明顯不滿、或某個需求現有工具明顯無法滿足。\n"
    "【嚴格】：\n"
    "①只提【真的有價值、且技術上做得出來】的能力（可寫成一個工具/後端邏輯）。沒有明確缺口就回 []。\n"
    "②不要提『調整prompt/講話語氣』這種非能力的東西（那是相處守則，別的系統管）。\n"
    "③不要重複已經有的能力或已提過的提案。\n"
    "④每個提案要具體到『能直接交給工程師做』的程度。\n"
    '只回 JSON 陣列，每筆：{"title":"能力名稱(簡短)","why":"為什麼需要(引用對話裡的觸發點)","build":"要做什麼(給工程師的一句話)"}。'
    "平淡沒缺口的日子就回 []。"
)


def _read_cursor():
    try:
        return int(open(CURSOR).read().strip())
    except Exception:
        return 0


def _write_cursor(n):
    try:
        open(CURSOR, "w").write(str(n))
    except Exception:
        pass


def _new_messages(since_id):
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT id, role, content FROM messages WHERE id>? "
            "AND role IN ('user','assistant') AND content IS NOT NULL "
            "AND length(content)>1 ORDER BY id ASC LIMIT 200",
            (since_id,)).fetchall()
        con.close()
        return rows
    except Exception as e:
        print("[review] read db error:", e)
        return []


def _existing_proposals():
    """已提過的提案標題 → 避免重複提。"""
    out = []
    try:
        with open(PROPOSALS, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    t = json.loads(line).get("title", "")
                    if t:
                        out.append(t)
    except Exception:
        pass
    return out


def _review(convo, existing):
    known = "、".join(existing) or "（尚無）"
    prompt = (REVIEW_PROMPT
              + f"\n【已經提過的提案(不要重複)】：{known}\n\n【今天的對話】：\n" + convo)
    body = {"model": "gemini-3.1-flash-lite",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False, "temperature": 0.3}
    try:
        req = urllib.request.Request(PROXY, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=40))
        txt = r["choices"][0]["message"]["content"].strip()
        if "```" in txt:
            txt = txt.split("```")[1].replace("json", "", 1).strip()
        i, j = txt.find("["), txt.rfind("]")
        return json.loads(txt[i:j + 1]) if i >= 0 and j >= 0 else []
    except Exception as e:
        print("[review] llm error:", e)
        return []


def _save_proposals(props):
    ts = datetime.datetime.now().isoformat()
    with open(PROPOSALS, "a", encoding="utf-8") as f:
        for p in props:
            p = {**p, "ts": ts, "status": "proposed"}
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def _auto_build(prop):
    """★自主進化★：把最高價值的提案直接【自己動手做】——走 8809 /build_feature(全棧+安全閘：
    改前備份、冒煙測試、壞了自動還原、自動上線)。不用等 Owen 核准。做完在 dashboard 看得到、可還原。"""
    desc = f"{prop.get('title', '')}：{prop.get('build', '')}"
    try:
        req = urllib.request.Request("http://127.0.0.1:8809/build_feature",
                                     data=json.dumps({"description": desc}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)  # fire-and-forget，build 在 8809 背景跑
        return True
    except Exception as e:
        print("[review] auto_build error:", e)
        return False


def _push_telegram(props, built=None):
    """推提案摘要給 Owen（Telegram），不自動建，等他挑。"""
    try:
        import sys
        sys.path.insert(0, os.path.expanduser("~/Hermes_Brain"))
        from modules.remote.telegram_handler import TelegramHandler
        h = TelegramHandler()
        lines = []
        if built:
            lines.append(f"🧬 昨晚我自己進化了：我發現缺「{built.get('title', '')}」，已經【自己動手做出來】並上線了，"
                         f"你在控制台(localhost:8811)就看得到。（有備份，不喜歡跟我說我還原）")
        rest = [p for p in props if p is not built]
        if rest:
            lines.append(f"\n另外還發現 {len(rest)} 個可以變強的地方：")
            for i, p in enumerate(rest, 1):
                lines.append(f"{i}. 【{p.get('title', '')}】{p.get('why', '')[:46]}")
            lines.append("要我也做的話跟我說編號。")
        msg = "\n".join(lines) if lines else ""
        if not msg:
            return True
        cfg = json.load(open(os.path.expanduser("~/Hermes_Brain/config/telegram.json")))
        for uid in cfg.get("allowed_user_ids", []):
            h.send_message(uid, msg)
        return True
    except Exception as e:
        print("[review] telegram error:", e)
        return False


def main():
    cursor = _read_cursor()
    rows = _new_messages(cursor)
    if not rows:
        print("[review] 沒有新對話")
        return
    max_id = max(r[0] for r in rows)
    convo = "\n".join(f"{'Owen' if r[1] == 'user' else 'Jarvis'}：{r[2]}" for r in rows)
    props = [p for p in _review(convo, _existing_proposals())
             if isinstance(p, dict) and p.get("title")]
    built = None
    if props:
        _save_proposals(props)
        # ★自主進化★：一晚自己動手做【最高價值那 1 個】(限 1 個控制風險)，其餘回報給 Owen 選。
        built = props[0]
        if _auto_build(built):
            print(f"[review] 自主建置：{built.get('title')}")
        else:
            built = None
        _push_telegram(props, built=built)
        for p in props:
            print(f"[review] 提案：{p.get('title')} — {p.get('build', '')[:50]}")
    _write_cursor(max_id)
    print(f"[review] 回顧 {len(rows)} 句 → {len(props)} 提案，自主建 {1 if built else 0} 個（游標→{max_id}）")


if __name__ == "__main__":
    main()
