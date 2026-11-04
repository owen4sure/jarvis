#!/usr/bin/env python3
"""
記憶整理員（每日自動跑）
- 刪除重複/幾乎重複的事實（保留最新）
- 衝突的舊資料被新的覆蓋（例：「住新北」→ 之後說「搬到台北」→ 只留台北）
- 重建 memory_doc.md（個人）與 system_notes.md（系統筆記），保持分類乾淨

安全：跑之前一定先備份；Gemini 失敗或回傳異常就「不動任何資料」。
"""
import os, json, urllib.request, datetime, shutil

FACTS = os.path.expanduser("~/.hermes/memories/facts.jsonl")
MEMORY_DOC = os.path.expanduser("~/.hermes/memories/memory_doc.md")
SYSTEM_NOTES = os.path.expanduser("~/.hermes/memories/system_notes.md")
BACKUP_DIR = os.path.expanduser("~/.hermes/memories/backups")
LOG = os.path.expanduser("~/.hermes/memories/janitor.log")
GEMINI = "http://127.0.0.1:8808/v1beta/models/gemini-2.5-flash:generateContent"
OLLAMA = "http://127.0.0.1:11434/api/embeddings"
EMB_MODEL = "nomic-embed-text"


def _embed(text):
    try:
        req = urllib.request.Request(OLLAMA,
              data=json.dumps({"model": EMB_MODEL, "prompt": text}).encode(),
              headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=20)).get("embedding")
    except Exception:
        return None

# ---- 分類（與 hermes_memory_endpoint 一致）----
SYSTEM_TAG = "__SYSTEM__"
_SYSTEM_KW = ["telegram bot", ".py", "launchd", "launchagent", "mqtt", "embedding",
              "roadmap", "system_status", "handoff", "稽核", "專案目錄", "config/",
              "daemon", "module", "commit", "修復了", "/research", "/remind", "api key",
              "重啟", "向量庫", "facts_db", "vector_db", "plist", "端點", "8809", "8811",
              "launchctl", "skill.py", "_tracker", "_manager", "webhook"]
_SECT = [("👤 個人資料", ["名字", "我叫", "生日", "住在", "住", "台中", "台北", "捷運", "市府", "租屋", "搬"]),
         ("💼 工作 & 財務", ["上班", "公司", "薪水", "發薪", "面試", "職位"]),
         ("🎨 偏好 & 習慣", ["喜歡", "討厭", "開工", "咖啡", "繁體中文", "預算目標", "stackchan 預算", "習慣"]),
         ("🤖 對 Jarvis 的期待", ["jarvis", "幕僚", "希望你", "我要你"]),
         ("👥 人際關係", ["女朋友", "男朋友", "老婆", "老公", "朋友", "家人", "弟弟", "妹妹", "哥哥", "姐姐", "同事"])]
_DEFAULT = "📌 其他"
_ORDER = ["👤 個人資料", "💼 工作 & 財務", "🎨 偏好 & 習慣", "🤖 對 Jarvis 的期待", "👥 人際關係", _DEFAULT]


def classify(t):
    tl = str(t or "").lower()
    if any(k in tl for k in _SYSTEM_KW):
        return SYSTEM_TAG
    for sec, kws in _SECT:
        if any(k in tl for k in kws):
            return sec
    return _DEFAULT


