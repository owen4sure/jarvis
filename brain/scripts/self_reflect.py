"""
夜間自我反省（Self-Reflect）— 人格/相處進化
=============================================
不靠大腦對話中自己記得呼叫 soul_evolve（時靈時不靈），改成【確定性的夜間排程】：
每晚掃當天的完整對話（含 Owen 跟 Jarvis 雙方），用 cheap LLM 萃取「怎麼跟 Owen 相處更好」
的【持久洞察】——他的溝通偏好、雷點、在意的事、被什麼惹毛/取悅——寫進 SOUL.md 的成長區
（8809 /soul/evolve，內含去重）。

跟 memory_scribe 分工：
  · memory_scribe 抽【事實】(Owen 是誰、喜好、行程) → /remember
  · self_reflect  抽【相處之道】(該怎麼對待他、該避免什麼)     → /soul/evolve

低風險：只【append】到 SOUL.md 的「我學到的（自我成長）」區段，不碰程式碼、不刪任何東西。
跑法：launchd 每晚一次（com.hermes.selfreflect），或手動 python self_reflect.py。
游標 ~/.hermes/.selfreflect_cursor 記到哪一筆 message id，只看新對話。
"""
import datetime
import json
import os
import sqlite3
import urllib.request

STATE_DB = os.path.expanduser("~/.hermes/state.db")
CURSOR = os.path.expanduser("~/.hermes/.selfreflect_cursor")
PROXY = "http://127.0.0.1:8808/v1beta/openai/chat/completions"
SOUL_EVOLVE = "http://127.0.0.1:8809/soul/evolve"
SOUL_PATH = os.path.expanduser("~/.hermes/SOUL.md")
TODAY = datetime.datetime.now().strftime("%Y-%m-%d")

REFLECT_PROMPT = (
    f"今天是 {TODAY}。下面是使用者 Owen 跟他的 AI 夥伴 Jarvis 今天的對話（含雙方）。\n"
    "你的任務：萃取【該怎麼跟 Owen 相處會更好】的持久相處守則——他的溝通偏好、雷點、"
    "被什麼惹毛或取悅、期待 Jarvis 用什麼態度/方式回應。\n"
    "【格式·嚴格】每筆是一句【短、通用】的相處守則，用第二人稱對 Jarvis 講，【最多 30 個字】。\n"
    "  好例子：「回答直接給結論，Owen 討厭囉嗦鋪陳」「講錢的數字絕不能算錯」「同件事別反覆問他確認，他會煩」「做不到就老實講，別假裝」。\n"
    "  壞例子（太長、太專案細節、不要這樣）：「在設計功能模組時必須預先規劃資訊隔離與全域檢索的平衡機制…」。\n"
    "【只抽·嚴格】：\n"
    "①只抽對話裡【真的展現】的相處訊號（Owen 明顯不耐煩、稱讚、糾正做法、說『不要這樣』『我要的是』）。沒有就回 []。\n"
    "②【絕對不要抽】：具體專案/技術需求或功能規格、個人事實、待辦行程、一次性查詢、當下情緒。這些不是『相處守則』。\n"
    "③抽的是【通用態度/溝通方式】，不是這次要做的某個功能。問自己：『這條放到任何話題都適用嗎？』不適用就不要抽。\n"
    "④跟已知守則換句話說的，不要抽。\n"
    '只回 JSON 陣列：[{"insight":"≤30字的相處守則"}]。多數平淡的日子該回 []。'
)


def _read_cursor():
    try:
        return int(open(CURSOR).read().strip())
    except Exception:
        return 0


def _write_cursor(n):
    try:
        with open(CURSOR, "w") as f:
            f.write(str(n))
    except Exception:
        pass


def _new_messages(since_id):
    """讀游標之後的對話（user + assistant 都要，才看得出互動品質）。回 [(id, role, content)]。"""
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
        print("[reflect] read db error:", e)
        return []


def _existing_growth():
    """讀 SOUL 成長區已有的相處守則 → 給 LLM 看，避免重抽。"""
    out = []
    try:
        with open(SOUL_PATH, encoding="utf-8") as f:
            soul = f.read()
        mark = "## 我學到的（自我成長）"
        if mark in soul:
            tail = soul.split(mark, 1)[1]
            for ln in tail.splitlines():
                ln = ln.strip()
                if ln.startswith("- "):
                    out.append(ln[2:])
    except Exception:
        pass
    return out


def _reflect(convo, existing):
    known = "\n".join(f"- {e}" for e in existing) or "（目前沒有已知相處守則）"
    prompt = (
        REFLECT_PROMPT
        + "\n【已經學到的相處守則（這些【不要】再抽，避免重複）】：\n" + known
        + "\n\n【今天的對話】：\n" + convo
    )
    body = {
        "model": "gemini-3.1-flash-lite",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "temperature": 0.2,
    }
    try:
        req = urllib.request.Request(PROXY, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=40))
        txt = r["choices"][0]["message"]["content"].strip()
        if "```" in txt:
            txt = txt.split("```")[1].replace("json", "", 1).strip()
        i, j = txt.find("["), txt.rfind("]")
        if i < 0 or j < 0:
            return []
        return json.loads(txt[i:j + 1])
    except Exception as e:
        print("[reflect] llm error:", e)
        return []


def _evolve(insight):
    try:
        req = urllib.request.Request(SOUL_EVOLVE, data=json.dumps({"insight": insight}).encode(),
                                     headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=15))
        return d.get("ok") and not d.get("skipped")
    except Exception as e:
        print("[reflect] evolve error:", e)
        return False


def main():
    cursor = _read_cursor()
    rows = _new_messages(cursor)
    if not rows:
        print("[reflect] 沒有新對話")
        return
    max_id = max(r[0] for r in rows)
    convo = "\n".join(f"{'Owen' if r[1] == 'user' else 'Jarvis'}：{r[2]}" for r in rows)
    insights = _reflect(convo, _existing_growth())
    added = 0
    for it in insights:
        if isinstance(it, dict) and it.get("insight"):
            ins = str(it["insight"]).strip()
            # safety net：太長的八成是專案細節/規格，不是相處守則 → 跳過（prompt 已要求 ≤30 字）
            if len(ins) > 40:
                print(f"[reflect] 跳過過長洞察：{ins[:30]}…")
                continue
            if _evolve(ins):
                added += 1
                print(f"[reflect] 學到相處：{ins}")
    _write_cursor(max_id)
    print(f"[reflect] 回顧 {len(rows)} 句對話 → 學到 {added} 條相處守則（游標→{max_id}）")


if __name__ == "__main__":
    main()
