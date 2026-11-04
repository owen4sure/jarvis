"""
記憶書記官（Memory Scribe）
============================
不靠大腦自己記——獨立掃過所有對話，用 cheap LLM 抽取 Owen 親口說的個人事實，
可靠地存進統一記憶（8809/remember，內含去重）。

解決的問題：大腦呼叫 remember_fact「時靈時不靈」（說了記住卻沒實際存）。
書記官是確定性的第二道網：每 N 分鐘掃 state.db 新的 user 訊息 → 抽事實 → 存。

跑法：launchd 每 5 分鐘跑一次（或手動 python memory_scribe.py）。
游標存在 ~/.hermes/memories/.scribe_cursor，記到哪一筆 message id。
"""
import json
import os
import sqlite3
import urllib.request

STATE_DB = os.path.expanduser("~/.hermes/state.db")
CURSOR = os.path.expanduser("~/.hermes/memories/.scribe_cursor")
PROXY = "http://127.0.0.1:8808/v1beta/openai/chat/completions"
REMEMBER = "http://127.0.0.1:8809/remember"
TODAY = __import__("datetime").datetime.now().strftime("%Y-%m-%d")

EXTRACT_PROMPT = (
    f"今天是 {TODAY}。下面是使用者 Owen 跟 AI 助理講的話（只有他講的）。"
    "抽出其中【值得長期記住的、穩定的個人事實】——他的長期偏好/習慣、人生計畫與目標、"
    "決定、人際關係(家人朋友同事寵物)、健康(過敏/疾病)、工作與生活的長期事實。\n"
    "【嚴格規則·寧可少記不要亂記】：\n"
    "①只抽他【親口說過】的長期事實，不要腦補、不要推測、不要把一次性的話當成長期偏好。\n"
    "②【絕對不要抽這些】(它們不是長期事實，已經有別的系統在管)：\n"
    "   ·花費/記帳/某東西多少錢/某東西的價格(例如「茶碗蒸36」「拿鐵85」「飲料150太貴」)——這是記帳，不是事實；\n"
    "   ·設提醒/某天有什麼行程/幾月幾號要做什麼(面試、開會、看牙醫、繳費)——這是提醒系統的事，不要當記憶；\n"
    "   ·他『問的問題』或『一次性的查詢/操作』(查預算、查股票漲幅、放歌、查天氣、問價錢)——問問題不是事實；\n"
    "   ·他『正在考慮/猶豫』還沒決定的事——還沒成為事實；\n"
    "   ·測試性、湊數、或換句話說已知記憶的內容。\n"
    "③純粹當下會過去的情緒/瑣事(累、餓、現在幾點)不要抽。\n"
    "④每筆用完整一句話、含主詞 Owen。\n"
    "判斷標準：問自己『這是不是三個月後還成立、而且值得 AI 記得的他這個人的事？』不是就【不要抽】。\n"
    '只回 JSON 陣列：[{"fact":"完整事實","expire":""}]。多數情況該回 []。'
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


def _new_user_messages(since_id):
    """讀游標之後的 user 訊息（排除工具/操作類短句由 LLM 判斷）。回 [(id, content)]。"""
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT id, content FROM messages WHERE role='user' AND id>? "
            "AND content IS NOT NULL AND length(content)>2 ORDER BY id ASC LIMIT 60",
            (since_id,)).fetchall()
        con.close()
        return rows
    except Exception as e:
        print("[scribe] read db error:", e)
        return []


def _existing_facts():
    """讀已知的記憶（facts.jsonl 的 text）→ 給抽取 LLM 看，避免重抽已知的造成重複。"""
    out = []
    try:
        with open(os.path.expanduser("~/.hermes/memories/facts.jsonl"), encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    t = json.loads(line).get("text", "")
                    if t and not t.startswith("（"):
                        out.append(t)
    except Exception:
        pass
    return out


def _extract(texts, existing):
    """用 cheap LLM 抽【新】事實（給它看已知記憶，只抽沒記過的）。回 [{"fact":..., "expire":...}]。"""
    known = "\n".join(f"- {e}" for e in existing) or "（目前沒有已知記憶）"
    prompt = (
        EXTRACT_PROMPT
        + "\n【已經知道的事（這些【不要】再抽出來，避免重複）】：\n" + known
        + "\n\n【新的對話】只抽上面【沒有】的、真正新的事實。"
          "如果新對話只是把已知的事換句話說，就【不要】抽。\n\nOwen 新說的話：\n"
        + "\n".join(f"- {t}" for t in texts)
    )
    body = {
        "model": "gemini-3.1-flash-lite",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "temperature": 0.1,
    }
    try:
        req = urllib.request.Request(PROXY, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=30))
        txt = r["choices"][0]["message"]["content"].strip()
        # 容錯：可能包了 ```json
        if "```" in txt:
            txt = txt.split("```")[1].replace("json", "", 1).strip()
        i, j = txt.find("["), txt.rfind("]")
        if i < 0 or j < 0:
            return []
        return json.loads(txt[i:j + 1])
    except Exception as e:
        print("[scribe] extract error:", e)
        return []


def _remember(fact, expire=""):
    payload = {"fact": fact}
    if expire:
        payload["expire"] = expire
    try:
        req = urllib.request.Request(REMEMBER, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=15))
        return d.get("ok")
    except Exception as e:
        print("[scribe] remember error:", e)
        return False


def main():
    cursor = _read_cursor()
    rows = _new_user_messages(cursor)
    if not rows:
        return
    max_id = max(r[0] for r in rows)
    texts = [r[1] for r in rows]
    facts = _extract(texts, _existing_facts())   # 給它看已知記憶,只抽新的
    saved = 0
    for f in facts:
        if isinstance(f, dict) and f.get("fact"):
            if _remember(f["fact"], f.get("expire", "")):
                saved += 1
                print(f"[scribe] 記下：{f['fact'][:40]}")
    _write_cursor(max_id)
    print(f"[scribe] 掃 {len(rows)} 句 → 記 {saved} 筆（游標→{max_id}）")


if __name__ == "__main__":
    main()