def log(msg):
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_facts():
    recs = []
    if os.path.exists(FACTS):
        for ln in open(FACTS, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                try:
                    recs.append(json.loads(ln))
                except Exception:
                    pass
    return recs


def ask_gemini(facts):
    """回傳 (keep_indices:set, removed:list[(idx,reason)]) 或 None（失敗→不動）。"""
    listing = "\n".join(f"{i+1}. {r.get('text','')}" for i, r in enumerate(facts))
    prompt = (
        "你是記憶整理員。下面是使用者 Owen 的長期記憶事實，由舊到新編號（編號越大代表越新加入）。\n"
        "請判斷三種處理：\n"
        "1. remove：重複/幾乎重複 → 只留最新（編號最大）那筆，其餘移除。\n"
        "2. remove：整筆被新資料取代的舊事實（同一件事不同值）→ 移除舊的。"
        "例如『住新北市』之後出現『搬到台北市』，移除『住新北市』。\n"
        "3. rewrite：某筆大部分還對、只有一小部分過時（例如一筆含名字+地址的複合事實，只有地址搬了）"
        "→ 不要整筆刪，改用 rewrite 更新成正確的完整文字（保留還對的部分，只改過時處）。\n"
        "彼此不衝突、各自獨立的事實不要動。不確定就保留。\n"
        '只回 JSON：{"remove":[{"id":N,"reason":"..."}],"rewrite":[{"id":N,"text":"更新後完整文字","reason":"..."}]}。'
        '沒事就回 {"remove":[],"rewrite":[]}。\n\n'
        f"事實列表：\n{listing}"
    )
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0.1}}).encode()
    try:
        req = urllib.request.Request(GEMINI, data=body, headers={"Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=40))
        txt = resp["candidates"][0]["content"]["parts"][0]["text"]
        s, e = txt.find("{"), txt.rfind("}")
        data = json.loads(txt[s:e + 1])
        removed, rewrites, rm_ids = [], [], set()
        for item in data.get("remove", []):
            i = int(item.get("id", 0)) - 1
            if 0 <= i < len(facts):
                rm_ids.add(i)
                removed.append((i, item.get("reason", "")))
        for item in data.get("rewrite", []):
            i = int(item.get("id", 0)) - 1
            nt = (item.get("text") or "").strip()
            if 0 <= i < len(facts) and nt and i not in rm_ids:
                rewrites.append((i, nt, item.get("reason", "")))
        keep = set(range(len(facts))) - rm_ids
        return keep, removed, rewrites
    except Exception as e:
        log(f"⚠️ Gemini 失敗，本次不動任何資料：{e}")
        return None


def rebuild_docs(facts):
    personal, system = {}, []
    for r in facts:
        t = r.get("text", "").strip()
        c = classify(t)
        if c == SYSTEM_TAG:
            system.append(t)
        else:
            personal.setdefault(c, []).append(t)
    pd = ["# Jarvis 記得你的事", "", "（只放固定的個人事實。會變的即時抓、聊天不進、系統筆記另存。每日自動去重+覆蓋舊資料）", ""]
    for s in _ORDER:
        if personal.get(s):
            pd.append("## " + s)
            pd += ["- " + f for f in personal[s]]
            pd.append("")
    open(MEMORY_DOC, "w", encoding="utf-8").write("\n".join(pd))
    sd = ["# 系統開發筆記", "", "（程式碼/架構變更紀錄，非個人記憶）", "", "## 🔧 系統開發筆記"]
    sd += ["- " + f for f in system]
    open(SYSTEM_NOTES, "w", encoding="utf-8").write("\n".join(sd) + "\n")
    return sum(len(v) for v in personal.values()), len(system)


def main():
    facts = load_facts()
    if len(facts) < 2:
        log(f"事實只有 {len(facts)} 筆，跳過。")
        return
    # 備份
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy(FACTS, os.path.join(BACKUP_DIR, f"facts.jsonl.{stamp}"))
    # 1) 精準去重（完全相同文字，保留最新=最後出現）
    seen, deduped, exact = set(), [], 0
    for r in reversed(facts):              # 從新到舊掃，新的先佔位
        t = r.get("text", "").strip()
        if t and t not in seen:
            seen.add(t)
            deduped.append(r)
        else:
            exact += 1
    deduped.reverse()
    # 2) 語意去重 + 衝突覆蓋（交給 Gemini）
    result = ask_gemini(deduped)
    if result is None:
        # Gemini 失敗：至少把精準去重的結果存回
        if exact:
            with open(FACTS, "w", encoding="utf-8") as f:
                for r in deduped:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            rebuild_docs(deduped)
            log(f"只做精準去重：移除 {exact} 筆完全重複。")
        return
    keep, removed, rewrites = result
    # 套用改寫（更新複合事實中過時的部分，重新算 embedding）
    applied = 0
    for i, nt, reason in rewrites:
        old = deduped[i].get("text", "")
        emb = _embed(nt)
        if not emb:
            # embedding 失敗就「不改」，保留原事實——絕不寫入沒有向量的事實(會從語意搜尋消失)
            log(f"  ⚠ 改寫跳過(embedding 失敗，保留原文避免無向量)：「{old[:32]}」")
            continue
        deduped[i] = {**deduped[i], "text": nt, "emb": emb}   # 保留原本其他欄位
        applied += 1
        log(f"  改寫：「{old[:32]}」→「{nt[:32]}」 ← {reason}")
    final = [deduped[i] for i in sorted(keep)]
    with open(FACTS, "w", encoding="utf-8") as f:
        for r in final:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    pc, sc = rebuild_docs(final)
    for i, reason in removed:
        log(f"  移除：「{deduped[i].get('text','')[:40]}」 ← {reason}")
    log(f"完成：{len(facts)} → {len(final)} 筆（精準去重 {exact}、衝突移除 {len(removed)}、改寫 {applied}）。個人 {pc}、系統 {sc}。")
    # 通知 8809 重載
    try:
        urllib.request.urlopen("http://127.0.0.1:8809/reload", timeout=5)
    except Exception:
        pass


if __name__ == "__main__":
    main()
