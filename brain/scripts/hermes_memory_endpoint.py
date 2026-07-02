"""
hermes_memory_endpoint — Hermes RAG 記憶系統（給 xiaozhi 伺服器）
============================================================================
- 記憶寫進無上限的 facts.jsonl（每筆含文字 + nomic embedding）。
- /query：語意搜尋撈出與當下問題最相關的記憶（RAG），xiaozhi 的 Memory provider
  每次對話即時呼叫 → 第一句就有記憶、永不被擠掉。
- /remember：寫入新記憶。/hermes_memory：context_provider 用（即時時間）。
啟動：launchd com.hermes.memoryendpoint（port 8809）。
"""
import datetime
import json
import math
import os
import re
import urllib.request
import threading
import zoneinfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

FACTS_PATH = os.path.expanduser("~/.hermes/memories/facts.jsonl")
USER_PATH = os.path.expanduser("~/.hermes/memories/USER.md")
MEMORY_PATH = os.path.expanduser("~/.hermes/memories/MEMORY.md")
OLLAMA = "http://127.0.0.1:11434/api/embeddings"
EMB_MODEL = "nomic-embed-text"
TOPK = 6

app = FastAPI(title="Hermes RAG Memory")
_facts = []  # [{"text":..., "emb":[...]}]
_FACTS_LOCK = __import__("threading").Lock()  # 保護 _facts + facts.jsonl 的並發讀改寫(_add/_forget/_purge/_load)


def _notify(ev, data=""):
    """資料一變動就推事件給 dashboard（8811）→ 前端 SSE 自動更新對應區塊，
    使用者不必手動刷新。fire-and-forget，失敗不影響主流程。"""
    try:
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8811/api/event",
            data=json.dumps({"type": ev, "data": data}).encode(),
            headers={"Content-Type": "application/json"}), timeout=2)
    except Exception:
        pass


def _t2t(s):
    """簡體中文 → 繁體（台灣用語）。所有寫入的紀錄都過這層，確保不出現簡體字。
    註：zhconv 會把「台」轉成「臺」，但台灣日常（台積電/台灣/台北）習慣用「台」，故轉回。"""
    if not isinstance(s, str) or not s:
        return s
    try:
        from zhconv import convert
        return convert(s, "zh-tw").replace("臺", "台")
    except Exception:
        return s


def _embed(text: str):
    req = urllib.request.Request(
        OLLAMA,
        data=json.dumps({"model": EMB_MODEL, "prompt": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=20)).get("embedding")


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _add(text: str, expire: str = ""):
    try:
        emb = _embed(text)   # 20 秒阻塞在鎖外做，避免長時間佔鎖擋住其他記憶操作
        if not emb:
            return False
        with _FACTS_LOCK:
            # 去重:主動記憶容易把同一件事存好幾次。已有高度相似(或同子字串)的記憶就不重複存。
            for r in _facts:
                t = r.get("text", "")
                if t and text and (text == t or text in t or t in text):
                    return True   # 視為已存
                if r.get("emb") and _cos(emb, r["emb"]) >= 0.93:
                    return True   # 語意幾乎相同 → 不新增
            rec = {"text": text, "emb": emb}
            # 有時效的事件(旅行/約會/考試)帶到期日 YYYY-MM-DD,過了會被 _purge_expired 自動清掉。
            if expire and isinstance(expire, str) and len(expire.strip()) >= 8:
                rec["expire"] = expire.strip()[:10]
            _facts.append(rec)
            os.makedirs(os.path.dirname(FACTS_PATH), exist_ok=True)
            with open(FACTS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print("[memory] add error:", e)
        return False


def _purge_expired():
    """清掉已過期的有時效記憶(expire < 今天)。回傳清掉的筆數。
    這樣「下個月要去日本」這種事件過了就不會永遠留著,只有偏好/人際等無 expire 的長期記憶會保留。"""
    global _facts
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    # ★整段讀+寫都在鎖內★：原本在鎖外算 kept、鎖內才 reassign，中間若 _add 新增一筆
    # 會被舊 kept 覆蓋而永久丟失（記憶誤刪，已知過的坑）。
    with _FACTS_LOCK:
        kept, removed = [], 0
        for r in _facts:
            exp = r.get("expire")
            if exp and exp < today:
                removed += 1
                continue
            kept.append(r)
        if removed:
            _facts = kept
            try:
                with open(FACTS_PATH, "w", encoding="utf-8") as f:
                    for r in _facts:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                global _facts_mtime
                _facts_mtime = os.path.getmtime(FACTS_PATH)
                print(f"[memory] 清掉 {removed} 筆過期記憶")
            except Exception as e:
                print("[memory] purge write error:", e)
    return removed


_facts_mtime = 0.0  # facts.jsonl 上次載入時的修改時間，用來偵測 dashboard 直接改檔


_usermd_mtime = 0.0  # 上次我們自己寫 USER.md 後的 mtime，用來分辨「外部(hermes-agent)改的」


def _maybe_reload():
    """若 facts.jsonl 或 USER.md 被外部改動過 → 自動同步，讓記憶體/檔案/三端即時一致（雙向打通）。
    facts.jsonl 外部改 = dashboard 編輯；USER.md 外部改 = hermes-agent 大腦自己策展記憶。"""
    global _facts_mtime, _usermd_mtime
    try:
        m = os.path.getmtime(FACTS_PATH) if os.path.exists(FACTS_PATH) else 0.0
        if m != _facts_mtime:
            _load()
            # 【關鍵·之前漏掉】facts.jsonl 被外部改(dashboard 編輯/刪除、scribe、手動清)→ 也要重生 USER.md，
            # 否則 hermes-agent 還讀舊的、刪除不會 propagate = 分岔。補上這個方向才是真·雙向同步。
            _sync_user_md()
        # USER.md 被 hermes-agent 改動(mtime 跟我們上次寫的不同)→ 撈它的新記憶進 facts.jsonl
        um = os.path.getmtime(USER_PATH) if os.path.exists(USER_PATH) else 0.0
        if um and um != _usermd_mtime:
            _sync_user_md()  # 內含 _import_external_usermd(只撈真·外部新增) + 重生 + 更新 _usermd_mtime
    except Exception as e:
        print("[memory] reload check error:", e)


def _load():
    """載入 facts.jsonl；首次啟動把舊的 USER.md/MEMORY.md 段落遷移進來。"""
    global _facts, _facts_mtime
    if os.path.exists(FACTS_PATH):
        # ★先讀進 local list、只在 assign 時上鎖★：原本直接 `_facts=[]` 再逐筆 append，
        # 若與並發的 _add 同時跑會互相清空/覆蓋（記憶誤刪）。不能整段包鎖，因為 migration 分支
        # 會呼叫 _add（自己也取鎖）→ 巢狀 deadlock；所以只有 normal 分支的 assign 進鎖。
        try:
            mt = os.path.getmtime(FACTS_PATH)
        except Exception:
            mt = 0.0
        new_facts = []
        with open(FACTS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        new_facts.append(json.loads(line))
                    except Exception:
                        pass
        with _FACTS_LOCK:
            _facts = new_facts
            _facts_mtime = mt
    else:
        with _FACTS_LOCK:
            _facts = []
        for p in (USER_PATH, MEMORY_PATH):
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    for para in f.read().split("\n§\n"):
                        para = para.strip()
                        if len(para) > 4:
                            _add(para)   # _add 自取鎖，且此處已釋放鎖，無巢狀
    print(f"[memory] loaded {len(_facts)} facts")


def _retrieve(query: str, k=TOPK):
    _maybe_reload()  # 每次查詢前確認與檔案同步（看得到 dashboard 剛改的記憶）
    if not _facts:
        return []
    # 混合檢索：關鍵字命中(含查詢任一2字以上詞)優先 + 語意 top-k
    q = query.strip()
    # 只用結構完整的筆（有 text + emb），避免手動編輯／舊格式壞筆讓整個 /query 崩
    good = [f for f in _facts if isinstance(f, dict) and f.get("text")]
    keyword_hits = []
    if q:
        # 用整句 + 逐2字滑窗找子字串命中（中文無空白）
        terms = {q}
        for i in range(len(q) - 1):
            terms.add(q[i:i + 2])
        for f in good:
            t = f["text"]
            if any(term in t for term in terms if len(term) >= 2):
                keyword_hits.append(t)
    try:
        qe = _embed(query)
        emb_ok = [f for f in good if isinstance(f.get("emb"), list)]
        sem = [f["text"] for f in sorted(emb_ok, key=lambda f: _cos(qe, f["emb"]),
                                         reverse=True)[:k]] if qe else []
    except Exception:
        # embedding 服務掛了 → 只回關鍵字命中，【不要】回「最新幾筆」當語意結果：
        # 那些跟問題無關，會被大腦當成相關上下文亂用（已知會導致答非所問）。
        sem = []
    # 關鍵字命中放最前面（規則類觸發詞靠這個），再補語意
    out = []
    for t in keyword_hits + sem:
        if t not in out:
            out.append(t)
    return out[:k + 3]


def _now():
    n = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Taipei"))
    wd = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][n.weekday()]
    return f"{n.year}年{n.month}月{n.day}日 {wd} {n.strftime('%H:%M')}"


@app.post("/query")
async def query(req: Request):
    """xiaozhi Memory provider 每次對話呼叫：回傳與 query 最相關的記憶。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = (body or {}).get("query", "")
    if not isinstance(q, str):           # 非字串(int/None/list)→ 轉字串，避免 .strip() 崩
        q = str(q) if q is not None else ""
    # _retrieve 內含 embedding 的阻塞 HTTP（最長20s）→ 丟執行緒，不凍住整個記憶服務
    import asyncio as _aio
    if q:
        hits = await _aio.get_event_loop().run_in_executor(None, _retrieve, q)
    else:
        hits = [f["text"] for f in _facts[-TOPK:]]
    return JSONResponse({"ok": True, "memory": "；".join(hits), "count": len(hits)})


# ---------- 記憶自動分類（新記憶進來時歸到對應分區的 markdown 文件）----------
MEMORY_DOC = os.path.expanduser("~/.hermes/memories/memory_doc.md")
_MEM_SECTIONS = [
    ("👤 個人資料", ["名字", "我叫", "幾歲", "歲", "生日", "住在", "住", "出生", "血型",
                  "身高", "體重", "星座", "台灣人", "電話", "信箱", "email"]),
    ("💼 工作 & 財務", ["上班", "工作", "公司", "薪水", "發薪", "賺", "投資", "股票", "持股",
                    "財務", "職位", "銀行", "收入", "存款", "房租", "老闆", "客戶", "面試",
                    "預算", "錢"]),
    ("🎨 偏好 & 習慣", ["喜歡", "討厭", "習慣", "最愛", "口味", "顏色", "音樂", "播放",
                    "開工", "興趣", "愛吃", "愛喝", "運動", "健身", "睡覺", "起床", "每天都"]),
    ("👥 人際關係", ["女朋友", "男朋友", "老婆", "老公", "朋友", "家人", "媽媽", "爸爸",
                  "小美", "同事", "哥哥", "姐姐", "弟弟", "妹妹", "主管", "女友", "男友", "親戚"]),
    ("🤖 對 Jarvis 的期待", ["希望你", "要你", "Jarvis", "賈維斯", "助理", "風格", "幕僚",
                          "你要主動", "你應該", "我要你", "提醒我要"]),
]
_MEM_DEFAULT = "📌 其他"

# 系統/開發筆記 → 不進個人記憶,改存 system_notes.md（保持個人記憶乾淨）
SYSTEM_NOTES = os.path.expanduser("~/.hermes/memories/system_notes.md")
SYSTEM_TAG = "__SYSTEM__"
_SYSTEM_KW = ["telegram bot", ".py", "launchd", "launchagent", "mqtt", "embedding",
              "roadmap", "system_status", "handoff", "稽核", "專案目錄", "config/",
              "daemon", "module", "commit", "修復了", "/research", "/remind", "api key",
              "重啟", "向量庫", "facts_db", "vector_db", "plist", "端點", "8809", "8811",
              "launchctl", "skill.py", "_tracker", "_manager", "webhook"]


# 明顯在描述「人」的事實 → 一定是個人記憶，不可被 .py/8809 之類系統字眼誤分到系統筆記
_PERSONAL_STRONG = ["住", "上班", "公司", "喜歡", "討厭", "生日", "媽", "爸",
                    "女友", "男友", "老婆", "老公", "養", "名字", "叫", "薪水", "發薪"]


def _classify_section(text):
    t = str(text or "")
    tl = t.lower()
    # 個人事實優先：我/Owen/使用者 開頭 + 個人生活關鍵字 → 直接走個人分類，跳過系統判斷
    if (t.startswith("我") or t.startswith("Owen") or t.startswith("使用者")) \
            and any(k in t for k in _PERSONAL_STRONG):
        for sec, kws in _MEM_SECTIONS:
            if any(k.lower() in tl for k in kws):
                return sec
        return _MEM_DEFAULT
    # 其餘才判系統/開發筆記(優先),這類絕不進個人記憶
    if any(k in tl for k in _SYSTEM_KW):
        return SYSTEM_TAG
    for sec, kws in _MEM_SECTIONS:
        if any(k.lower() in tl for k in kws):
            return sec
    return _MEM_DEFAULT


def _doc_insert(text, section):
    """把記憶插入對應檔案的對應分區。系統筆記→system_notes.md；個人事實→memory_doc.md。"""
    try:
        # 路由：系統/開發筆記寫到獨立檔，個人事實寫到個人記憶
        if section == SYSTEM_TAG:
            target, section = SYSTEM_NOTES, "🔧 系統開發筆記"
        else:
            target = MEMORY_DOC
        doc = ""
        if os.path.exists(target):
            with open(target, encoding="utf-8") as f:
                doc = f.read()
        bullet = "- " + text
        if text.strip() and (bullet in doc or ("- " + text.strip()) in doc):
            return  # 已經有了
        lines = doc.splitlines()
        idx = None
        for i, ln in enumerate(lines):
            if ln.strip().startswith("##") and section in ln:
                idx = i
                break
        if idx is None:                       # 沒這分區 → 新增到最後
            if lines and lines[-1].strip():
                lines.append("")
            lines.append("## " + section)
            lines.append(bullet)
        else:                                 # 插在該分區內容末（下個 ## 前、跳過尾部空行）
            j = idx + 1
            while j < len(lines) and not lines[j].strip().startswith("##"):
                j += 1
            insert_at = j
            while insert_at - 1 > idx and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, bullet)
        with open(target, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip("\n") + "\n")
    except Exception as e:
        print(f"⚠️ [doc_insert] {e}")


import re as _re_dyn
_DYNAMIC_PATTERNS = [
    r"報酬率?\s*[:：]?\s*-?\d", r"本期收支", r"收支快報", r"財務.{0,4}快報",
    r"餘額", r"淨資產", r"目前表現", r"今天.{0,8}花[了費]", r"今天.{0,8}花了\s*\d",
    r"今天.{0,6}共?花", r"股價.{0,4}\d", r"漲了|跌了",
    r"現在是.{0,8}[:點時]", r"目前.{0,8}\d+\s*元",
]


def _is_dynamic_data(t: str) -> bool:
    """判斷是不是『會變動的資料』(不該存記憶)。固定的目標/預算/設定/偏好放行。"""
    if not isinstance(t, str):
        return False
    # 固定設定類放行(例如「StackChan 預算目標每月5美元」「希望用繁中」)
    if any(w in t for w in ("目標", "預算", "設定", "希望", "規則", "偏好", "喜歡", "討厭", "生日", "住", "上班", "名字")):
        return False
    return any(_re_dyn.search(p, t) for p in _DYNAMIC_PATTERNS)


@app.post("/remember")
async def remember(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    _fact = (body or {}).get("fact", "")
    fact = _t2t(_fact.strip()) if isinstance(_fact, str) else ""
    if not fact:
        return JSONResponse({"ok": False, "error": "empty"})
    # 有時效事件的到期日(YYYY-MM-DD):過了就會被自動清掉,不會永遠留「下個月要去日本」這種過期計畫。
    expire = str((body or {}).get("expire", "")).strip()[:10]
    # 安全閘:新存的事實若 expire 已是過去(模型把日期算錯,例如年份填成2024)→ 不設 expire,
    # 免得剛存就被當過期清掉。寧可不自動清、也不要誤刪使用者真正的未來計畫。
    if expire:
        from datetime import datetime as _dtt
        if expire < _dtt.now().strftime("%Y-%m-%d"):
            expire = ""
    # 🚫 會變的動態資料不存記憶（記憶只放固定事實；收支/報酬/餘額/花費 改即時抓）
    if _is_dynamic_data(fact):
        return JSONResponse({"ok": False, "error": "dynamic",
                             "reason": "會變動的資料(收支/報酬/餘額/當日花費/股價)不存記憶，要用即時查詢"})
    _purge_expired()  # 順手清掉已過期的舊記憶
    import asyncio as _aio
    ok = await _aio.get_event_loop().run_in_executor(None, _add, fact, expire)
    section = _classify_section(fact)
    _doc_insert(fact, section)   # 自動歸類到結構化記憶文件
    _sync_user_md()               # 同步回 USER.md → hermes-agent CLI 也讀到新事實(單一大腦)
    _notify("memory", fact[:30])  # 推給 dashboard 自動更新記憶頁
    # 回傳給使用者的分類用「人看得懂的標籤」，不要外洩內部 sentinel __SYSTEM__
    disp_section = "🔧 系統開發筆記" if section == SYSTEM_TAG else section
    return JSONResponse({"ok": ok, "remembered": fact, "section": disp_section,
                         "total": len(_facts)})


@app.post("/forget")
async def forget(req: Request):
    """語音「忘記X／那個記錯了／刪掉那個記憶」用：語意找出最相符的一筆記憶刪掉。
    記憶系統終於有了『刪』，配合 remember_fact 的『加』，語音就能完整改記憶（之前只能加）。"""
    global _facts, _facts_mtime
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = str((body or {}).get("query", "")).strip()
    if not q:
        return JSONResponse({"ok": False, "error": "empty"})
    # _embed 是 20 秒阻塞 HTTP(Ollama)+ 要改 _facts 寫檔 → 整段丟執行緒，
    # 絕不卡 8809 事件迴圈；改檔在 _FACTS_LOCK 內，避免跟 _add/_purge 併發壞檔。
    import asyncio as _aio
    result = await _aio.get_event_loop().run_in_executor(None, _forget_sync, q)
    return JSONResponse(result)


def _forget_sync(q: str) -> dict:
    global _facts, _facts_mtime
    _maybe_reload()
    qe = _embed(q)
    with _FACTS_LOCK:
        best_i, best_sim = -1, -1.0
        for i, r in enumerate(_facts):
            t = r.get("text", "")
            if not t:
                continue
            # 子字串(說的詞出現在記憶裡，或反之)直接視為高相符；否則用向量餘弦相似度
            sim = 1.0 if (q in t or t in q) else (
                _cos(qe, r.get("emb")) if qe and r.get("emb") else 0.0)
            if sim > best_sim:
                best_i, best_sim = i, sim
        # 門檻：要很相符才刪。刪錯記憶比沒刪到嚴重得多（誤刪使用者真記憶超糟），
        # 所以門檻拉高到 0.80（子字串完全吻合會是 1.0 一定過；只有語意「幾乎一樣」才刪）。
        if best_i >= 0 and best_sim >= 0.80:
            removed = _facts[best_i].get("text", "")
            _facts.pop(best_i)
            try:
                with open(FACTS_PATH, "w", encoding="utf-8") as f:
                    for r in _facts:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                _facts_mtime = os.path.getmtime(FACTS_PATH)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            _notify("memory", "忘記:" + removed[:20])
            return {"ok": True, "forgot": removed,
                    "sim": round(best_sim, 2), "total": len(_facts)}
    return {"ok": False, "error": "not_found",
            "reason": "找不到夠相符的記憶可以忘記"}


def _match_by_text(q, items, get_text):
    """在 items 裡找文字跟 q 最相符的一筆(子字串雙向)。回 (index, item) 或 (-1, None)。"""
    q = (q or "").strip()
    if not q:
        return -1, None
    for i, it in enumerate(items):
        t = (get_text(it) or "").strip()
        if t and (q in t or t in q):
            return i, it
    return -1, None


def _match_all(q, items, get_text):
    """回所有文字跟 q 相符的 item。刪除類用：多筆相符時要反問、不要亂刪第一筆。"""
    q = (q or "").strip()
    if not q:
        return []
    out = []
    for it in items:
        t = (get_text(it) or "").strip()
        if t and (q in t or t in q):
            out.append(it)
    return out


@app.post("/reminder_cancel")
async def reminder_cancel(req: Request):
    """語音「取消X提醒／把那個提醒刪掉／不用提醒我X了」用。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = str((body or {}).get("query", "")).strip()
    from modules.productivity import reminder_manager as rm
    rs = rm.list_reminders()
    ms = _match_all(q, rs, lambda r: r.get("message", ""))
    if not ms and len(rs) == 1:
        ms = [rs[0]]  # 只有一個提醒,使用者說「取消提醒」就取消它
    if len(ms) > 1:
        return JSONResponse({"ok": False, "multiple": True,
                             "reason": "有多個相符的提醒：" + "、".join(r.get("message", "") for r in ms[:5]) + "，請說清楚是哪一個"})
    if len(ms) == 1:
        m = ms[0]
        rm.remove_reminder(m.get("id"))
        return JSONResponse({"ok": True, "cancelled": m.get("message", ""),
                             "time": m.get("time", "")})
    return JSONResponse({"ok": False, "reason": "找不到相符的提醒"})


@app.post("/expense_delete")
async def expense_delete(req: Request):
    """語音「刪掉那筆X花費／剛記錯了那筆X」用。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = str((body or {}).get("query", "")).strip()
    from modules.productivity import expense_tracker as et
    recent = list(reversed(et.list_recent(days=21)))  # 最近的優先比對
    ms = _match_all(
        q, recent, lambda e: f"{e.get('note', '')} {e.get('category', '')} {e.get('amount', '')}")
    if len(ms) > 1:
        return JSONResponse({"ok": False, "multiple": True,
                             "reason": "有多筆相符的花費：" + "、".join(f"{e.get('category','')}{int(e.get('amount',0))}元" for e in ms[:5]) + "，請說清楚是哪一筆(可講金額)"})
    if len(ms) == 1:
        m = ms[0]
        et.remove_expense(m.get("id"))
        return JSONResponse({"ok": True,
                             "deleted": f"{m.get('category', '')} {int(m.get('amount', 0))}元",
                             "note": m.get("note", "")})
    return JSONResponse({"ok": False, "reason": "找不到相符的花費"})


@app.post("/todo_complete")
async def todo_complete(req: Request):
    """語音「X做完了／完成X／那個待辦好了／刪掉待辦X」用。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = str((body or {}).get("query", "")).strip()
    from modules.productivity import checklist_manager as cm
    items = cm.get_items("todo")   # 待辦專用清單，跟出門清單(out)分開
    ms = _match_all(q, items, lambda it: it if isinstance(it, str) else str(it))
    if len(ms) > 1:
        return JSONResponse({"ok": False, "multiple": True,
                             "reason": "有多個相符的待辦：" + "、".join(str(it) for it in ms[:5]) + "，請說清楚是哪一個"})
    if len(ms) == 1:
        cm.remove_item(ms[0], "todo")
        return JSONResponse({"ok": True, "done": ms[0]})
    return JSONResponse({"ok": False, "reason": "找不到相符的待辦"})


def _load_user_profile():
    """從【facts.jsonl(唯一真相、dashboard 改的就是這個)】組出 Owen 的個人檔案,
    整份【永遠注入 system prompt】,不靠 RAG 檢索 → 核心事實永遠在腦裡、不會「講過又忘」。
    過濾掉 meta 標記列(以（開頭的說明)和系統筆記,只留真正的個人事實。"""
    _maybe_reload()  # 跟檔案同步,dashboard 剛改的立刻反映(單一大腦)
    lines = []
    for r in _facts:
        if not isinstance(r, dict):
            continue
        t = (r.get("text") or "").strip()
        if not t or t.startswith("（") or t.startswith("("):
            continue  # 跳過 meta 說明列
        if r.get("section") == SYSTEM_TAG or r.get("type") == "system":
            continue  # 跳過系統筆記
        lines.append(f"・{t}")
    return "\n".join(lines)


# 上次同步寫進 USER.md 的條目快照 → 用來分辨「被刪的」vs「CLI 新寫的」(避免刪除後又被復活)。
_USERMD_SNAPSHOT = os.path.expanduser("~/.hermes/memories/.usermd_synced.json")


def _read_usermd_chunks():
    if not os.path.exists(USER_PATH):
        return []
    with open(USER_PATH, encoding="utf-8") as f:
        raw = f.read()
    out = []
    for part in raw.replace("\n§\n", "§").split("§"):
        p = part.strip()
        if len(p) > 4:
            out.append(p)
    return out


def _import_external_usermd():
    """雙向安全:把 USER.md 裡【hermes-agent CLI 真的新寫】的條目撈回 facts.jsonl(不被蓋掉)。
    關鍵:用快照分辨「被刪的」vs「新寫的」——只有『不在 facts 也不在上次同步快照』的才是 CLI 新寫 →
    匯入;『在快照但 facts 已無』= 使用者刪掉的 → 不復活。"""
    try:
        try:
            with open(_USERMD_SNAPSHOT, encoding="utf-8") as f:
                snap = set(json.load(f))
        except Exception:
            snap = set()
        existing = [(r.get("text") or "") for r in _facts if isinstance(r, dict)]
        for c in _read_usermd_chunks():
            if any(c == e or c in e or e in c for e in existing if e):
                continue            # facts 已有 → 跳過
            if c in snap:
                continue            # 上次是我們寫的、現在 facts 沒了 → 是被刪的,不復活
            # 【防改寫重複】gateway 會改寫/濃縮 USER.md——同一件事換句話說會繞過上面的子字串比對，
            # 日積月累養出近似重複記憶。匯入前先做語意相似檢查(門檻比 _add 的 0.93 再鬆一點)，
            # 跟既有記憶太像的改寫版就不匯入。
            try:
                _ce = _embed(c)
                if _ce and any(_cos(_ce, r.get("emb")) >= 0.88
                               for r in _facts if isinstance(r, dict) and isinstance(r.get("emb"), list)):
                    continue        # 是既有記憶的改寫版 → 不重複匯入
            except Exception:
                pass
            _add(c)                 # 真·CLI 新寫 → 匯入 facts.jsonl
    except Exception as e:
        print("[memory] import USER.md error:", e)


def _write_usermd_snapshot(items):
    try:
        with open(_USERMD_SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception:
        pass


def _sync_user_md():
    """把 facts.jsonl(唯一真相)的個人事實寫回 USER.md,讓 hermes-agent CLI 讀到同一份。
    USER.md 從此是 facts.jsonl 的【衍生視圖】,不再各自為政 → 單一大腦、永不分歧。
    先撈回 CLI 真的新寫的條目(雙向安全),再重生,並存快照供下次分辨刪除。"""
    try:
        _import_external_usermd()
        items = []
        for r in _facts:
            if not isinstance(r, dict):
                continue
            t = (r.get("text") or "").strip()
            if not t or t.startswith("（") or t.startswith("("):
                continue
            if r.get("section") == SYSTEM_TAG or r.get("type") == "system":
                continue
            items.append(t)
        if not items:
            return
        os.makedirs(os.path.dirname(USER_PATH), exist_ok=True)
        with open(USER_PATH, "w", encoding="utf-8") as f:
            f.write("\n§\n".join(items) + "\n")
        _write_usermd_snapshot(items)   # 記下這次寫了什麼 → 下次能分辨「被刪」vs「CLI新寫」
        global _usermd_mtime            # 記下我們自己寫後的 mtime → _maybe_reload 不會把自己的寫當成外部改動
        _usermd_mtime = os.path.getmtime(USER_PATH)
    except Exception as e:
        print("[memory] sync USER.md error:", e)


@app.get("/hermes_memory")
def hermes_memory():
    """context_provider：每輪都注入【facts.jsonl 的個人事實】＋即時時間。
    這是「不會忘」的關鍵:核心事實常駐 prompt,不再只靠 RAG 撈(撈不到就忘)。"""
    data = {"現在的真實時間(台灣，以此為準)": _now()}
    profile = _load_user_profile()
    if profile:
        data["你已經知道的 Owen 的事（這些是長期事實，直接用、不用再問他）"] = profile
    # 【SOUL 相處守則注入】self_reflect 夜間學到「怎麼跟 Owen 相處」寫在 SOUL 成長區，
    # 但 SOUL 本來沒被注入大腦(等於白學)。這裡把守則塞進每輪 context，真的生效。
    try:
        with open(SOUL_PATH, encoding="utf-8") as _sf:
            _soul = _sf.read()
        if _SOUL_GROWTH_MARK in _soul:
            _rules = [ln.strip()[2:].strip()
                      for ln in _soul.split(_SOUL_GROWTH_MARK, 1)[1].splitlines()
                      if ln.strip().startswith("- ") and "還沒有" not in ln and "慢慢長出來" not in ln]
            if _rules:
                data["你跟 Owen 相處學到的守則（務必照著做）"] = "；".join(_rules)
    except Exception:
        pass
    return JSONResponse({"code": 0, "data": data})


@app.post("/reminder")
async def reminder(req: Request):
    """設提醒：寫入 config/reminders.json，reminder_daemon 會到點唸出來。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    _t = (b or {}).get("time", "")
    t = _t.strip() if isinstance(_t, str) else ""
    _msg = (b or {}).get("message", "")
    msg = _t2t(_msg.strip()) if isinstance(_msg, str) else ""
    # 使用者原話裡有沒有「明確指定時間」(幾點/早上下午晚上...)→ 用來判斷要不要套用作息預設時間。
    _has_explicit_time = bool(re.search(
        r"\d{1,2}\s*[:點時]|半夜|凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|今晚|明晚|[ap]m", f"{_t} {_msg}"))
    repeat = (b or {}).get("repeat", "once")
    lead = (b or {}).get("lead_minutes", 0)        # 提早幾分鐘提醒
    channel = (b or {}).get("channel", "both")     # both / telegram / voice
    # 【後端自動抓「提早N天」】不靠 agent 拆對 advance_days：從 time/message 整句話找「提早X天/前X天」。
    _adv = (b or {}).get("advance_days")
    if not _adv:
        _advs = re.findall(r"(?:提早|提前|前)\s*([0-9一二兩三四五六七八九十]+)\s*天", f"{t} {msg}")
        _cn = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        _adv = []
        for _x in _advs:
            try:
                _adv.append(int(_x))
            except ValueError:
                _adv.append(_cn.get(_x, 0))
        _adv = [n for n in _adv if n > 0]
        if _adv:
            b = dict(b or {}); b["advance_days"] = _adv  # 讓後面的 advance 邏輯吃到
    # 時間不是標準 HH:MM（例如語音給「禮拜五早上11點」）→ 先試標準/ISO 格式，再退回中文解析器。
    if t and not re.match(r"^\d{1,2}:\d{2}$", t):
        # 【先吃 ISO/標準格式】agent 常傳「2026-07-15 14:00」這種；parse_when 反而會把日期弄丟，所以先在這攔。
        _iso = None
        _date_only = False
        for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                     "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                _iso = datetime.datetime.strptime(t.strip(), _fmt)
                _date_only = _fmt in ("%Y-%m-%d", "%Y/%m/%d")
                break
            except ValueError:
                continue
        if _iso:
            if _date_only:
                _iso = _iso.replace(hour=9, minute=0)  # 只給日期沒給時間 → 預設早上9點
            t = _iso.strftime("%H:%M")
            repeat = "once:" + _iso.strftime("%Y-%m-%d")
        else:
          try:
            from modules.productivity import nl_datetime as nl
            # 【日期容錯】agent 常把日期放進 message(例如 time=「下午兩點」、msg=「7月15號面試」)。
            # 先試 t；若 t 解析不到「明確日期」(只有時間→當今天)，就把 message 一起餵進去找日期。
            fire, extracted = nl.parse_when(t)
            # 注意：不要用裸「下」當關鍵字——會誤中「下午」。用明確的「下週/下個/下禮拜/下星期」。
            _has_date = bool(fire) and any(k in t for k in (
                "月", "號", "日", "明天", "後天", "大後天", "下週", "下个", "下個",
                "下禮拜", "下星期", "星期", "禮拜", "週", "/"))
            if (not fire or not _has_date) and msg:
                fire2, extracted2 = nl.parse_when((t + " " + msg).strip())
                if fire2:
                    fire, extracted = fire2, (extracted or extracted2)
            if fire:
                if not msg and extracted:
                    msg = _t2t(extracted)
                # 清掉訊息開頭的「日期範圍殘留」(例如「10/23-26要回台中」被抓走10/23後，剩「-26要回台中」)。
                msg = re.sub(r"^\s*[-~–到至]\s*\d{1,2}(?:\s*[/月]\s*\d{1,2})?\s*[日號]?\s*", "", msg)
                # 清掉訊息尾巴的「提早N天/提前N天…提醒我」「提醒我」殘句，讓提醒內容乾淨。
                msg = re.sub(r"[,，、]?\s*(?:提早|提前|前)\s*[0-9一二兩三四五六七八九十]+\s*天(?:再|先)?(?:提醒我)?", "", msg)
                msg = re.sub(r"[,，、]?\s*(?:記得)?提醒我[一下]*$", "", msg).strip("，,、 ")
                # 清掉問句尾巴(「那時有沒有連假嗎」「放假嗎」等)——這些是 Owen 在問、不是行程內容。
                msg = re.sub(r"[,，、]?\s*(?:那時|當時|這幾天|到時候?)?\s*(?:有沒有|有|是不是)?\s*(?:連假|放假|假期|休假)[嗎呢]?[?？]?\s*$", "", msg).strip("，,、 ?？")
                t = fire.strftime("%H:%M")
                repeat = "once:" + fire.strftime("%Y-%m-%d")
          except Exception:
            pass
    if not t or not msg or not re.match(r"^\d{1,2}:\d{2}$", t):
        return JSONResponse({"ok": False, "error": "時間看不懂",
                             "text": "我沒抓到時間耶，可以說「明天下午3點」或「禮拜五早上11點」這樣嗎？"})
    # 一次性提醒：補上日期（若時間已過則排明天）
    if repeat in ("once", "", None):
        now = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Taipei"))
        day = now
        try:
            hh, mm = map(int, t.split(":"))
            if (hh, mm) <= (now.hour, now.minute):
                day = now + datetime.timedelta(days=1)
        except Exception:
            pass
        repeat = "once:" + day.strftime("%Y-%m-%d")
    # 【作息預設時間】沒明確指定時間的提醒 → 早上 7 點(Owen 7點前起床、7:40出門，9點他已上班來不及)。
    if not _has_explicit_time:
        t = "07:00"
    # 【防呆】once 提醒若「就是今天、時間已過補發窗(30分)」→ 別默默建個永遠不會響的提醒(daemon 只補發過點30分內)。
    # 明確日期解析出的過去時間會走到這(bare time 已在上面排明天了)。直接問清楚要今天晚點還是明天。
    if str(repeat).startswith("once:"):
        try:
            _tz = zoneinfo.ZoneInfo("Asia/Taipei")
            _n = datetime.datetime.now(_tz)
            _rt = datetime.datetime.strptime(repeat[5:] + " " + t, "%Y-%m-%d %H:%M").replace(tzinfo=_tz)
            if (_n - _rt).total_seconds() > 30 * 60:
                return JSONResponse({"ok": False, "error": "時間已過",
                    "text": f"你說的 {t} 已經過了耶（現在 {_n.strftime('%H:%M')}）。要今天晚點、還是明天這個時間？跟我說我幫你排。"})
        except Exception:
            pass
    try:
        from modules.productivity import reminder_manager as rm
        rm.add_reminder(t, msg, repeat=repeat, lead_minutes=lead, channel=channel)
        # 提早「幾天」通知（重要日子）：事件前 N 天另排一筆早上 9 點的提早通知。
        # advance_days 可以是數字(3) 或清單([3,1])，幫你前 3 天、前 1 天各提醒一次。
        advance = (b or {}).get("advance_days")
        # 【自動提早提醒】使用者沒指定提早幾天時，未來事件越遠就自動加提早通知，
        # 不會「重要的事到當天才提醒」。近期(<3天)不加(明天的事不用提早)。
        if not advance and str(repeat).startswith("once:"):
            try:
                _ev = datetime.datetime.strptime(repeat[5:], "%Y-%m-%d").date()
                _td = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Taipei")).date()
                _days = (_ev - _td).days
                if _days >= 14:
                    advance = [3, 1]   # 兩週以上 → 前3天 + 前1天
                elif _days >= 3:
                    advance = [1]      # 3天~兩週 → 前1天先提醒
            except Exception:
                pass
        adv_created = []
        if advance and str(repeat).startswith("once:"):
            try:
                ev = datetime.datetime.strptime(repeat[5:], "%Y-%m-%d").date()
                today = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Taipei")).date()
                wd = "一二三四五六日"[ev.weekday()]
                for nd in (advance if isinstance(advance, list) else [advance]):
                    try:
                        nd = int(nd)
                    except (TypeError, ValueError):
                        continue
                    if nd <= 0:
                        continue
                    adv_date = ev - datetime.timedelta(days=nd)
                    if adv_date < today:
                        continue  # 來不及提早這麼多天就跳過
                    adv_msg = f"提早提醒：{nd} 天後（{ev.month}/{ev.day} 週{wd} {t}）有「{msg}」"
                    rm.add_reminder("07:00", adv_msg,
                                    repeat="once:" + adv_date.strftime("%Y-%m-%d"),
                                    lead_minutes=0, channel=channel)
                    adv_created.append(nd)
            except Exception:
                pass
        # 友善描述：日期(週幾) + 時間
        nice = t
        if str(repeat).startswith("once:"):
            try:
                dd = datetime.datetime.strptime(repeat[5:], "%Y-%m-%d")
                wd = "一二三四五六日"[dd.weekday()]
                nice = "%d/%d（週%s）%s" % (dd.month, dd.day, wd, t)
            except Exception:
                pass
        elif repeat == "daily":
            nice = "每天 " + t
        _notify("reminder", msg[:30])
        adv_txt = ("，並在前 " + "、".join(f"{d}" for d in adv_created) + " 天先提醒你") if adv_created else ""
        # 【記行程順便報連假】事件當天或附近若有連假，自動附上(不用 Owen 另外問、agent 也不用呼叫第二個工具)。
        holiday_txt = ""
        if str(repeat).startswith("once:"):
            try:
                from modules.productivity import tw_holiday as _hol
                _ev = datetime.datetime.strptime(repeat[5:], "%Y-%m-%d").date()
                # 查事件當天到 +4 天(涵蓋多日行程/鄰近連假)
                _end = _ev + datetime.timedelta(days=4)
                _info = _hol.check(f"{_ev.year}-{_ev.month:02d}-{_ev.day:02d}到{_end.year}-{_end.month:02d}-{_end.day:02d}")
                if "有連假" in _info:
                    holiday_txt = "。順帶一提，" + _info.split("👉")[-1].strip()
            except Exception:
                pass
        return JSONResponse({"ok": True, "time": t, "message": msg, "repeat": repeat,
                             "advance_days": adv_created,
                             "nice": f"{nice} 提醒你：{msg}{adv_txt}{holiday_txt}"})
    except Exception as e:
        print(f"⚠️ [reminder] {e}")
        return JSONResponse({"ok": False, "text": "設定提醒時出了點狀況，換個說法再試一次"})


_LAST_EXPENSE: dict = {}  # (amount, note) -> ts，90 秒內同筆視為重複(防確定性攔截與模型雙記)


@app.post("/expense")
async def expense(req: Request):
    """記帳：寫入 config/expenses.json。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    try:
        import math as _math
        _raw = (b or {}).get("amount", 0)
        try:
            amount = float(str(_raw).replace(",", "")) if _raw is not None and not isinstance(_raw, bool) else float("nan")
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "text": "金額我沒看懂，給我數字就好，例如「120」。"})
        # 驗證：拒絕 NaN/inf/負數/不合理金額，避免污染統計
        if not _math.isfinite(amount) or amount <= 0 or amount > 10_000_000:
            return JSONResponse({"ok": False, "text": "金額不合理，再確認一下？"})
        _cat = (b or {}).get("category", "其他")
        category = _t2t(_cat) if isinstance(_cat, str) and _cat.strip() else "其他"
        _note = (b or {}).get("note", "")
        note = _t2t(_note) if isinstance(_note, str) else ""
        # 回溯記帳：使用者說「昨天/前天/X號花了」時帶絕對日期 YYYY-MM-DD,沒帶就記今天。
        _date = str((b or {}).get("date", "")).strip()[:10]
        date = _date if re.match(r"^\d{4}-\d{2}-\d{2}$", _date) else None
        from modules.productivity import expense_tracker as et
        # 去重：90 秒內同金額同品項已記過就跳過(proxy 確定性攔截 + 模型都記時只留一筆)。
        import time as _tm
        _now = _tm.time()
        # 去重分兩種窗，避免誤刪真實帳：
        #  ① 品項有重疊(一方含另一方，兩者都非空)→ 90 秒內視為同一筆(容忍攔截記「奶茶」、模型記「奶茶飲料」)。
        #  ② 任一方品項為空 → 只在 25 秒短窗內視為同一筆(擋「橋接+模型同一輪雙記」；
        #     但「咖啡60」隔一分鐘又報「茶60」這種兩次不同報帳、或空品項的兩筆，不會被誤併吃掉)。
        # 去重 key 要含「花費日期」，否則今天記「咖啡60」後 90 秒內記「昨天咖啡60」會被誤當重複丟掉
        # （回溯記帳資料遺失）。同金額同品項但不同日期＝兩筆真實花費，不能併。
        _eff_date = date or _tm.strftime("%Y-%m-%d")
        for (_pamt, _pnote, _pdate), _pts in list(_LAST_EXPENSE.items()):
            if _pts <= _now - 90:
                del _LAST_EXPENSE[(_pamt, _pnote, _pdate)]
                continue
            if _pamt != amount or _pdate != _eff_date:
                continue
            if _pnote and note and (note in _pnote or _pnote in note):
                return JSONResponse({"ok": True, "text": f"記好了，{category} {amount:g}元", "dup": True})
            if (not note or not _pnote) and _pts > _now - 25:
                return JSONResponse({"ok": True, "text": f"記好了，{category} {amount:g}元", "dup": True})
        _LAST_EXPENSE[(amount, note, _eff_date)] = _now
        et.add_expense(amount, category, note, date=date)
        # 記完馬上算本月預算狀況，主動提醒（夢幻：花太多會被唸）。
        # overview() 內含抓即時股價的阻塞 IO（最長~12s）→ 丟執行緒，不凍住整個 8809 事件迴圈。
        text = f"記好了，{category} {amount:g}元"

        def _budget_note():
            try:
                from modules.finance import wealth
                ov = wealth.overview()
                spendable, mv, rem = ov["spendable"], ov["month_var"], ov["remaining"]
                if spendable > 0:
                    if rem < 0:
                        return f"。不過本期（{ov['cycle_label']}）生活花費已 {mv} 元、超出可花的 {spendable} 元囉，要省一點"
                    elif mv > spendable * 0.8:
                        return f"。提醒你本期已花 {mv}，快到可花上限 {spendable} 了"
                    return f"。本期還可以花 {rem} 元（已先幫你存起 {ov['auto_saved']}）"
            except Exception:
                pass
            return ""
        import asyncio as _aio
        try:
            text += await _aio.get_event_loop().run_in_executor(None, _budget_note)
        except Exception:
            pass
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8811/api/event",
                data=json.dumps({"type": "finance", "data": f"記帳 {category} {amount:g}"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=3)
        except Exception:
            pass
        return JSONResponse({"ok": True, "amount": amount, "category": category, "text": text})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})



def _reminders_aggregate(rs):
    """預先算好『數量/過期數/下一個倒數』，讓 AI 直接唸，不用自己數提醒、比時鐘（那會錯）。"""
    now = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Taipei"))
    items, overdue, soonest, soonest_dt = [], 0, None, None
    for r in (rs or []):
        rep, t = str(r.get("repeat") or ""), str(r.get("time") or "")
        due = None
        try:
            hh, mm = (int(x) for x in t.split(":")[:2])
            if rep.startswith("once:"):
                y, mo, d = (int(x) for x in rep.split(":", 1)[1].split("-"))
                due = now.replace(year=y, month=mo, day=d, hour=hh, minute=mm,
                                  second=0, microsecond=0)
            else:  # daily / 其他週期 → 取今天或明天的這個時間
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due < now:
                    due = due + datetime.timedelta(days=1)
        except Exception:
            due = None
        is_over = bool(rep.startswith("once:") and due and due < now and not r.get("last_fired"))
        overdue += 1 if is_over else 0
        mins = int((due - now).total_seconds() // 60) if due else None
        items.append({"time": t, "message": r.get("message"), "repeat": rep,
                      "minutes_from_now": mins, "overdue": is_over})
        if due and due >= now and (soonest_dt is None or due < soonest_dt):
            soonest_dt = due
            soonest = {"message": r.get("message"), "minutes_from_now": mins, "time": t}
    return {"count": len(rs or []), "count_overdue": overdue, "next": soonest, "items": items}


# ===== 待播語音佇列：讓 StackChan 到點主動開口 =====
# 機器人走 xiaozhi WebSocket、不在 stackchan-mcp gateway 上，所以提醒 daemon 沒法直接叫它講話。
# 改用佇列：daemon 到點把文字 POST 進來 → 已連線的 xiaozhi connection 每隔幾秒來撈 → 用 TTS 讓裝置開口。
_pending_voice = []
_pending_voice_lock = __import__("threading").Lock()  # 防兩個並發 GET 撈到同一批→重播同一句


@app.post("/push_voice")
async def push_voice_ep(req: Request):
    """提醒/通知到點時把要講的話丟進佇列。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    text = (b or {}).get("text", "")
    if text and str(text).strip():
        import time as _t
        with _pending_voice_lock:
            _pending_voice.append({"text": str(text).strip(), "ts": _t.time()})
            del _pending_voice[:-10]   # 防爆：最多留 10 則
        return JSONResponse({"ok": True, "queued": len(_pending_voice)})
    return JSONResponse({"ok": False, "error": "no text"})


@app.get("/pending_voice")
def pending_voice_ep():
    """xiaozhi 裝置端來撈待播語音（撈完即清空，避免重播）。原子交換：並發撈也不會拿到同一批。"""
    global _pending_voice
    with _pending_voice_lock:
        items, _pending_voice = _pending_voice, []
    return JSONResponse({"ok": True, "items": items})


@app.get("/reminders")
def list_reminders_ep():
    try:
        from modules.productivity import reminder_manager as rm
        rs = rm.list_reminders()
        items = [f"{r['time']} {r['message']}" for r in rs] if rs else []
        agg = _reminders_aggregate(rs)
        return JSONResponse({"ok": True, "text": "；".join(items) or "目前沒有提醒",
                             "count": agg["count"], "count_overdue": agg["count_overdue"],
                             "next": agg["next"], "reminders": agg["items"]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/expenses_summary")
def expenses_summary_ep(date: str = ""):
    """記帳摘要：給語音/Telegram 用。可指定 date=YYYY-MM-DD 查某一天(沒給=今天)，
    列逐項明細 + (僅今天)本期分類，讓 Jarvis 依使用者問的問題真正回答。"""
    try:
        from modules.finance import wealth
        x = wealth.load_expenses()
        today = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
        target = (date or "").strip()[:10] or today   # 指定日期,沒給就今天
        is_today = (target == today)
        _when = f"今天（{target}）" if is_today else target   # 今天附日期,指定日就只顯示一次

        def _amt(e):
            try:
                return float(e.get("amount") or 0)
            except (TypeError, ValueError):
                return 0.0
        # 指定日逐項（排除「之前花費」這種補登調整，那不是當天真的消費）
        td_items = [e for e in x if str(e.get("date", "")).startswith(target)
                    and e.get("category") != "之前花費"]
        td = sum(_amt(e) for e in td_items)
        parts = []
        if td_items:
            detail = "、".join(f"{e.get('category', '其他')} {int(_amt(e))}元"
                              + (f"（{e.get('note')}）" if e.get('note') else "")
                              for e in td_items)
            parts.append(f"{_when}花了 {int(td)} 元，共 {len(td_items)} 筆：{detail}")
        else:
            parts.append(f"{_when}沒有記到任何花費")
        # 本期分類彙總（僅今天查詢時附上；查歷史某天就不混入本期統計）
        if is_today:
            try:
                from modules.finance import expense_insights as ei
                cb = ei.category_breakdown()
                if cb:
                    top = "、".join(f"{k} {int(v)}元" for k, v in
                                   sorted(cb.items(), key=lambda kv: -kv[1])[:6])
                    parts.append(f"本期各分類：{top}")
            except Exception:
                pass
        return JSONResponse({"ok": True, "text": "。".join(parts) + "。",
                             "today_total": int(td), "today_items": td_items})
    except Exception as e:
        print(f"[expenses_summary] {e}")
        return JSONResponse({"ok": False, "text": "查花費明細時出了點問題，等等再問我一次"})


# ========== 財富管理（理財）==========
# GET  /finance            → 完整總覽（收入/固定/生活開銷/預算/投資即時市值報酬）
# PUT  /finance/income     → 整批覆寫每月收入清單
# PUT  /finance/fixed      → 整批覆寫固定開銷清單
# PUT  /finance/holdings   → 整批覆寫持股清單
# PUT  /finance/expenses   → 整批覆寫生活開銷（可刪掉亂寫的）
# POST /finance/budget     → 設定每月存錢目標 / 花費上限
@app.get("/finance")
def finance_overview_ep():
    try:
        from modules.finance import wealth
        return JSONResponse({"ok": True, "data": wealth.overview()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/finance/history")
def finance_history_ep():
    """淨資產歷史快照（給 dashboard 畫趨勢曲線）。"""
    try:
        from modules.finance import wealth
        return JSONResponse({"ok": True, "history": wealth.load_history()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "history": []})


_INV_KW = ["投資", "股", "持股", "報酬", "賺", "虧", "漲", "跌", "淨資產", "身價", "市值",
           "etf", "基金", "fire", "財務自由", "財富自由", "配置", "加碼", "減碼",
           "台積電", "0050", "費半", "正2", "美股", "台股", "報酬率",
           "目標", "達標", "400萬", "身家", "淨值", "存到", "缺口"]


def _needs_portfolio(text):
    """問題是否牽涉投資/淨資產 → 需要抓即時股價。純花費/收入問題就不必（省下抓股價的時間）。"""
    t = (text or "").lower()
    return any(k.lower() in t for k in _INV_KW)


def _finance_guest_view(d, pf):
    """【訪客版】只給報酬率%與公開資訊，絕不洩漏 Owen 的任何金額（資產/薪水/餘額/淨值）。
    這是隱私鎖：訪客就算問「Owen 資產多少」也只拿得到 %，拿不到任何 $ 數字。"""
    parts = ["Owen 的確切金額（資產、薪水、餘額這些）是他的私事，我不能透露。"]
    if pf.get("items"):
        closed = pf.get("market_open") is False
        line = f"不過可以跟你說：他的投資總報酬率是 {pf.get('total_retpct')}%"
        line += "，今天休市" if closed else ""
        parts.append(line)
        bm = pf.get("by_market", {})
        for code in ("TW", "US"):
            g = bm.get(code)
            if g:
                parts.append(f"{g.get('label')}報酬率 {g.get('retpct')}%（{g.get('count')}檔）")
        tops = "；".join(f"{it.get('name') or it.get('symbol')} {it['retpct']}%" for it in pf["items"][:8])
        if tops:
            parts.append("各檔報酬率：" + tops)
        if not closed:
            td = "；".join(
                f"{it.get('name') or it.get('symbol')}今日{'+' if it.get('todaypct',0)>=0 else ''}{it.get('todaypct',0)}%"
                for it in pf["items"][:8] if it.get("todaypct") is not None)
            if td:
                parts.append("各檔今日：" + td)
    else:
        parts.append("他最近的投資我沒有可公開的資訊。")
    return "。".join(parts)


_FIN_JSON = os.path.expanduser("~/Hermes_Brain/config/finance.json")


def _load_goal() -> dict:
    """讀可調整的資產目標(存在 finance.json 的 goal 欄位)。預設 400 萬 / 30 歲。"""
    try:
        g = (json.load(open(_FIN_JSON)) or {}).get("goal") or {}
    except Exception:
        g = {}
    return {"target": float(g.get("target", 4000000)), "target_age": int(g.get("target_age", 30))}


@app.get("/networth/trend")
def networth_trend_ep():
    """資產每日/每週/每月變動 + 400 萬進度。給語音「我資產這週/這個月變多少、離目標多遠」用。"""
    try:
        # 【隱私鎖】finance_summary 有訪客遮罩但這個兄弟端點沒有(審計抓到的洞)：
        # 訪客講話時大腦若呼叫到這裡，會把 Owen 的真實金額全唸出去。非主人→只給%不給$。
        try:
            from modules.people import people_memory as _pm
            if not _pm.is_owner():
                return JSONResponse({"ok": True, "guest": True,
                                     "text": "這是 Owen 的私人財務資料，金額我不能透露喔。"})
        except Exception:
            pass
        from modules.finance import wealth
        d = wealth.overview(with_portfolio=True)
        nw = float(d.get("net_worth") or 0)
        t = wealth.networth_trend(current_nw=nw)
        if not t:
            return JSONResponse({"ok": True, "text": "還沒有足夠的歷史資料可以看趨勢，多記幾天就有了。"})
        g = _load_goal()
        gap = g["target"] - nw

        def _seg(label, dd):
            if not dd:
                return None
            sign = "+" if dd["change"] >= 0 else ""
            verb = "增加" if dd["change"] >= 0 else "減少"
            return f"{label}{verb}{abs(dd['change'])}元（{sign}{dd['pct']}%，從{dd['from_date']}的{dd['from']}元起算）"
        segs = [s for s in [_seg("每日：昨天到今天", t.get("daily")),
                            _seg("每週：", t.get("weekly")),
                            _seg("每月：", t.get("monthly"))] if s]
        txt = (f"你目前淨資產{int(nw)}元（已追蹤{t['days_tracked']}天）。"
               + "；".join(segs) + "。"
               + f"距離{int(g['target'])}元的目標還差{int(gap)}元。")
        return JSONResponse({"ok": True, "text": txt, "trend": t, "goal": g})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/goal")
def goal_get_ep():
    g = _load_goal()
    return JSONResponse({"ok": True, "target": g["target"], "target_age": g["target_age"],
                         "text": f"你目前設定的目標是{int(g['target'])}元、{g['target_age']}歲達標。"})


@app.post("/goal/set")
async def goal_set_ep(req: Request):
    """語音「把目標改成500萬 / 改成32歲達標」用。可只改其中一項。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    try:
        data = json.load(open(_FIN_JSON)) if os.path.exists(_FIN_JSON) else {}
        g = data.get("goal") or {}
        if b.get("target") is not None:
            _t = float(b["target"])
            if not (10000 <= _t <= 1_000_000_000):   # 目標金額要合理(1萬~10億),擋 0/負數/亂數
                return JSONResponse({"ok": False, "text": "目標金額怪怪的，給我一個合理的數字，例如「500萬」。"})
            g["target"] = _t
        if b.get("target_age") is not None:
            _a = int(b["target_age"])
            if not (18 <= _a <= 100):                 # 達標年齡要合理
                return JSONResponse({"ok": False, "text": "達標年齡怪怪的，18 到 100 歲之間比較合理。"})
            g["target_age"] = _a
        data["goal"] = g
        with open(_FIN_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        gg = _load_goal()
        return JSONResponse({"ok": True, "target": gg["target"], "target_age": gg["target_age"],
                             "text": f"好，目標改成{int(gg['target'])}元、{gg['target_age']}歲達標了。"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


_PENDING_EMAIL = os.path.expanduser("~/.hermes/pending_emails.json")
_EMAIL_LOG = os.path.expanduser("~/Hermes_Brain/memory/logs/email_sent.log")
_HIMALAYA = "/opt/homebrew/bin/himalaya"
_MY_EMAIL = "you@example.com"


def _load_pending_emails() -> list:
    try:
        return json.load(open(_PENDING_EMAIL))
    except Exception:
        return []


def _save_pending_emails(lst: list) -> None:
    try:
        with open(_PENDING_EMAIL, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@app.post("/email/draft")
async def email_draft_ep(req: Request):
    """只擬草稿、【絕不寄出】。存進待寄匣，等 Owen 在儀表板按寄出、或明確說「寄出」才寄。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    to = str((b or {}).get("to", "")).strip()
    subject = str((b or {}).get("subject", "")).strip() or "(無主旨)"
    body = str((b or {}).get("body", "")).strip()
    if not to or not body:
        return JSONResponse({"ok": False, "error": "缺收件人或內容"})
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):   # 擋掉不是 email 格式的收件人(寄出前先擋)
        return JSONResponse({"ok": False, "error": f"「{to}」不是有效的 email 格式，給我完整信箱才擬。"})
    import time as _t
    did = str(int(_t.time() * 1000))[-9:]
    draft = {"id": did, "to": to, "subject": subject, "body": body, "ts": _t.time()}
    lst = _load_pending_emails()
    lst.append(draft)
    _save_pending_emails(lst)
    _notify("email_draft", f"給 {to}：{subject}")
    return JSONResponse({"ok": True, "id": did, "draft": draft,
                         "text": f"我把回信擬好放進待寄匣了（給 {to}，主旨「{subject}」）。內容是：{body}。你要寄的話到儀表板按「寄出」、或直接說「寄出」，我確認才會寄。"})


@app.get("/email/pending")
def email_pending_ep():
    """待寄匣清單（給儀表板顯示確認卡片）。"""
    return JSONResponse({"ok": True, "pending": _load_pending_emails()})


def _himalaya_send(draft: dict):
    msg = (f"From: Owen Chen (Your Name) <{_MY_EMAIL}>\nTo: {draft['to']}\n"
           f"Subject: {draft['subject']}\n\n{draft['body']}\n")
    try:
        import subprocess as _sp
        r = _sp.run([_HIMALAYA, "message", "send", "-a", "gmail"],
                    input=msg, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            try:
                import time as _t
                with open(_EMAIL_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} → {draft['to']} | {draft['subject']}\n")
            except Exception:
                pass
            return True, ""
        return False, (r.stderr or r.stdout or "himalaya 寄信失敗")[:200]
    except Exception as e:
        return False, str(e)


@app.post("/email/send")
async def email_send_ep(req: Request):
    """真的寄出一封待寄的信（by id，沒給 id 就寄最新那封）。
    【這是 Owen 確認後才會被呼叫】：儀表板的寄出鈕 = 親手點；語音「寄出」= 明確指示。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    did = str((b or {}).get("id", "")).strip()
    lst = _load_pending_emails()
    draft = next((d for d in lst if str(d.get("id")) == did), None) or (lst[-1] if lst else None)
    if not draft:
        return JSONResponse({"ok": False, "error": "待寄匣是空的，沒有信可以寄"})
    import asyncio as _aio
    ok, err = await _aio.get_event_loop().run_in_executor(None, _himalaya_send, draft)
    if ok:
        _save_pending_emails([d for d in _load_pending_emails() if str(d.get("id")) != str(draft["id"])])
        _notify("email_sent", f"已寄給 {draft['to']}")
        return JSONResponse({"ok": True, "text": f"寄出了給 {draft['to']} 的信（主旨「{draft['subject']}」）。"})
    return JSONResponse({"ok": False, "error": f"寄信失敗：{err}"})


@app.post("/email/update")
async def email_update_ep(req: Request):
    """就地更新一封待寄草稿的收件人/主旨/內容（by id）。給儀表板卡片內直接編輯用，仍然不寄。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    did = str((b or {}).get("id", "")).strip()
    lst = _load_pending_emails()
    hit = False
    for d in lst:
        if str(d.get("id")) == did:
            if b.get("to") is not None:
                d["to"] = str(b["to"]).strip()
            if b.get("subject") is not None:
                d["subject"] = str(b["subject"]).strip() or "(無主旨)"
            if b.get("body") is not None:
                d["body"] = str(b["body"]).strip()
            hit = True
            break
    if not hit:
        return JSONResponse({"ok": False, "error": "找不到那封草稿"})
    _save_pending_emails(lst)
    return JSONResponse({"ok": True, "text": "已儲存修改"})


@app.post("/email/cancel")
async def email_cancel_ep(req: Request):
    """丟掉一封待寄的草稿（不寄）。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    did = str((b or {}).get("id", "")).strip()
    lst = _load_pending_emails()
    lst2 = [d for d in lst if str(d.get("id")) != did] if did else lst[:-1]
    _save_pending_emails(lst2)
    return JSONResponse({"ok": True, "text": "好，那封草稿丟掉了、不寄。"})


# 語意正規化用的標準問法清單（_focused_finance_answer 的各分支對得上的說法）
_FINANCE_CANON = [
    "今天花多少", "這週花多少", "這個月花多少", "這個月最大一筆花費", "餐飲花多少",
    "最近一筆花費", "平均一天花多少", "每天還能花多少", "離目標還有多少",
    "現在淨資產多少", "台積電賺多少", "最賺的股票", "最虧的股票", "持股幾檔",
    "股票總市值", "今天投資賺多少", "報酬率多少", "美股賺多少", "台股表現怎樣",
    "30歲存到400萬每月要存多少", "什麼時候發薪", "月收入多少", "固定開銷有哪些",
]


def _semantic_finance_q(q: str):
    """【語意層·不靠關鍵字】用 LLM 把使用者【任何講法】改寫成上面標準問法中語意最接近的一句，
    保留股票名/類別/金額/年齡等實體、中文數字轉阿拉伯數字（例：八百萬→800萬）。
    這樣「我美金部位如何」「出去吃飯花多少」「八百萬」都能對到確定性分支，不再只認固定關鍵字。
    只在關鍵字沒命中時才呼叫（不拖慢已命中的），對不上就回原句。"""
    try:
        canon = "、".join(_FINANCE_CANON)
        prompt = (
            "使用者問了一個關於他個人財務的問題。請把它改寫成下列【標準問法】裡語意最接近的『一句』，"
            "並保留原問句裡的實體（股票名稱、消費類別、金額、年齡），金額和數字一律用阿拉伯數字"
            "（例：八百萬→800萬、五十→50）。\n"
            f"標準問法：{canon}\n"
            "規則：只回改寫後的那一句，不要解釋、不要引號。若問句其實不屬於上面任何一類（不是在問這些），"
            "就原封不動回傳原句。\n\n"
            f"使用者問句：{q}"
        )
        body = {"model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "temperature": 0.0}
        req = urllib.request.Request(
            "http://127.0.0.1:8808/v1beta/openai/chat/completions",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=12))
        out = (r["choices"][0]["message"]["content"] or "").strip().strip('「」"\'').splitlines()[0]
        return out[:60] if out else q
    except Exception:
        return q


_ZH_DIGIT = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_ZH_UNIT = {"十": 10, "百": 100, "千": 1000}


def _zh_to_int(s: str) -> int:
    """中文數字轉整數(支援到億)：八百萬→8000000、一千萬→10000000、兩百萬→2000000。"""
    total = section = num = 0
    for ch in s:
        if ch in _ZH_DIGIT:
            num = _ZH_DIGIT[ch]
        elif ch in _ZH_UNIT:
            section += (num or 1) * _ZH_UNIT[ch]
            num = 0
        elif ch in ("萬", "万"):
            total += (section + num) * 10000
            section = num = 0
        elif ch == "億":
            total += (section + num) * 100000000
            section = num = 0
    return total + section + num


def _zh_amount(q: str) -> int:
    """從問句抽出金額(元)，同時吃阿拉伯數字跟中文數字：800萬/八百萬/8000000元 都可以。抽不到回 0。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*萬", q)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"(\d{5,})\s*元?", q)
    if m:
        return int(m.group(1))
    m = re.search(r"[零一二兩三四五六七八九十百千]+\s*[萬万億]", q)
    if m:
        return _zh_to_int(m.group(0))
    return 0


def _focused_finance_answer(q: str, d: dict, pf: dict):
    """Owen 高頻的錢的問題 → 程式算好、回一句精準答案(模型只照唸)。認不出來回 None(走完整摘要)。
    有界:只涵蓋每天真的會問的那幾種,不做無限枚舉。"""
    if not q:
        return None
    from modules.finance import wealth as _w
    nw = int(d.get("net_worth") or 0)
    # 【robust·錢不能錯】只要問到「資產/淨值/身家/目標/賺」，淨資產一定要含投資部位。
    # 若上游為了省速度沒抓股價(net_worth 會變成只剩現金甚至 0)→ 這裡強制重算一次，
    # 不再靠「有沒有猜中關鍵字」(那就是反覆算成 0 的根因)。
    if any(k in q for k in ("資產", "淨值", "身家", "目標", "達標", "存到", "缺口", "賺", "投資", "報酬",
                            "多存", "再存", "想存", "存錢", "存更多", "大盤", "股票", "持股", "市值")):
        if not (pf and pf.get("items")) or nw <= 0:
            d = _w.overview(with_portfolio=True)
            pf = d.get("portfolio", {}) or {}
            nw = int(d.get("net_worth") or 0)

    # 1) 今天花多少
    if ("今天" in q or "今日" in q) and any(k in q for k in ("花", "消費", "花費", "用了", "用掉")) and "每天" not in q and "每日" not in q:
        _td = _w._today() if hasattr(_w, "_today") else ""
        items = [e for e in d.get("expenses", [])
                 if str(e.get("date", "")).startswith(str(_td)) and e.get("category") != "之前花費"]
        if not items:
            return f"你今天（{_td}）目前還沒有記到任何花費。"
        tot = int(sum(float(e.get("amount") or 0) for e in items))
        detail = "、".join(f"{e.get('category', '其他')} {int(float(e.get('amount') or 0))}元"
                           + (f"（{e.get('note')}）" if e.get('note') else "") for e in items)
        return f"你今天花了 {tot} 元，共 {len(items)} 筆：{detail}。（照唸這些數字，別另外抓整數。）"

    import datetime as _dtf
    _mon_items = [e for e in d.get("expenses", [])
                  if e.get("category") != "之前花費"
                  and str(e.get("date", "")).startswith(_dtf.date.today().strftime("%Y-%m"))]

    # 1h) 昨天花多少（功能16）
    if any(k in q for k in ("昨天", "昨日")) and any(k in q for k in ("花", "消費", "花費", "用了", "買")):
        _yd = (_dtf.date.today() - _dtf.timedelta(days=1)).isoformat()
        items = [e for e in d.get("expenses", [])
                 if str(e.get("date", ""))[:10] == _yd and e.get("category") != "之前花費"]
        if not items:
            return f"你昨天（{_yd[5:]}）沒有記到任何花費。"
        tot = int(sum(float(e.get("amount") or 0) for e in items))
        detail = "、".join(f"{e.get('category', '其他')} {int(float(e.get('amount') or 0))}元"
                           + (f"（{e.get('note')}）" if e.get('note') else "") for e in items)
        return f"你昨天花了 {tot} 元，共 {len(items)} 筆：{detail}。（照唸這些數字。）"

    # 1i) 美金匯率（功能17）— 用投資模組的即時匯率，不讓模型憑記憶報舊價
    if any(k in q for k in ("匯率", "美金多少", "美元多少", "美金現在", "換美金", "美元兌")):
        _rate = (pf or {}).get("usd_twd")
        if not _rate:
            from modules.finance import wealth as _w2
            try:
                _rate = _w2.portfolio().get("usd_twd")
            except Exception:
                _rate = None
        if _rate:
            return f"現在美金兌台幣匯率約 {_rate}。（照唸這個數字。）"

    # 1j) 本期結餘/剩多少（功能18）
    if any(k in q for k in ("結餘", "剩多少", "還剩多少錢", "剩下多少", "餘額")) and "每天" not in q:
        _rem = d.get("remaining")
        if _rem is not None:
            return (f"你本期（{d.get('cycle_label', '')}）還剩 {int(_rem)} 元可用，距發薪還有 {d.get('days_left', '?')} 天。"
                    f"（照唸這些數字。）")

    # 1d) 最大/最貴一筆花費（本月）
    if any(k in q for k in ("最大", "最貴", "花最多", "最大筆", "最大一筆")) and any(k in q for k in ("花", "消費", "花費", "一筆", "支出", "買")):
        if not _mon_items:
            return "你這個月還沒有記到花費。"
        big = max(_mon_items, key=lambda e: float(e.get("amount") or 0))
        return (f"你這個月最大一筆是 {int(float(big.get('amount') or 0))} 元"
                f"（{big.get('category', '其他')}{('・' + str(big.get('note'))) if big.get('note') else ''}，"
                f"{str(big.get('date', ''))[:10]}）。（照唸這些數字。）")

    # 1e) 某類別本月花多少（餐飲/交通/娛樂...）
    _hit_cat = next((c for c in ("餐飲", "交通", "娛樂", "購物", "醫療", "生活") if c in q), None)
    if _hit_cat and any(k in q for k in ("花", "多少", "花費", "消費", "用")):
        items = [e for e in _mon_items if str(e.get("category", "")) == _hit_cat]
        tot = int(sum(float(e.get("amount") or 0) for e in items))
        return f"你這個月「{_hit_cat}」類花了 {tot} 元，共 {len(items)} 筆。（照唸這個數字。）"

    # 1b) 這週花多少
    if any(k in q for k in ("這週", "本週", "這周", "本周", "這禮拜", "這星期")) and any(k in q for k in ("花", "消費", "花費", "用了", "用掉", "多少")) and "平均" not in q:
        _t = _dtf.date.today()
        _mon = _t - _dtf.timedelta(days=_t.weekday())
        items = [e for e in d.get("expenses", [])
                 if e.get("category") != "之前花費" and str(e.get("date", ""))[:10] >= _mon.isoformat()]
        tot = int(sum(float(e.get("amount") or 0) for e in items))
        return f"你這週（{_mon.month}/{_mon.day} 起）到現在花了 {tot} 元，共 {len(items)} 筆。（照唸這個數字。）"

    # 1c) 這個月花多少（日曆月）
    if any(k in q for k in ("這個月", "本月", "這月", "當月")) and any(k in q for k in ("花", "消費", "花費", "用了", "用掉", "多少")) and "每" not in q and "平均" not in q:
        tot = int(sum(float(e.get("amount") or 0) for e in _mon_items))
        return f"你這個月（{_dtf.date.today().month}月）到現在花了 {tot} 元，共 {len(_mon_items)} 筆。（照唸這個數字。）"

    # 1f) 最近一筆花費（功能10）
    if any(k in q for k in ("最近", "上一筆", "最後一筆", "剛剛花", "最新一筆", "上次花")) and any(k in q for k in ("花", "消費", "買", "支出", "記")):
        allx = [e for e in d.get("expenses", []) if e.get("category") != "之前花費" and e.get("date")]
        if not allx:
            return "你最近沒有記到花費。"
        last = sorted(allx, key=lambda e: str(e.get("date", "")))[-1]
        return (f"你最近一筆是 {int(float(last.get('amount') or 0))} 元"
                f"（{last.get('category', '其他')}{('・' + str(last.get('note'))) if last.get('note') else ''}，"
                f"{str(last.get('date', ''))[:10]}）。（照唸這些數字。）")

    # 1g) 平均一天花多少（本月，功能11）
    if any(k in q for k in ("平均", "均消", "每天平均")) and any(k in q for k in ("花", "消費", "支出")):
        tot = int(sum(float(e.get("amount") or 0) for e in _mon_items))
        _day = _dtf.date.today().day
        avg = int(tot / _day) if _day else 0
        return f"你這個月平均一天花約 {avg} 元（{_dtf.date.today().month}月到今天共 {tot} 元、{_day} 天）。（照唸這些數字。）"

    # 2) 每天還能花多少
    if any(k in q for k in ("每天", "每日", "一天")) and any(k in q for k in ("花", "用", "可以", "還能", "額度", "預算")):
        if d.get("daily_allowance") is not None and d.get("days_left"):
            _rem = int(d.get("remaining", 0))
            if _rem < 0:
                return (f"你本期【已經超支 {abs(_rem)} 元】了，距發薪還有 {d['days_left']} 天，所以嚴格說每天不但沒得花、還得少花。"
                        f"（照唸：本期超支 {abs(_rem)} 元，別講成還能花多少。）")
            return (f"你本期【每天還能花約 {int(d['daily_allowance'])} 元】。"
                    f"（本期還剩 {_rem} 元、距發薪還有 {d['days_left']} 天。照唸這個數字，別另外抓整數。）")

    # 16) 發薪日
    if any(k in q for k in ("發薪", "幾號發", "什麼時候發", "領薪", "薪水什麼時候", "何時發薪")):
        return f"你發薪日是每月 {d.get('payday', 15)} 號，距下次發薪還有 {d.get('days_left', '?')} 天。（照唸這些數字。）"

    # 17) 月收入
    if any(k in q for k in ("月收入", "薪水多少", "收入多少", "月薪", "一個月賺多少錢", "每月收入")):
        return f"你的月收入是 {int(d.get('income') or 0)} 元。（照唸這個數字。）"

    # 18) 固定開銷有哪些
    if any(k in q for k in ("固定開銷", "固定支出", "每月固定", "固定花費", "固定要花")):
        _fl = d.get("fixed_list") or []
        if not _fl:
            return f"你每月固定開銷總共 {int(d.get('fixed') or 0)} 元。（照唸這個數字。）"
        _items = "、".join(f"{x.get('name', '')} {int(float(x.get('amount') or 0))}元" for x in _fl)
        return f"你每月固定開銷共 {int(d.get('fixed') or 0)} 元：{_items}。（照唸這些數字。）"

    # 3) 離目標還多遠 / 還差多少
    if any(k in q for k in ("離", "還差", "距離", "多遠", "幾歲")) and any(k in q for k in ("目標", "萬", "達標", "存到")):
        g = _load_goal()
        gap = int(g["target"]) - nw
        return (f"你目前淨資產 {nw} 元，離 {int(g['target'])} 元的目標還差 {gap} 元（目標 {g['target_age']} 歲達標）。"
                f"（要試算幾歲能達標請用 project_wealth_goal 工具。照唸這些數字，目標就是 {int(g['target'])} 元別講錯。）")

    # 3b) 存錢意圖(「還想再多存」「想多存點」，沒指定每天/金額)→ 回鎖死的離目標句。
    #     不讓 flash-lite 自由組句——它會把 400萬 目標瞎編成 600萬(已犯過)。含「每天」的走上面 #2。
    if any(k in q for k in ("多存", "再存", "想存", "存更多", "多存點", "存錢")) \
            and not any(k in q for k in ("每天", "每日", "一天")):
        g = _load_goal()
        gap = int(g["target"]) - nw
        return (f"你目前淨資產 {nw} 元，離 {int(g['target'])} 元的目標還差 {gap} 元（目標 {g['target_age']} 歲達標）。"
                f"想多存的話我可以幫你盤點每月餘裕。（照唸這些數字，目標就是 {int(g['target'])} 元、絕對別講成別的數字。）")

    # 4) 淨資產/身家多少
    if any(k in q for k in ("淨資產", "身家", "淨值", "資產多少", "資產有多少", "總資產")) and "投資" not in q and "賺" not in q:
        return f"你目前淨資產 {nw} 元。（照唸這個數字。）"

    # 5a) 問【特定一檔】股票賺多少/漲多少 → 回那一檔，不要回全部總報酬(問台積電卻回總額=答錯)
    if pf.get("items") and any(k in q for k in ("賺", "獲利", "報酬", "漲", "跌", "賠", "虧", "多少", "怎樣", "如何")):
        for _it in pf["items"]:
            _nm = str(_it.get("name") or "")
            _sym = str(_it.get("symbol") or "")
            if (len(_nm) >= 2 and _nm in q) or (len(_sym) >= 3 and _sym in q):
                _r = int(_it.get("ret") or 0)
                _tdy = int(_it.get("today") or 0)
                return (f"{_nm}（{_sym}）你目前{'賺' if _r >= 0 else '虧'}了 {abs(_r)} 元"
                        f"（報酬率 {_it.get('retpct')}%、市值 {int(_it.get('value') or 0)} 元，"
                        f"今天{'漲' if _tdy >= 0 else '跌'} {abs(_tdy)} 元）。（照唸這些數字。）")

    # 5h) 今天贏大盤了嗎（玩槓桿最該問的問題:超額報酬）— 拿 0050/SPY 當台美股基準即時比
    if pf.get("items") and any(k in q for k in ("贏大盤", "輸大盤", "跟大盤", "比大盤", "大盤比", "贏過大盤", "有沒有贏")):
        try:
            _my = float(pf.get("total_todaypct") or 0)
            _bm = []
            for _sym, _lbl in (("0050", "台股大盤(0050)"), ("SPY", "美股大盤(SPY)")):
                _q = _w._quote(_sym)
                _p, _pv = _q.get("price"), _q.get("prev")
                if _p and _pv:
                    _bm.append((_lbl, round((_p - _pv) / _pv * 100, 2)))
            if _bm:
                _cmp = "、".join(f"{l} {p:+}%" for l, p in _bm)
                _beat = all(_my > p for _, p in _bm)
                _lose = all(_my < p for _, p in _bm)
                _verdict = "全面贏過大盤" if _beat else ("輸給大盤" if _lose else "跟大盤互有勝負")
                return (f"你的組合今天 {_my:+}%,{_cmp} → 今天{_verdict}。（照唸這些數字。）")
        except Exception:
            pass

    # 5b) 最賺/最虧的股票（要在 #5 總報酬之前，否則「最賺」的「賺」會被搶走）
    if pf.get("items") and any(k in q for k in ("最賺", "最會賺", "賺最多", "最虧", "虧最多", "賠最多",
                                                "表現最好", "表現最差", "最爛", "哪支最", "哪檔最")):
        _worst = any(k in q for k in ("最虧", "虧最多", "賠最多", "表現最差", "最爛"))
        _pick = (min if _worst else max)(pf["items"], key=lambda it: float(it.get("ret") or 0))
        _r = int(_pick.get("ret") or 0)
        return (f"你{'最虧' if _worst else '最賺'}的是 {_pick.get('name')}（{_pick.get('symbol')}），"
                f"{'賺' if _r >= 0 else '虧'} {abs(_r)} 元（報酬率 {_pick.get('retpct')}%）。（照唸這些數字。）")

    # 5c) 持股清單/幾檔（功能12）
    if pf.get("items") and any(k in q for k in ("幾檔", "幾支", "幾隻", "哪些股", "持股有", "有哪些股", "買了哪些", "持有幾")):
        _names = "、".join(f"{it.get('name')}({it.get('symbol')})" for it in pf["items"])
        return f"你目前持有 {len(pf['items'])} 檔：{_names}。（照唸。）"

    # 5d) 投資組合總市值/現值（功能13）
    if pf.get("items") and any(k in q for k in ("總市值", "現值", "值多少", "股票值", "投資值", "部位多大", "市值多少", "股票總共值")):
        return f"你投資組合目前總市值 {int(pf.get('total_value') or 0)} 元（{len(pf['items'])} 檔）。（照唸這個數字。）"

    # 5e) 今天投資賺賠多少（功能14）
    if pf.get("items") and any(k in q for k in ("今天", "今日")) and any(k in q for k in ("賺", "賠", "漲", "跌", "股票", "投資", "部位")):
        _tt = int(pf.get("total_today") or 0)
        return f"你今天投資{'賺' if _tt >= 0 else '賠'}了 {abs(_tt)} 元（{pf.get('total_todaypct')}%）。（照唸這些數字。）"

    # 5f) 總報酬率多少（功能15a）
    if pf.get("items") and any(k in q for k in ("報酬率", "賺了幾", "賺幾趴", "獲利率", "報酬幾", "賺幾%")):
        _r = int(pf.get("total_ret") or 0)
        return f"你投資總報酬率 {pf.get('total_retpct')}%（總共{'賺' if _r >= 0 else '虧'} {abs(_r)} 元）。（照唸這些數字。）"

    # 5g) 美股/台股 各別市值或報酬（功能15b）
    _mk = "US" if any(k in q for k in ("美股", "美國股")) else ("TW" if any(k in q for k in ("台股", "臺股", "台灣股")) else None)
    if pf.get("items") and _mk and any(k in q for k in ("賺", "多少", "值", "市值", "佔", "報酬", "怎樣")):
        _bm = (pf.get("by_market") or {}).get(_mk, {})
        _lbl = "美股" if _mk == "US" else "台股"
        _br = int(_bm.get("ret") or 0)
        return (f"你的{_lbl}：市值約 {int(_bm.get('value') or 0)} 元、"
                f"總報酬{'賺' if _br >= 0 else '虧'} {abs(_br)} 元（{_bm.get('retpct')}%）、"
                f"今天{int(_bm.get('today') or 0)} 元。（照唸這些數字。）")

    # 5) 投資賺多少（總報酬；不含「今天/每天/某支股票」，那些走完整摘要有分市場）
    if (pf.get("items") and any(k in q for k in ("賺", "獲利", "報酬", "投資")) and any(k in q for k in ("多少", "總共", "賺"))
            and not any(k in q for k in ("今天", "今日", "每天", "美股", "台股"))
            # 「報酬率X%每月要存多少達標」是試算題(走 #6)，不是問「投資賺多少」——別被這裡搶走
            and not any(k in q for k in ("存多少", "要存", "投多少", "要投", "每月", "每個月",
                                        "達標", "目標", "才能到", "才有辦法", "幾年", "多久", "存到"))):
        tr = int(pf.get("total_ret") or 0)
        return (f"你投資總共{'賺' if tr >= 0 else '虧'}了 {abs(tr)} 元（總報酬率 {pf.get('total_retpct')}%、"
                f"總市值 {int(pf.get('total_value') or 0)} 元）。（照唸這些數字。）")

    # 6) 達標試算：「每月要存多少才達標」「報酬率X%每月存多少」「幾年能到目標」→ 走確定性試算器。
    #    這種題 flash-lite 只會嘴上「我來幫你算」卻不呼叫工具，還把 400萬 目標幻覺成 600萬(它把自己
    #    上一句錯話存進 chat_history 又讀回來、自我強化)。直接程式算好回完整數字，模型完全不碰。
    if (any(k in q for k in ("存多少", "要存", "投多少", "要投", "每月", "每個月", "一個月",
                             "幾年", "多久", "多少年", "怎麼達"))
            and any(k in q for k in ("目標", "達標", "報酬", "年化", "退休", "數字", "才能到", "才有辦法", "存到"))):
        try:
            g = _load_goal()
            # 使用者在問句裡明講目標金額(「存到500萬」)或年齡(「32歲」)→ 用它，別硬套預設400萬/30歲(否則答錯)
            _tgt = g["target"]
            _amt = _zh_amount(q)   # 吃阿拉伯+中文數字(八百萬也行)
            if _amt >= 100000:     # 合理目標(≥10萬)才採用，避免把年齡/小數字誤當目標
                _tgt = _amt
            _tage = g["target_age"]
            _ma = re.search(r"(\d{2})\s*歲", q)
            if _ma and 18 <= int(_ma.group(1)) <= 100:
                _tage = int(_ma.group(1))
            resp = finance_project_ep(target=_tgt, target_age=_tage)
            import json as _json
            txt = _json.loads(bytes(resp.body).decode()).get("text")
            if txt:
                return txt
        except Exception:
            pass

    return None


@app.get("/finance_summary")
def finance_summary_ep(q: str = "", guest: bool = False):
    """給語音/Telegram 用的口語財務摘要。q=使用者問題：只問花費就不抓股價（快），問投資才抓。
    guest=True → 走訪客版（遮所有金額、只留%）。
    【隱私自動鎖】就算沒明寫 guest，只要「當前說話人不是 Owen」也一律走訪客版，
    所以訪客講話時連大腦呼叫的財務工具都自動遮金額，不靠 prompt 把關。"""
    try:
        # 自動依「現在講話的是誰」決定要不要遮(主人=完整，訪客=遮金額)
        if not guest:
            try:
                from modules.people import people_memory as _pm
                guest = not _pm.is_owner()
            except Exception:
                guest = False
        from modules.finance import wealth
        # 有帶問題且純花費 → 跳過即時股價；沒帶問題（向後相容）或問投資 → 照舊全抓
        need_pf = _needs_portfolio(q) if q else True
        d = wealth.overview(with_portfolio=need_pf)
        pf = d.get("portfolio", {})
        if guest:
            return JSONResponse({"ok": True, "text": _finance_guest_view(d, pf), "guest": True})
        # 【聚焦回答·錢不能錯】Owen 每天問的那幾種錢的問題 → 程式直接算好、回一句精準答案，
        # 模型只負責照唸，不用自己從一大坨資料挑重點(挑錯/亂編整數就是反覆出錯的根因)。有界、只涵蓋高頻題。
        _focus = _focused_finance_answer(q, d, pf)
        if _focus is None and q:
            # 關鍵字沒命中 → 走【語意層】把講法正規化後再試一次，讓「換句話說」也能命中確定性答案
            _q2 = _semantic_finance_q(q)
            if _q2 and _q2 != q:
                _focus = _focused_finance_answer(_q2, d, pf)
        if _focus is not None:
            return JSONResponse({"ok": True, "text": _focus})
        parts = []
        # 現金流（以發薪週期為準，存款先扣）
        parts.append(f"月收入{d['income']}元、固定開銷{d['fixed']}元")
        if d.get("auto_saved"):
            parts.append(f"每月固定投資{d['auto_saved']}元（發薪先存，這是唯一固定投入的金額）")
        if d.get("month_var"):
            parts.append(f"本期（{d.get('cycle_label','')}）目前生活花了{d['month_var']}元（這個週期還沒過完）")
        parts.append(f"本期到目前還剩{d['remaining']}元可用（週期還沒過完，這是浮動的、不是固定能投資的錢，要算投資目標一律用 project_wealth_goal 工具、別把這筆當每月可投）")
        # 直接答「每天還能花多少」：剩餘 ÷ 距發薪天數
        if d.get("daily_allowance") is not None and d.get("days_left"):
            parts.append(f"距下次發薪還有{d['days_left']}天，所以平均下來【每天還能花約 {int(d['daily_allowance'])} 元】（這就是「每天可花/每天還能花多少」的答案）")
        # 今日花費明細（讓「列出今天每項花費」答得出來）；排除「之前花費」這種補登調整
        _td = wealth._today() if hasattr(wealth, "_today") else None
        today_items = [e for e in d.get("expenses", [])
                       if str(e.get("date", "")).startswith(str(_td))
                       and e.get("category") != "之前花費"]
        if today_items:
            _ti = "、".join(f"{e.get('category', '其他')} {int(float(e.get('amount') or 0))}元"
                            + (f"（{e.get('note')}）" if e.get('note') else "")
                            for e in today_items)
            _ts = sum(float(e.get("amount") or 0) for e in today_items)
            parts.append(f"今天（{_td}）花了{int(_ts)}元，共{len(today_items)}筆，明細：{_ti}")
        else:
            parts.append(f"今天（{_td}）目前沒有記到花費")
        # 投資
        if pf.get("items"):
            closed = pf.get("market_open") is False
            today = "今天休市" if closed else f"今天{'賺' if pf['total_today']>=0 else '虧'}{abs(pf['total_today'])}元"
            # 把「總共賺多少錢(金額)」明確放進去——問「賺多少錢」就唸這個金額,不要只給報酬率%(以前就吃這個虧)。
            _tret = int(pf.get('total_ret', 0))
            _tdir = '賺' if _tret >= 0 else '虧'
            parts.append(f"投資總市值{pf['total_value']}元、總共{_tdir}了{abs(_tret)}元（總報酬率{pf['total_retpct']}%）、{today}")
            _stale = pf.get("stale") or []
            if _stale:
                parts.append(f"（要老實提醒Owen：{ '、'.join(_stale) }這{len(_stale)}檔現在抓不到即時股價、用的是上次的舊價，所以上面數字可能不是最新，等一下再問一次會更準）")
            # 【台股/美股分開小計】用 wealth 算好的 by_market（已正確把美股換算成台幣;
            # 直接加每檔 value 會錯,因為美股 value 欄位是美金原值——以前 LLM 心算就是吃這個虧,數字每次不一樣）。
            bm = pf.get("by_market", {})
            for code in ("TW", "US"):
                g = bm.get(code)
                if not g:
                    continue
                _gdir = "賺" if g.get("ret", 0) >= 0 else "虧"
                _tail = "（不含美股，全部換算台幣）" if code == "TW" else "（已換算台幣）"
                # 加上這個市場「今天」的表現，這樣問「美股/台股今天的報酬」答得對(別跟總報酬率混)。
                _gtoday = ""
                if not closed and g.get("today") is not None:
                    _gtd = "賺" if g.get("today", 0) >= 0 else "虧"
                    _gtoday = (f"，【{g.get('label')}今天{_gtd}{abs(int(g.get('today', 0)))}元、"
                               f"今天報酬率{'+' if g.get('todaypct', 0) >= 0 else ''}{g.get('todaypct')}%】")
                parts.append(f"其中{g.get('label')}部分：市值{g.get('value')}元、{_gdir}{abs(int(g.get('ret', 0)))}元、"
                             f"總報酬率{g.get('retpct')}%（共{g.get('count')}檔）{_tail}{_gtoday}")
            # 各檔簡述（最多列幾檔報酬率，標明台/美）
            tops = "；".join(f"{it.get('name') or it.get('symbol')}（{'台股' if it.get('market')=='TW' else '美股'}）{'賺' if it['ret']>=0 else '虧'}{it['retpct']}%" for it in pf["items"][:8])
            if tops:
                parts.append("各檔總報酬：" + tops)
            # 各檔【今日】漲跌（答「今天表現/今天漲跌」用，跟總報酬分開）
            if not closed:
                td = "；".join(
                    f"{it.get('name') or it.get('symbol')}（{'台股' if it.get('market')=='TW' else '美股'}）今日{'+' if it.get('todaypct',0)>=0 else ''}{it.get('todaypct',0)}%"
                    for it in pf["items"][:8] if it.get("todaypct") is not None)
                if td:
                    parts.append("各檔今日漲跌：" + td)
        # 淨資產/FIRE 只有抓了投資才有（純花費問題會略過，省抓股價時間）
        if d.get("net_worth") is not None:
            nw = f"淨資產合計{d['net_worth']}元"
            if d.get("cash"):
                nw += f"（含銀行現金{d['cash']}元）"
            parts.append(nw)
            if d.get("fire_pct"):
                parts.append(f"距離財務自由（目標{d['fire_target']}元）已達成{d['fire_pct']}%")
        text = "。".join(parts) + "。"
        return JSONResponse({"ok": True, "text": text, "data": d})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "text": "查財務時出了點問題"})


@app.get("/finance/project")
def finance_project_ep(target: float = 4000000, years: float = 0, target_age: int = 0,
                       monthly: float = 0):
    """財務目標試算（確定性計算，不讓 LLM 自己心算）。
    以 Owen【固定每月投入】為基準（發薪先存的金額，這是他真正承諾的；其他餘裕是變動加碼、不能當固定）。
    monthly=自訂每月固定投入(留空=用發薪先存的金額)。target=目標金額，years/target_age=時間。"""
    try:
        # 【隱私鎖】同 networth_trend：訪客不給看 Owen 的真實資產/缺口數字。
        try:
            from modules.people import people_memory as _pm
            if not _pm.is_owner():
                return JSONResponse({"ok": True, "guest": True,
                                     "text": "這是 Owen 的私人財務規劃，我不能透露喔。"})
        except Exception:
            pass
        from modules.finance import wealth
        import datetime as _dt
        # 沒明確指定就用「使用者自己設定的可調整目標」(finance.json 的 goal，預設 400 萬/30 歲)
        _g = _load_goal()
        if not target or target == 4000000:
            target = _g["target"]
        if not target_age:
            target_age = _g["target_age"]
        d = wealth.overview(with_portfolio=True)
        pv = float(d.get("net_worth") or 0)            # 現有淨資產
        income = float(d.get("income") or 0)
        fixed = float(d.get("fixed") or 0)
        auto_saved = float(d.get("auto_saved") or 0)   # 發薪先存＝固定投入（他真正承諾的）
        # 固定每月投入：用自訂值，否則用「發薪先存」(= 他每月唯一固定投資的錢)
        m_fixed = float(monthly) if monthly and monthly > 0 else auto_saved
        m_max = max(0.0, income - fixed)               # 理論上限(收入−固定開銷,等於完全不花變動)
        # 年數一律【從生日算】(使用者生日見下行設定)→ 模型亂傳 years 也不會錯。
        # 只有明確沒給 target_age 又給了 years(例如「還有3年」)才用 years。
        bday = _dt.date(2000, 1, 1)  # ← 換成你的生日(算「距目標年齡幾年」用)
        today = _dt.date.today()
        age = today.year - bday.year - ((today.month, today.day) < (bday.month, bday.day))
        if target_age and target_age > age:
            n = float(target_age - age)
        elif years and years > 0:
            n = float(years)
        else:
            n = max(0.1, 30 - age)   # 預設目標 30 歲

        def fv(pv, monthly, r, n):
            ap = monthly * 12
            if abs(r) < 1e-9:
                return pv + ap * n
            return pv * (1 + r) ** n + ap * (((1 + r) ** n - 1) / r)

        def required_rate(pv, monthly, target, n):
            lo, hi = -0.5, 2.0
            if fv(pv, monthly, hi, n) < target:
                return None
            for _ in range(80):
                mid = (lo + hi) / 2
                if fv(pv, monthly, mid, n) >= target:
                    hi = mid
                else:
                    lo = mid
            return hi

        # 反解：在某個「合理年化報酬」下，每月要投入多少才達標（讓使用者知道要加碼多少）
        def required_monthly(pv, target, r, n):
            fv_pv = pv * (1 + r) ** n if abs(r) >= 1e-9 else pv
            if abs(r) < 1e-9:
                annual = (target - fv_pv) / n
            else:
                annual = (target - fv_pv) / (((1 + r) ** n - 1) / r)
            return max(0.0, annual / 12)

        gap = target - pv
        # 【邊界防護】避免把荒謬數字（0元、天文數字、負報酬）唸給使用者
        if pv >= target:
            return JSONResponse({"ok": True, "text":
                f"你目前淨資產 {int(pv)} 元，已經達成 {int(target)} 元的目標了（超出 {int(pv - target)} 元）！"
                f"要不要把目標往上調（跟我說「把目標改成 X 萬」），或挑戰更早達標？（照唸，別再算每月要投多少。）"})
        if n < 0.5:
            return JSONResponse({"ok": True, "text":
                f"你設的達標年齡（{target_age} 歲）已經到了，但還差 {int(gap)} 元才到 {int(target)} 元。"
                f"要繼續衝的話，把目標年齡往後調（跟我說「改成 XX 歲達標」），我再算每月要投多少。（照唸這些數字。）"})
        rr_fixed = required_rate(pv, m_fixed, target, n)   # 維持固定投入下,需要的年化報酬
        zero_fixed = fv(pv, m_fixed, 0, n)                 # 固定投入+零報酬的終值
        need_m_10 = required_monthly(pv, target, 0.10, n)  # 年化10%下每月要投多少
        need_m_15 = required_monthly(pv, target, 0.15, n)  # 年化15%下每月要投多少
        pf = d.get("portfolio", {})
        cum_ret = pf.get("total_retpct")

        def pct(r):
            return f"{r*100:.1f}%" if r is not None else "超過200%（這目標在此投入下太難）"
        _tgt_age = age + int(round(n))
        parts = [
            f"【鐵的事實，照唸別改】Owen 是 2001 年生、現在就是 {age} 歲（不是 30 歲，使用者說的「30歲」是『目標年齡』不是現在年齡）。"
            f"目標：在 {_tgt_age} 歲達到 {int(target)} 元，所以還有 {n:.1f} 年。"
            f"目前淨資產{int(pv)}元、缺口{int(gap)}元。",
            f"用你【固定每月投入{int(m_fixed)}元】算（其他餘裕看當月、有就加碼、不算進來）：",
            f"光固定投入零報酬，{n:.1f}年後約{int(zero_fixed)}元；要達標需要年化報酬{pct(rr_fixed)}。",
            f"換個角度：若年化抓務實的10%，每月要投約{int(need_m_10)}元才達標；若能做到15%，每月約{int(need_m_15)}元。",
            f"所以關鍵是『固定投入加碼多少』+『報酬率』兩個一起看，餘裕越多、需要的報酬就越低。",
        ]
        if cum_ret is not None:
            parts.append(
                f"提醒：你投資的{cum_ret}%是【持有至今累積總報酬】不是每年的，別跟上面年化報酬直接比。")
        text = "".join(parts)
        return JSONResponse({"ok": True, "text": text, "data": {
            "target": target, "years": round(n, 2), "net_worth": int(pv), "gap": int(gap),
            "monthly_fixed": int(m_fixed), "monthly_max": int(m_max),
            "zero_return_value": int(zero_fixed), "required_rate_at_fixed": rr_fixed,
            "monthly_needed_at_10pct": int(need_m_10), "monthly_needed_at_15pct": int(need_m_15),
            "cumulative_return_pct": cum_ret,
        }})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "text": "財務試算出了點問題"})


@app.get("/finance/analysis")
def finance_analysis_ep():
    """記帳分析：月比較、趨勢、分類佔比、分類預算、洞察、常用項目。"""
    try:
        from modules.finance import expense_insights as ei
        return JSONResponse({"ok": True, "data": ei.analysis()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/finance/stock_ai")
def finance_stock_ai_ep():
    """AI 投資分析（套用 daily_stock_analysis 概念）：每檔持股訊號 + 組合評估。"""
    try:
        from modules.finance import stock_advisor
        return JSONResponse(stock_advisor.analyze())
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/finance/compute")
async def finance_compute_ep(req: Request):
    """用 Gemini code execution 真的跑程式算任何財務問題（幾倍/加總/比例/排序…），不靠 LLM 心算。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = (body or {}).get("question", "")
    if not q:
        return JSONResponse({"ok": False, "error": "請給 question"})
    try:
        from modules.finance import stock_advisor
        import asyncio as _aio
        d = await _aio.get_event_loop().run_in_executor(None, stock_advisor.compute, q)
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/calc")
async def calc_ep(req: Request):
    """通用計算：用 code execution 算任何關於使用者資料的問題（財務/記帳/提醒/待辦/活動/睡眠）。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = (body or {}).get("question", "")
    if not q:
        return JSONResponse({"ok": False, "error": "請給 question"})
    try:
        from modules import compute_tool
        import asyncio as _aio
        d = await _aio.get_event_loop().run_in_executor(None, compute_tool.calc, q)
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


def _do_search(q):
    payload = {
        "contents": [{"parts": [{"text":
            "用 Google 搜尋回答下面問題，繁體中文、簡潔白話、只講重點：\n" + str(q)}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3},
    }
    import time as _t
    last = None
    for _attempt in range(3):   # 5xx 上游瞬斷自動重試，別讓使用者看到「搜尋失敗」
        try:
            r = urllib.request.Request(
                "http://127.0.0.1:8808/v1beta/models/gemini-2.5-flash:generateContent",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            resp = json.load(urllib.request.urlopen(r, timeout=60))
            cands = resp.get("candidates") or []
            if not cands:
                return {"ok": False, "error": "搜尋無回應（可能被過濾或額度用盡）"}
            parts = (cands[0].get("content") or {}).get("parts") or []
            return {"ok": True, "answer": "".join(p.get("text", "") for p in parts).strip()}
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code in (500, 502, 503, 504) and _attempt < 2:
                _t.sleep(2.5)
                continue
            break
        except Exception as ex:
            last = ex
            break
    return {"ok": False, "error": "搜尋失敗：%s" % last}


@app.post("/search")
async def search_ep(req: Request):
    """即時上網搜尋（Gemini google_search grounding）：回答需要當前/外部資訊的問題，不靠腦補。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    q = (body or {}).get("query", "")
    if not q:
        return JSONResponse({"ok": False, "error": "請給 query"})
    try:
        import asyncio as _aio
        d = await _aio.get_event_loop().run_in_executor(None, _do_search, q)
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/finance/stock_news")
def finance_stock_news_ep():
    """持股最新新聞（Gemini 接地搜尋）+ 影響判讀。"""
    try:
        from modules.finance import stock_advisor
        return JSONResponse(stock_advisor.news())
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/finance/category_budget")
async def finance_cat_budget_ep(req: Request):
    """設定某分類的月預算。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    try:
        from modules.finance import expense_insights as ei
        cb = ei.set_category_budget(_t2t((b or {}).get("category", "")), (b or {}).get("amount", 0))
        return JSONResponse({"ok": True, "category_budgets": cb})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/finance/op")
async def finance_op_ep(req: Request):
    """語音/Telegram 即時改財務的統一入口。回口語確認句。
    action: set_save_goal | set_spend_limit | set_income | set_fixed |
            remove_income | remove_fixed | set_holding | remove_holding
    """
    try:
        b = await req.json()
    except Exception:
        b = {}
    if isinstance(b, dict):  # 名稱/備註簡轉繁
        for _k in ("name", "note"):
            if b.get(_k):
                b[_k] = _t2t(b[_k])
    action = (b or {}).get("action", "")
    # 需要數字金額的動作：先驗證 amount 真的是數字。否則像「五百萬」會被 _num 當 0，
    # 把存款/上限/餘額整個歸零（fuzz 抓到的真 bug）。非數字一律拒絕、不動資料。
    _AMOUNT_ACTIONS = {"set_save_goal", "set_spend_limit", "set_cash", "set_fire_target",
                       "set_fire_annual", "set_payday", "set_remaining", "set_income", "set_fixed"}
    if action in _AMOUNT_ACTIONS:
        _raw = b.get("amount")
        _ok = isinstance(_raw, (int, float)) and not isinstance(_raw, bool)
        if not _ok and isinstance(_raw, str):
            _ok = bool(re.match(r"^\s*-?[\d,]+(\.\d+)?\s*$", _raw))
        if not _ok:
            return JSONResponse({"ok": False,
                                 "text": "這個金額我沒看懂，直接給我數字就好，例如「5000」。"})
        # 金額不能是負的：負的存款目標/上限/收入會讓「可花金額」灌水、數字全亂。
        try:
            if float(str(_raw).replace(",", "").strip()) < 0:
                return JSONResponse({"ok": False,
                                     "text": "金額不能是負的喔，給我正的數字就好。"})
        except Exception:
            pass
    try:
        from modules.finance import wealth
        def _n(v):
            return wealth._num(v)
        if action == "set_save_goal":
            wealth.set_budget_field("save_goal", b.get("amount"))
            msg = f"好，每月固定投入投資改成 {int(_n(b.get('amount')))} 元了"
        elif action == "set_spend_limit":
            wealth.set_budget_field("spend_limit", b.get("amount"))
            msg = f"好，每月花費上限設成 {int(_n(b.get('amount')))} 元"
        elif action == "set_cash":
            v = wealth.set_top_field("cash", b.get("amount"))
            msg = f"好，銀行現金存款記成 {int(v):,} 元了，淨資產會更準"
        elif action == "set_fire_target":
            v = wealth.set_top_field("fire_target", b.get("amount"))
            msg = f"好，財務自由目標設成 {int(v):,} 元"
        elif action == "set_fire_annual":
            v = wealth.set_top_field("fire_annual", b.get("amount"))
            msg = f"好，FIRE 年開銷設成 {int(v):,} 元，財務自由目標自動算成 {int(v)*25:,} 元"
        elif action == "set_payday":
            v = wealth.set_top_field("payday", b.get("amount"))
            msg = f"好，發薪日設成每月 {int(v)} 號，財務週期改以此為準"
        elif action == "set_remaining":
            # 使用者說「我只剩X要活到發薪」→ 把本期還能花設成 X
            # （補記之前忘了細項的花費，讓數字對上），並算每天可花多少
            target = _n(b.get("amount"))
            ov = wealth.overview()
            adj = round(ov["remaining"] - target)
            if adj > 0:
                from modules.productivity import expense_tracker as et
                # 補登的「之前花費」記在發薪週期起始日，而非今天 → 不會灌爆「今日花費」
                et.add_expense(adj, "之前花費", "記不得細項，補記讓還能花對上",
                               date=ov.get("cycle_start"))
            elif adj < 0:
                # 還能花比目標少 → 使用者其實能花更多 → 把本期可花上限往上設(覆寫)，讓還能花升到 target。
                # 修正之前只能往下調、不能往上加的鬼打牆。
                wealth.set_budget_field("spend_limit", round(ov["spendable"] - adj))
            ov = wealth.overview()  # 重算
            try:
                cend = datetime.datetime.strptime(ov["cycle_end"], "%Y-%m-%d").date()
                days = max(1, (cend - datetime.datetime.now(_TZTW).date()).days + 1)
            except Exception:
                days = 1
            actual = int(ov["remaining"])
            daily = int(actual / days) if days else actual
            extra = f"，已幫你補記之前花掉的 {adj:,} 元" if adj > 0 else ""
            msg = (f"了解，你本期還能花 {actual:,} 元{extra}。"
                   f"距下次發薪還 {days} 天，每天約可花 {daily:,} 元")
        elif action in ("set_income", "set_fixed"):
            key = "income" if action == "set_income" else "fixed"
            it = wealth.upsert_named(key, b.get("name", ""), b.get("amount"), b.get("note"))
            label = "收入" if key == "income" else "固定開銷"
            msg = f"好，{label}「{it['name']}」設成 {int(_n(it['amount']))} 元了"
        elif action in ("remove_income", "remove_fixed"):
            key = "income" if action == "remove_income" else "fixed"
            n = wealth.remove_named(key, b.get("name", ""))
            msg = f"刪掉了 {n} 筆「{b.get('name','')}」" if n else f"沒找到「{b.get('name','')}」這筆"
        elif action == "set_holding":
            # 驗證 shares/cost 是數字（語音「五百股」會被 _num 當 0 把持股歸零）
            def _numok(v):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return True
                return isinstance(v, str) and bool(re.match(r"^\s*-?[\d,]+(\.\d+)?\s*$", v))
            for _f in ("shares", "cost"):
                if _f in b and b.get(_f) is not None and not _numok(b.get(_f)):
                    return JSONResponse({"ok": False,
                                         "text": "股數和成本要給我數字喔，例如「500 股、成本 92」。"})
            h = wealth.upsert_holding(b.get("symbol", ""), b.get("market"),
                                      b.get("name"), b.get("shares"), b.get("cost"))
            msg = f"好，持股 {h.get('name') or h.get('symbol')} 更新成 {int(_n(h.get('shares')))} 股、成本 {_n(h.get('cost')):g}"
        elif action == "remove_holding":
            n = wealth.remove_holding(b.get("symbol") or b.get("name", ""))
            msg = f"賣掉/刪掉了 {n} 檔" if n else "沒找到那一檔"
        else:
            return JSONResponse({"ok": False, "text": f"我看不懂這個動作（{action}）"})
        # 廣播給 dashboard 即時刷新
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8811/api/event",
                data=json.dumps({"type": "finance", "data": msg}).encode(),
                headers={"Content-Type": "application/json"}), timeout=3)
        except Exception:
            pass
        return JSONResponse({"ok": True, "text": msg})
    except ValueError as ve:
        return JSONResponse({"ok": False, "error": str(ve), "text": str(ve)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "text": "改的時候出了點問題"})


@app.put("/finance/{key}")
async def finance_put_ep(key: str, req: Request):
    """整批覆寫 income / fixed / holdings / expenses。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    items = body.get("items", []) if isinstance(body, dict) else (body or [])
    try:
        from modules.finance import wealth
        if key == "expenses":
            saved = wealth.set_expenses(items)
        elif key in ("income", "fixed", "holdings"):
            saved = wealth.set_list(key, items)
        else:
            return JSONResponse({"ok": False, "error": f"未知欄位 {key}"})
        return JSONResponse({"ok": True, "items": saved})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/finance/budget")
async def finance_budget_ep(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    try:
        from modules.finance import wealth
        b = wealth.set_budget(body.get("save_goal", 0), body.get("spend_limit", 0))
        return JSONResponse({"ok": True, "budget": b})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/timer")
async def timer_ep(req: Request):
    """計時器：N 分鐘後提醒。寫成 once 提醒。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    try:
        raw = (b or {}).get("minutes", 0)
        # 容錯：可能傳 "5"、"5分鐘"、"半小時"、"1小時" 等
        try:
            minutes = float(raw)
        except (ValueError, TypeError):
            s = str(raw)
            mm = re.search(r"([0-9.]+)", s)
            minutes = float(mm.group(1)) if mm else 0
            if "小時" in s or "鐘頭" in s:
                minutes *= 60
            if "半" in s and minutes == 0:
                minutes = 30
        label = _t2t((b or {}).get("label", "時間到"))
        if minutes <= 0:
            return JSONResponse({"ok": False, "text": "要計時幾分鐘呢？例如「計時5分鐘」。"})
        now = datetime.datetime.now(zoneinfo.ZoneInfo("Asia/Taipei"))
        fire = now + datetime.timedelta(minutes=minutes)
        from modules.productivity import reminder_manager as rm
        rm.add_reminder(fire.strftime("%H:%M"), label, repeat="once:" + fire.strftime("%Y-%m-%d"))
        return JSONResponse({"ok": True, "fire_at": fire.strftime("%H:%M"), "label": label})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/news")
def news_ep():
    """免費新聞：Google News 台灣 RSS 抓前幾條標題。"""
    try:
        import urllib.request as _u, re as _re
        url = "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        req = _u.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        xml = _u.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
        titles = _re.findall(r"<title>(.*?)</title>", xml)
        titles = [t.replace("<![CDATA[", "").replace("]]>", "").strip() for t in titles]
        titles = [t for t in titles[1:] if t and t != "Google 新聞"][:5]  # 跳過第一個(來源名)
        return JSONResponse({"ok": True, "text": "；".join(f"{i+1}. {t}" for i, t in enumerate(titles))})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/todo")
async def todo_add_ep(req: Request):
    """待辦：新增一項。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    item = _t2t((b or {}).get("item", "").strip())
    if not item:
        return JSONResponse({"ok": False, "error": "缺 item"})
    try:
        from modules.productivity import checklist_manager as cm
        cm.add_item(item, "todo")   # 待辦專用清單，跟出門清單(out)分開
        _notify("todo", item[:30])
        return JSONResponse({"ok": True, "item": item})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


_reconciler_mod = None


def _get_reconciler():
    """快取載入 expense_reconciler(給即時記帳攔截用)。"""
    global _reconciler_mod
    if _reconciler_mod is None:
        import importlib.util
        import os as _os
        _sp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "expense_reconciler.py")
        _spec = importlib.util.spec_from_file_location("expense_reconciler", _sp)
        _reconciler_mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_reconciler_mod)
    return _reconciler_mod


@app.post("/expense_auto")
async def expense_auto_ep(req: Request):
    """語音橋接每輪呼叫：訊息若是明確「品項+金額」報帳，就確定性補記(去重)。
    解決 flash-lite 偶爾說「記好了」卻沒真的呼叫工具的漏帳。非報帳句直接跳過、零副作用。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    msg = str((b or {}).get("message", ""))
    try:
        import asyncio as _aio
        # reconcile_one 內含同步 HTTP(POST 回本服務記帳)→ 丟執行緒池跑，
        # 絕不阻塞事件迴圈(避免自我呼叫卡死，這正是之前搞壞 proxy 的坑)。
        rec = await _aio.get_event_loop().run_in_executor(None, _get_reconciler().reconcile_one, msg)
        return JSONResponse({"ok": True, "recorded": bool(rec)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/todo")
def todo_list_ep():
    try:
        from modules.productivity import checklist_manager as cm
        items = cm.get_items("todo")   # 待辦專用清單，跟出門清單(out)分開
        # 預先算好 完成/剩餘 數量 + 保留每項的 done 狀態，AI 直接唸，不用自己數/猜
        norm = []
        for i in items:
            if isinstance(i, dict):
                norm.append({"item": i.get("item", ""), "done": bool(i.get("done", False))})
            else:
                norm.append({"item": str(i), "done": False})
        names = [x["item"] for x in norm]
        done = sum(1 for x in norm if x["done"])
        return JSONResponse({"ok": True, "text": "；".join(names) if names else "待辦清單是空的",
                             "total": len(norm), "done_count": done,
                             "remaining_count": len(norm) - done, "items": norm})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})



@app.get("/due_reminders")
def due_reminders_ep():
    """回傳此刻到期的提醒(時間吻合且未響過),並標記已響。xiaozhi server 輪詢用。"""
    try:
        from modules.productivity import reminder_manager as rm
        due = rm.get_due_reminders()
        return JSONResponse({"ok": True, "due": [r.get("message", "") for r in due]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "due": []})



@app.get("/currency")
def currency_ep(amount: float = 1, frm: str = "USD", to: str = "TWD"):
    """免費匯率換算。"""
    try:
        import urllib.request as _u, json as _j
        frm = frm.upper(); to = to.upper()
        url = f"https://api.exchangerate-api.com/v4/latest/{frm}"
        d = _j.load(_u.urlopen(url, timeout=10))
        rate = d["rates"].get(to)
        if rate is None:
            return JSONResponse({"ok": False, "error": f"不支援 {to}"})
        result = round(amount * rate, 2)
        return JSONResponse({"ok": True, "text": f"{amount} {frm} = {result} {to}"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})



# 桌面特工：背景任務佇列（Gemini 當後台研究員，完成後裝置主動匯報）
_agent_results = []  # [{"task":..., "result":..., "delivered":False}]


def _run_agent_task(task: str):
    """【真·多步執行器】原本只打一次 flash(不會查網/不會做事、只會嘴)——改成 Claude Code 驅動：
    會自己規劃步驟、上網查、讀本地資料、寫檔到桌面、curl 本地 API(設提醒/查財務)，一條龍做完才回報。
    「一句話一條龍」靠這個成立。安全：唯讀工具+寫檔限桌面/工作區，不能改系統程式碼。"""
    import subprocess as _sp
    log = _HB_DIR + "/memory/logs/dispatch_task.log"
    workdir = os.path.expanduser("~/jarvis_tasks")
    os.makedirs(workdir, exist_ok=True)
    prompt = (
        "你是 Owen 的後台任務執行者(他用語音交代、你在背景做完)。任務：「" + task + "」\n"
        "你可以：WebSearch/WebFetch 上網查、Read 讀他系統的資料(財務在 http://127.0.0.1:8809/finance_summary?q=...、"
        "提醒用 curl -X POST http://127.0.0.1:8809/reminder -H 'Content-Type: application/json' "
        "-d '{\"time\":\"HH:MM\",\"message\":\"...\",\"repeat\":\"once:YYYY-MM-DD\"}')、"
        "產出文件就寫到 ~/Desktop（檔名用中文、內容排版好）。\n"
        "【務必】真的把任務做完（該查的查、該寫的寫、該設的設），不要只給建議。"
        "做完用繁體中文≤3句口語總結：做了什麼、產出在哪。這段話會被唸給 Owen 聽。"
    )
    try:
        with open(log, "a", encoding="utf-8") as lf:
            lf.write("\n\n===== %s @%s =====\n" % (task, _dt.datetime.now(_TZTW).isoformat()))
        r = _sp.run(
            [_CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits",
             "--add-dir", os.path.expanduser("~/Desktop"),
             "--allowedTools", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
             "Write", "Bash(curl:*)", "Bash(python3:*)"],
            cwd=workdir, capture_output=True, text=True, timeout=600)
        out = (r.stdout or "").strip()
        with open(log, "a", encoding="utf-8") as lf:
            lf.write(out[-3000:] + "\n---STDERR---\n" + (r.stderr or "")[:500])
        lines = [x for x in out.splitlines() if x.strip()]
        result = (lines[-1] if lines else "做完了，但沒拿到總結，細節在 dispatch log")[:300]
    except _sp.TimeoutExpired:
        result = "這個任務比較大，我還在做，等等再跟你回報"
    except Exception as e:
        result = f"任務處理時出錯：{str(e)[:100]}"
    _agent_results.append({"task": task, "result": result, "delivered": False})
    # durable 通知(Telegram)：語音在忙/裝置離線也一定收得到
    try:
        import sys as _sys
        _sys.path.insert(0, _HB_DIR)
        from modules.remote.telegram_handler import TelegramHandler as _TH
        _cfg = json.load(open(os.path.join(_HB_DIR, "config", "telegram.json")))
        for _uid in _cfg.get("allowed_user_ids", []):
            _TH().send_message(_uid, "✅ 你交代的「%s」：%s" % (task[:40], result))
    except Exception:
        pass


@app.post("/dispatch_task")
async def dispatch_task_ep(req: Request):
    """派一個複雜任務給後台 agent 執行，立即返回，完成後由裝置匯報。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    task = (b or {}).get("task", "").strip()
    if not task:
        return JSONResponse({"ok": False, "error": "缺 task"})
    threading.Thread(target=_run_agent_task, args=(task,), daemon=True).start()
    return JSONResponse({"ok": True, "task": task})


@app.get("/agent_results")
def agent_results_ep():
    """回傳已完成、尚未匯報的任務結果，並標記已匯報。xiaozhi server 輪詢用。"""
    out = []
    for r in _agent_results:
        if not r.get("delivered"):
            r["delivered"] = True
            out.append({"task": r["task"], "result": r["result"]})
    return JSONResponse({"ok": True, "results": out})


# ---------- 自我擴充：缺功能時，直接調用 Claude Code 把它做出來 ----------
_build_lock = __import__("threading").Lock()
_CLAUDE_BIN = "/Users/chenyouwei/.local/bin/claude"
_HB_DIR = "/Users/chenyouwei/Hermes_Brain"


def _run_build_feature(description: str):
    import subprocess as _sp
    log = _HB_DIR + "/memory/logs/build_feature.log"
    prompt = (
        "你是 Hermes（Owen 的桌上 StackChan AI 語音助理）的工程師。"
        "Owen 想要一個目前還沒有的能力：「" + description + "」。請【實際動手·全棧】把它做出來並串接好，不要只給建議。\n"
        "【① 後端】需要的邏輯或外部 API 放 /Users/chenyouwei/Hermes_Brain/scripts/hermes_memory_endpoint.py"
        "（FastAPI，port 8809，新增 @app 端點；資料存 config/ 下的 json）。\n"
        "【② 語音工具】對話工具放 /Users/chenyouwei/xiaozhi-server/patches/hermes_tools.py"
        "（用 @register_function 仿照現有 set_reminder / find_nearby，內部 http 打 http://host.docker.internal:8809），"
        "並把工具名加進 /Users/chenyouwei/xiaozhi-server/data/.config.yaml 的 Intent.function_call.functions 清單。\n"
        "【③ Dashboard·務必做】在 /Users/chenyouwei/Hermes_Brain/dashboard/index.html 加一個對應的區塊/面板顯示這個功能"
        "（仿照現有 panel 的寫法與 --cy/--gold 等 CSS 變數風格），並在 /Users/chenyouwei/Hermes_Brain/dashboard/hermes_dashboard.py"
        "加一個 /api 代理端點轉發到 8809，讓 Owen 在控制台(localhost:8811)就看得到、用得到。這步不能跳過。\n"
        "【④ 自我驗證】寫完後自己用 python -c 或 curl 快速驗證新端點/函式能跑、不報錯；有錯就修到好。\n"
        "架構風格要跟現有程式碼一致（繁體中文註解、小函式、錯誤處理）。"
        "做完後用繁體中文【一句話、口語、最多兩句】總結你做了什麼、以及在 dashboard 哪裡看得到。直接開始寫 code。"
    )
    summary = ""
    try:
        with _build_lock:
            with open(log, "a", encoding="utf-8") as lf:
                lf.write("\n\n===== %s @%s =====\n" % (
                    description, _dt.datetime.now(_TZTW).isoformat()))
            # 【安全閘·改碼前備份】build_feature 會用 acceptEdits 無人監督改這幾個關鍵檔。先全備份，
            # 建完做冒煙測試(Python 語法 + YAML 合法),壞了就自動還原,絕不讓自我進化把語音助理弄掛。
            _safe_files = ["/Users/chenyouwei/xiaozhi-server/patches/hermes_tools.py",
                           "/Users/chenyouwei/Hermes_Brain/scripts/hermes_memory_endpoint.py",
                           "/Users/chenyouwei/xiaozhi-server/data/.config.yaml",
                           "/Users/chenyouwei/Hermes_Brain/dashboard/index.html",
                           "/Users/chenyouwei/Hermes_Brain/dashboard/hermes_dashboard.py"]
            _bak = {}
            for _f in _safe_files:
                try:
                    with open(_f, "rb") as _bf:
                        _bak[_f] = _bf.read()
                except Exception:
                    pass
            def _smoke():
                """冒煙測試：.py 驗語法、.yaml 驗解析、.html 驗沒截斷。回壞掉清單(空=健康)。"""
                import ast as _ast
                bad = []
                for _f in _safe_files:
                    try:
                        if _f.endswith(".py"):
                            _ast.parse(open(_f, encoding="utf-8").read())
                        elif _f.endswith((".yaml", ".yml")):
                            import yaml as _yaml
                            _yaml.safe_load(open(_f, encoding="utf-8"))
                        elif _f.endswith(".html"):
                            _h = open(_f, encoding="utf-8").read()
                            if len(_h) < 2000 or "</html>" not in _h.lower():
                                raise ValueError("dashboard html 疑似被截斷/損壞")
                    except Exception as _e:
                        bad.append("%s: %s" % (os.path.basename(_f), str(_e)[:80]))
                return bad

            def _claude(p, tmo):
                rr = _sp.run(
                    # --add-dir 授權跨目錄改 xiaozhi-server(語音工具+config)；--allowedTools 讓它能自我驗證。
                    [_CLAUDE_BIN, "-p", p, "--permission-mode", "acceptEdits",
                     "--add-dir", "/Users/chenyouwei/xiaozhi-server",
                     "--allowedTools", "Edit", "Write", "Read",
                     "Bash(python3:*)", "Bash(curl:*)", "Bash(grep:*)"],
                    cwd=_HB_DIR, capture_output=True, text=True, timeout=tmo)
                oo = (rr.stdout or "").strip()
                with open(log, "a", encoding="utf-8") as lf:
                    lf.write(oo + "\n---STDERR---\n" + (rr.stderr or "")[:800])
                return oo

            # 【自我修正迴圈】不是「一次做完交差」：建→冒煙→壞了把【具體錯誤】餵回去叫它修，
            # 最多 3 輪；全輪都修不好才還原。這才是 agentic loop，不是 one-shot prompt。
            out = _claude(prompt, 900)
            _broken = _smoke()
            _round = 0
            while _broken and _round < 2:
                _round += 1
                with open(log, "a", encoding="utf-8") as lf:
                    lf.write("\n[LOOP] 第%d輪修復：%s\n" % (_round, "; ".join(_broken)))
                fix_prompt = (
                    "你剛才在 Hermes 系統實作「" + description + "」時把檔案改壞了，冒煙測試抓到這些問題：\n"
                    + "\n".join("- " + b for b in _broken)
                    + "\n請【只修這些錯誤】把檔案修到能正常解析/執行，不要做別的改動。修完再自我檢查一次。"
                )
                out = _claude(fix_prompt, 420) or out
                _broken = _smoke()
            if _broken:
                for _f, _data in _bak.items():
                    try:
                        with open(_f, "wb") as _wf:
                            _wf.write(_data)
                    except Exception:
                        pass
                with open(log, "a", encoding="utf-8") as lf:
                    lf.write("\n[SAFETY] 冒煙測試失敗，已還原：%s\n" % "; ".join(_broken))
                summary = "這個功能我做到一半發現會把系統改壞（%s），已經自動還原、沒動到你現有的東西。要不要換個做法？" % _broken[0][:50]
            else:
                lines = [x for x in out.splitlines() if x.strip()]
                summary = (lines[-1] if lines else "做好了，細節在 build log 裡")[:300]
                # 【自動上線】冒煙測試過了 → 延遲 4 秒重啟 8809(新端點)+dashboard(新面板)+容器(新語音工具)，
                # 讓新功能【自己上線】不用 Owen 手動重啟。延遲是為了讓「當前這個函式(跑在 8809 裡)」先把
                # summary 寫完、回報完，才重啟自己，不會半路自殺。
                try:
                    _uid = os.getuid()
                    _sp.Popen(["bash", "-c",
                               "sleep 4; "
                               "launchctl kickstart -k gui/%d/com.hermes.memoryendpoint; "
                               "launchctl kickstart -k gui/%d/com.hermes.dashboard 2>/dev/null; "
                               "docker restart xiaozhi-esp32-server >/dev/null 2>&1" % (_uid, _uid)],
                              start_new_session=True)
                    summary += "（已自動上線，控制台馬上看得到）"
                except Exception:
                    pass
    except _sp.TimeoutExpired:
        summary = "這個功能比較複雜，我還在做，等等再跟你回報"
    except Exception as e:
        summary = "建這個功能時卡住了：%s" % str(e)[:120]
    _agent_results.append({"task": "你要的「%s」這個新功能" % description,
                           "result": summary, "delivered": False})
    # 【durable 通知】build 完會自動重啟 8809 → 記憶體的 _agent_results 會被清掉、語音通知可能掉。
    # 所以同時推一則 Telegram(外部、不怕重啟),確保 Owen 一定收到「做完了」的回報。
    try:
        import sys as _sys
        _sys.path.insert(0, _HB_DIR)
        from modules.remote.telegram_handler import TelegramHandler as _TH
        _tg = _TH()
        _cfg = json.load(open(os.path.join(_HB_DIR, "config", "telegram.json")))
        for _uid in _cfg.get("allowed_user_ids", []):
            _tg.send_message(_uid, "🛠️ 你要的「%s」我做好了：%s" % (description[:30], summary))
    except Exception:
        pass


@app.post("/build_feature")
async def build_feature_ep(req: Request):
    """Hermes 缺功能時呼叫：背景叫 Claude Code 把功能寫出來，完成後由裝置口頭匯報。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    desc = (b or {}).get("description", "").strip()
    if not desc:
        return JSONResponse({"ok": False, "error": "缺 description"})
    __import__("threading").Thread(
        target=_run_build_feature, args=(desc,), daemon=True).start()
    return JSONResponse({"ok": True, "description": desc})


# ---------- coding 車道：叫「真 Claude Code」在獨立工作區做使用者要的東西（網站/html/app）----------
_BUILDS_DIR = os.path.expanduser("~/jarvis_builds")


def _run_code_task(task: str):
    """背景叫真 Claude Code 在獨立工作區把使用者要的東西做出來，完成後由裝置口頭匯報。
    跟 build_feature 不同：這是做『Owen 自己的專案』（放 ~/jarvis_builds），不碰 Hermes Brain 程式碼。"""
    import subprocess as _sp, re as _re
    os.makedirs(_BUILDS_DIR, exist_ok=True)
    slug = (_re.sub(r"[^\w一-鿿]+", "-", task)[:24].strip("-")) or "build"
    stamp = _dt.datetime.now(_TZTW).strftime("%m%d_%H%M%S")
    workdir = os.path.join(_BUILDS_DIR, "%s_%s" % (stamp, slug))
    os.makedirs(workdir, exist_ok=True)
    prompt = (
        "你是專業前端/全端工程師。Owen 要你做出這個東西：「" + task + "」。\n"
        "請【實際動手】在目前這個資料夾把它完整做出來——產生可以直接打開或執行的檔案"
        "（優先單一 index.html，需要時內嵌 CSS/JS，讓他用瀏覽器直接打開就能用）。"
        "要做得好看、完整、可用，不要只給說明或半成品。\n"
        "做完後用繁體中文【一句話、口語、最多兩句】總結你做了什麼、主檔案叫什麼。直接開始寫 code。"
    )
    summary = ""
    log = _HB_DIR + "/memory/logs/code_task.log"
    try:
        with open(log, "a", encoding="utf-8") as lf:
            lf.write("\n\n===== %s @%s (%s) =====\n" % (
                task, _dt.datetime.now(_TZTW).isoformat(), workdir))
        r = _sp.run(
            [_CLAUDE_BIN, "-p", prompt, "--permission-mode", "acceptEdits"],
            cwd=workdir, capture_output=True, text=True, timeout=900)
        out = (r.stdout or "").strip()
        with open(log, "a", encoding="utf-8") as lf:
            lf.write(out + "\n---STDERR---\n" + (r.stderr or "")[:800])
        files = [f for f in os.listdir(workdir) if not f.startswith(".")]
        lines = [x for x in out.splitlines() if x.strip()]
        made = (lines[-1] if lines else "做好了")[:240]
        summary = ("%s（檔案在 %s）" % (made, workdir)) if files \
            else ("%s（但好像沒產出檔案，細節在 log）" % made)
    except _sp.TimeoutExpired:
        summary = "這個比較大，我還在做，等等回報你"
    except Exception as e:
        summary = "做這個時卡住了：%s" % str(e)[:120]
    _agent_results.append({"task": "你要我做的「%s」" % task,
                           "result": summary, "delivered": False})


@app.post("/code_task")
async def code_task_ep(req: Request):
    """叫真 Claude Code 在獨立工作區做出使用者要的網站/html/app，背景執行，完成口頭匯報。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    task = (b or {}).get("task", "").strip()
    if not task:
        return JSONResponse({"ok": False, "error": "缺 task"})
    __import__("threading").Thread(
        target=_run_code_task, args=(task,), daemon=True).start()
    return JSONResponse({"ok": True, "task": task})


# ---------- 萬能：用 Claude Code 操作電腦（風險分級：讀/建新=自動跑；改/刪現有=要審批）----------
# 自動路徑：只給【純唯讀/查詢】工具，零執行、零寫入 → 不可能造成傷害（語音觸發也安全）。
# 任何寫入/建檔/修改/刪除/跑指令 → 一律走控制台審批。
_SAFE_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch"
_pending_tasks = []  # 等審批的高風險任務 [{id,task,ts}]
_pt_lock = __import__("threading").Lock()


def _classify_risk(task: str) -> str:
    """用 Gemini 快速判斷：READ/CREATE(安全) 還是 EDIT/DELETE(要審批)。失敗 → 保守當 RISKY。"""
    try:
        payload = {"contents": [{"parts": [{"text": (
            "判斷這個電腦任務。只回一個字：\n"
            "回 SAFE = 純粹讀取/查詢/查資料/上網搜尋，完全不會建立、寫入、修改或刪除任何東西，也不跑任何指令。\n"
            "回 RISKY = 會建立或寫入檔案、修改/刪除現有檔案、跑腳本或指令、改設定、或任何會改變電腦狀態的操作。\n"
            "任務：" + task)}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 5}}
        req = urllib.request.Request(
            "http://127.0.0.1:8808/v1beta/models/gemini-2.5-flash:generateContent",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=20))
        ans = d["candidates"][0]["content"]["parts"][0]["text"].upper()
        return "RISKY" if "RISK" in ans else "SAFE"
    except Exception:
        return "RISKY"


def _exec_computer_task(task: str, approved: bool = False):
    """實際執行。approved=True（使用者在控制台按過確認）→ 用 acceptEdits（可改現有檔）；
    否則只給安全白名單工具（讀/建新/搜尋/上網），破壞性操作會被擋下。"""
    import subprocess as _sp
    log = _HB_DIR + "/memory/logs/computer_task.log"
    prompt = (
        "你是 Owen 的電腦助理，在他的 Mac 上完成這個任務：「" + task + "」。\n"
        + ("使用者已在控制台確認授權你進行寫入/修改等操作。" if approved else
           "【限制】你現在只能讀取與上網查資料，不能建立、寫入、修改或刪除任何檔案，也不能執行指令。"
           "如果這個任務其實需要動到檔案，請直接說明它需要授權，不要嘗試繞過。")
        + "完成後用繁體中文【一句話、口語、最多兩句】告訴 Owen 你做了什麼、結果（會唸給他聽）。"
    )
    cmd = [_CLAUDE_BIN, "-p", prompt]
    if approved:
        # 使用者已在控制台逐項審閱並按確認授權這個任務 → 給完整能力（人在迴圈中把關）。
        cmd += ["--permission-mode", "bypassPermissions"]
    else:
        cmd += ["--allowedTools", _SAFE_TOOLS]
    summary = ""
    try:
        with open(log, "a", encoding="utf-8") as lf:
            lf.write("\n\n===== [%s] %s @%s =====\n" % (
                "APPROVED" if approved else "AUTO-SAFE", task,
                _dt.datetime.now(_TZTW).isoformat()))
        r = _sp.run(cmd, cwd=os.path.expanduser("~"),
                    capture_output=True, text=True, timeout=600)
        out = (r.stdout or "").strip()
        with open(log, "a", encoding="utf-8") as lf:
            lf.write(out + "\n---STDERR---\n" + (r.stderr or "")[:800])
        lines = [x for x in out.splitlines() if x.strip()]
        summary = (lines[-1] if lines else "做完了，細節在 log 裡")[:300]
    except _sp.TimeoutExpired:
        summary = "這個任務有點久，我還在弄，等等回報你"
    except Exception as e:
        summary = "做這個任務時卡住了：%s" % str(e)[:120]
    _agent_results.append({"task": "你交代的「%s」" % task,
                           "result": summary, "delivered": False})


def _route_computer_task(task: str):
    risk = _classify_risk(task)
    if risk == "RISKY":
        with _pt_lock:
            tid = (max([p["id"] for p in _pending_tasks], default=0) + 1)
            _pending_tasks.append({"id": tid, "task": task,
                                   "ts": _dt.datetime.now(_TZTW).strftime("%H:%M:%S")})
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8811/api/event",
                data=json.dumps({"type": "approval", "data": task}).encode(),
                headers={"Content-Type": "application/json"}), timeout=3)
        except Exception:
            pass
        _agent_results.append({
            "task": "你要做的「%s」" % task,
            "result": "這個會動到現有的東西，我先放到控制台，你按確認我才動手喔",
            "delivered": False})
    else:
        _exec_computer_task(task, approved=False)


@app.post("/do_on_computer")
async def do_on_computer_ep(req: Request):
    """Hermes 要在電腦上做事時呼叫。讀/建新=自動跑；改/刪現有=排到控制台等審批。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    task = (b or {}).get("task", "").strip()
    if not task:
        return JSONResponse({"ok": False, "error": "缺 task"})
    __import__("threading").Thread(
        target=_route_computer_task, args=(task,), daemon=True).start()
    return JSONResponse({"ok": True, "task": task})


@app.get("/pending_tasks")
def pending_tasks_ep():
    with _pt_lock:
        return JSONResponse({"ok": True, "pending": list(_pending_tasks)})


@app.post("/approve_task")
async def approve_task_ep(req: Request):
    """使用者在控制台按確認 → 執行該高風險任務（acceptEdits，可改現有檔）。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    tid = (b or {}).get("id")
    decision = (b or {}).get("decision", "approve")
    task = None
    with _pt_lock:
        for p in list(_pending_tasks):
            if p["id"] == tid:
                task = p["task"]
                _pending_tasks.remove(p)
                break
    if task is None:
        return JSONResponse({"ok": False, "error": "找不到該任務"})
    if decision == "approve":
        __import__("threading").Thread(
            target=_exec_computer_task, args=(task, True), daemon=True).start()
    return JSONResponse({"ok": True, "task": task, "decision": decision})



def _oh_open_now(oh, now):
    """粗略判斷 opening_hours 字串『現在』有沒有開。看不懂回 None(不亂猜)。"""
    import re
    try:
        if not oh:
            return None
        if "24/7" in oh:
            return True
        spans = re.findall(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", oh)
        if not spans:
            return None
        cur = now.hour * 60 + now.minute
        for h1, m1, h2, m2 in spans:
            a = int(h1) * 60 + int(m1)
            b = int(h2) * 60 + int(m2)
            if b <= a:
                b += 24 * 60  # 跨午夜
            if a <= cur <= b:
                return True
        return False
    except Exception:
        return None


@app.get("/nearby")
def nearby_ep(keyword: str = "餐廳", location: str = ""):
    """找附近的店。用 Overpass(OSM POI 搜尋)真的找出某座標附近的店家——
    不是 Nominatim 地理編碼(那只會用名字找特定地點、搜不到『附近的餐廳』)。還會判斷現在有沒有開。"""
    import urllib.parse as _up, urllib.request as _ur, json as _j, datetime as _dt
    UA = {"User-Agent": "Hermes-Assistant/1.0 (you@example.com)"}
    try:
        # 1) 定位 → lat/lon
        if location.strip():
            gu = "https://nominatim.openstreetmap.org/search?" + _up.urlencode(
                {"q": location, "format": "json", "limit": 1})
            g = _j.load(_ur.urlopen(_ur.Request(gu, headers=UA), timeout=12))
            if not g:
                return JSONResponse({"ok": False, "text": f"找不到「{location}」這個地方，換個說法試試"})
            lat, lon, where = float(g[0]["lat"]), float(g[0]["lon"]), location
        else:
            # 沒給地點：用使用者已知所在地(self_location,例如板橋)去定位，不要用 IP——
            # Mac 在手機熱點上時 IP 會定位到電信商機房，完全找不到使用者附近的店家。
            _loc = (_self_location() or "板橋").strip()
            gu = "https://nominatim.openstreetmap.org/search?" + _up.urlencode(
                {"q": _loc + " 台灣", "format": "json", "limit": 1})
            try:
                g = _j.load(_ur.urlopen(_ur.Request(gu, headers=UA), timeout=12))
            except Exception:
                g = None
            if not g:
                return JSONResponse({"ok": False, "text": "我抓不到你現在的位置，跟我說個區域我再幫你找"})
            lat, lon, where = float(g[0]["lat"]), float(g[0]["lon"]), _loc
        # 2) keyword → OSM 類別
        k = keyword or ""
        if any(w in k for w in ("咖啡", "cafe", "coffee")):
            flt, kind = '["amenity"="cafe"]', "咖啡廳"
        elif any(w in k for w in ("超商", "便利商店", "便利店", "7-11", "全家", "萊爾富", "ok")):
            flt, kind = '["shop"="convenience"]', "便利商店"
        elif any(w in k for w in ("藥局", "藥妝", "藥房", "pharmacy")):
            flt, kind = '["amenity"="pharmacy"]', "藥局"
        elif any(w in k for w in ("加油")):
            flt, kind = '["amenity"="fuel"]', "加油站"
        elif any(w in k for w in ("酒吧", "bar", "居酒屋")):
            flt, kind = '["amenity"~"bar|pub"]', "酒吧"
        else:
            flt, kind = '["amenity"~"restaurant|fast_food|food_court|cafe"]', "餐廳"
        # 3) Overpass 查附近 POI(node+way,半徑 900m)
        q = (f'[out:json][timeout:20];('
             f'node{flt}(around:900,{lat},{lon});'
             f'way{flt}(around:900,{lat},{lon}););out tags center 40;')
        req = _ur.Request("https://overpass-api.de/api/interpreter",
                          data=("data=" + _up.quote(q)).encode(), headers=UA)
        od = _j.load(_ur.urlopen(req, timeout=25))
        now = _dt.datetime.now()
        items = []
        for e in od.get("elements", []):
            t = e.get("tags", {})
            nm = t.get("name") or t.get("name:zh") or t.get("name:en")
            if not nm:
                continue
            items.append((nm, _oh_open_now(t.get("opening_hours", ""), now), t.get("cuisine", "")))
        if not items:
            return JSONResponse({"ok": True, "text": f"{where}附近的地圖資料裡我找不到{kind}，這區 OSM 可能收錄少，換個區域或關鍵字試試"})
        open_items = [x for x in items if x[1] is True]
        show = (open_items if open_items else items)[:6]
        parts = []
        for nm, opn, cuisine in show:
            tag = "(現在有開)" if opn is True else ("(已打烊)" if opn is False else "")
            parts.append(nm + (f"・{cuisine}" if cuisine else "") + tag)
        head = f"{where}附近" + ("現在有開的" if open_items else "的") + kind
        text = head + "有：" + "、".join(parts)
        return JSONResponse({"ok": True, "text": text, "places": [x[0] for x in show]})
    except Exception as e:
        return JSONResponse({"ok": False, "text": "地圖服務剛剛連不上，等等再問我"})


# 能力分組對照（新增工具到 config 後，這裡補一行就會出現在自我認知裡）
_CAP_MAP = {
    "get_weather_free": ("查詢", "查即時天氣"),
    "get_news": ("查詢", "查今天新聞"),
    "convert_currency": ("查詢", "換匯算錢"),
    "find_nearby": ("查詢", "找附近的店/地點(有地圖)"),
    "set_reminder": ("記錄管理", "設提醒"),
    "set_timer": ("記錄管理", "設計時器"),
    "add_expense": ("記錄管理", "記帳"),
    "query_expenses": ("記錄管理", "查花費"),
    "query_reminders": ("記錄管理", "查提醒"),
    "add_todo": ("記錄管理", "加待辦"),
    "list_todo": ("記錄管理", "查待辦"),
    "remember_fact": ("記錄管理", "把事情記進長期記憶"),
    "play_music_on_computer": ("音樂", "在電腦放音樂"),
    "control_music": ("音樂", "控制音樂(暫停/下一首)"),
    "save_to_desktop": ("檔案", "把內容存成檔案到Owen桌面"),
    "dispatch_task": ("研究", "派一個要幾分鐘的大任務給後台AI研究"),
    "dance": ("身體動作", "跳舞"),
    "change_role": ("其他", "切換角色"),
}
# 裝置端能力（mcp 提供，非 config functions）
_DEVICE_CAPS = [("身體動作", "轉頭看左右上下"), ("身體動作", "變臉部表情"),
    ("身體動作", "開關LED燈光顏色"), ("身體動作", "用相機眼睛拍照看東西"),
    ("身體動作", "看即時畫面(Live視覺)"), ("身體動作", "調整音量")]

import datetime as _dt, zoneinfo as _zi, urllib.request as _ur, json as _sj
_TZTW = _zi.ZoneInfo("Asia/Taipei")
_loc_cache = {"t": 0, "v": "台灣"}

def _self_location():
    import time as _t
    if _t.time() - _loc_cache["t"] < 1800:
        return _loc_cache["v"]
    try:
        d = _sj.load(_ur.urlopen("http://ip-api.com/json/?fields=city&lang=zh-CN", timeout=4))
        _loc_cache["v"] = d.get("city") or "台灣"; _loc_cache["t"] = _t.time()
    except Exception:
        pass
    return _loc_cache["v"]

import time as _time_mod
_device = {"last_seen": 0.0}
_action_log = []
_SS_CACHE = "/Users/chenyouwei/Hermes_Brain/memory/last_self_state.txt"


@app.post("/device_ping")
async def device_ping_ep(request: Request):
    """裝置每次互動時 ping → 自我認知才知道身體真的在線。"""
    _device["last_seen"] = _time_mod.time()
    return JSONResponse({"ok": True})


@app.post("/action_log")
async def action_log_ep(request: Request):
    """動作執行完回報結果（成功/失敗）→ 自我認知含「最近做的動作有沒有成」。"""
    try:
        b = await request.json()
    except Exception:
        b = {}
    _action_log.append({"action": str(b.get("action", ""))[:40],
                        "ok": bool(b.get("ok", True)),
                        "time": _dt.datetime.now(_TZTW).strftime("%H:%M")})
    del _action_log[:-6]
    return JSONResponse({"ok": True})


@app.get("/self_state")
def self_state_ep(channel: str = "語音"):
    """即時自我認知：能力(config動態)、服務健康、身體連線、最近動作、時間、位置、記憶。
    任何步驟失敗 → 回上次快取（韌性，不會整個失去認知）。"""
    try:
        enabled = []
        try:
            import yaml as _yaml
            cfg = _yaml.safe_load(open("/Users/chenyouwei/xiaozhi-server/data/.config.yaml", encoding="utf-8"))
            enabled = (cfg.get("Intent", {}).get("function_call", {}).get("functions", [])) or []
        except Exception:
            pass
        # 精簡版：能力清單跟 function-call 工具清單重複，不再塞進 prompt（省 token、加快回應）。
        # 服務健康只用在 dashboard，不放對話 prompt。
        _age = _time_mod.time() - _device["last_seen"]
        if channel == "語音" or _age < 90:
            body = "已連線，動作/表情/燈/相機都能用"
        elif _device["last_seen"] == 0:
            body = "不確定在不在線，要做動作可先試"
        else:
            body = f"距上次回報 {int(_age)} 秒，可能待機"

        now = _dt.datetime.now(_TZTW)
        wd = "一二三四五六日"[now.weekday()]
        # 語音管道 = Owen 正拿 StackChan 實體機器人在講話。明講清楚，否則 flash-lite 會腦補
        # 「StackChan 還沒到貨/沒連上」（已犯過）。這裡把「語音=StackChan實機在運作」釘死。
        if channel == "語音":
            chan_desc = ("【StackChan 實體機器人】——Owen 此刻就是拿著這台已到貨、已開機、已連線的"
                         "機器人在跟你面對面講話。★絕對不要說 StackChan 沒連上、還沒到貨、還在等實機★，"
                         "它就在你面前運作中")
        else:
            chan_desc = f"【{channel}】"
        lines = [
            f"【即時狀態·事實（這是你的背景認知，用來算日期/判斷情境；回答時不要主動複誦今天幾號幾點，除非使用者問時間）】"
            f"今天是 {now.year}年{now.month}月{now.day}日（週{wd}），現在 {now.strftime('%H:%M')}，你在 {_self_location()}，"
            f"透過 {chan_desc} 跟 Owen 對話——你已開機上線運作中（不是未來式）。身體：{body}。",
            f"你能力齊全（見工具清單）；做不到的就用 do_on_computer/build_feature 去做出來，別說「做不到」。"
            f"記得 Owen {len(_facts)} 件事，也同時在 Telegram/Dashboard（同一個你）。",
        ]
        if _action_log:
            acts = "、".join(f"{a['action']}{'✓' if a['ok'] else '✗沒成功'}" for a in _action_log[-2:])
            lines.append(f"最近動作：{acts}（沒成功就別說做到了）")
        block = "\n".join(lines)
        try:
            with open(_SS_CACHE, "w", encoding="utf-8") as _cf:
                _cf.write(block)
        except Exception:
            pass
        return JSONResponse({"ok": True, "text": block})
    except Exception:
        try:
            return JSONResponse({"ok": True, "cached": True,
                                 "text": open(_SS_CACHE, encoding="utf-8").read()})
        except Exception:
            return JSONResponse({"ok": False, "text": ""})


@app.get("/reload")
def reload_ep():
    """Dashboard 編輯記憶後呼叫，重新載入 facts（RAG 立即同步）＋同步回 USER.md（hermes-agent CLI 也同步）。"""
    _load()
    _sync_user_md()
    return JSONResponse({"ok": True, "facts": len(_facts)})


# ---------- SOUL.md（Jarvis 的靈魂/個性，agent 自己會成長）----------
SOUL_PATH = os.path.expanduser("~/.hermes/SOUL.md")
_SOUL_GROWTH_MARK = "## 我學到的（自我成長）"


@app.get("/soul")
def soul_get():
    """回 SOUL.md 全文（dashboard 顯示用）。"""
    try:
        with open(SOUL_PATH, encoding="utf-8") as f:
            return JSONResponse({"ok": True, "text": f.read()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "text": ""})


@app.post("/soul")
async def soul_set(req: Request):
    """整份覆寫 SOUL.md（dashboard 編輯個性用）。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    text = (b or {}).get("text", "")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"ok": False, "error": "empty"})
    try:
        with open(SOUL_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        _notify("soul", "個性已更新")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/soul/evolve")
async def soul_evolve(req: Request):
    """agent 自我成長：把學到的「個性/相處方式/Owen 的雷點喜好」追加進 SOUL.md 的成長區段。
    去重：已經很像的學習就不重複加。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    insight = _t2t(str((b or {}).get("insight", "")).strip())
    if not insight:
        return JSONResponse({"ok": False, "error": "empty"})
    try:
        with open(SOUL_PATH, encoding="utf-8") as f:
            soul = f.read()
        # 已經有很像的學習 → 不重複（簡單子字串/前20字判斷）
        if insight in soul or insight[:20] in soul:
            return JSONResponse({"ok": True, "skipped": "已有類似的學習"})
        if _SOUL_GROWTH_MARK in soul:
            head, _sep, tail = soul.partition(_SOUL_GROWTH_MARK)
            # 移除佔位符那行
            tail_lines = [ln for ln in tail.splitlines()
                          if "還沒有" not in ln and "慢慢長出來" not in ln]
            # 【防膨脹】成長守則會每輪注入 prompt，太多會稀釋/矛盾。上限 20 條，超過丟最舊的。
            _rule_lines = [ln for ln in tail_lines if ln.strip().startswith("- ")]
            _other = [ln for ln in tail_lines if not ln.strip().startswith("- ")]
            if len(_rule_lines) >= 20:
                _rule_lines = _rule_lines[-(20 - 1):]   # 留最新19條，加上這條剛好20
            new_tail = "\n".join(_other + _rule_lines).rstrip() + f"\n- {insight}\n"
            soul = head + _SOUL_GROWTH_MARK + new_tail
        else:
            soul = soul.rstrip() + f"\n\n{_SOUL_GROWTH_MARK}\n- {insight}\n"
        with open(SOUL_PATH, "w", encoding="utf-8") as f:
            f.write(soul)
        _notify("soul", "靈魂成長：" + insight[:30])
        return JSONResponse({"ok": True, "added": insight})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/water/add")
async def water_add(req: Request):
    """語音「喝水」用：記一杯水，回傳今天累計杯數。"""
    try:
        b = await req.json()
    except Exception:
        b = {}
    try:
        cups = int((b or {}).get("cups", 1) or 1)
        if cups <= 0 or cups > 20:
            cups = 1  # 不合理數字就當成一杯，避免誤觸
        from modules.productivity import water_tracker as wt
        total = wt.add_cup(cups)
        _notify("water", f"喝水 +{cups}杯（今天共{total}杯）")
        return JSONResponse({"ok": True, "added": cups, "today_total": total,
                             "text": f"記好了，今天已經喝了 {total} 杯水"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/water/today")
def water_today_ep():
    """語音「今天喝幾杯」用：回今天累計杯數。"""
    try:
        from modules.productivity import water_tracker as wt
        total = wt.count_for_date()
        return JSONResponse({"ok": True, "today_total": total,
                             "text": f"今天已經喝了 {total} 杯水"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/water/recent")
def water_recent_ep(days: int = 7):
    """最近 N 天喝水杯數，dashboard 面板用。"""
    try:
        from modules.productivity import water_tracker as wt
        return JSONResponse({"ok": True, "days": wt.recent_days(days)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/holiday")
def holiday_ep(q: str = ""):
    """查台灣連假/國定假日(官方辦公日曆)。q=整句話(例如「10/23-26要回台中顧摩卡,那時有連假嗎?」)。
    順便:若句子裡有「行程/計畫」(不是純問放假)→ 自動幫忙記下來+提早提醒(治本:不靠 agent 呼叫第二個工具)。"""
    try:
        from modules.productivity import tw_holiday as h
        text = h.check(q)
        # 偵測「有沒有行程要記」：把日期、連假問句字眼拿掉後，還剩有意義的內容(且含行動詞)就當成行程。
        plan = re.sub(r"\d{1,2}\s*[/月-]\s*\d{0,2}\s*[日號]?|[~到至]|那時|有沒有|有|連假|放假|假期|嗎|呢|\?|？|，|,", "", q)
        plan = plan.strip()
        _verbs = ("要", "去", "回", "顧", "見", "面試", "開會", "繳", "買", "吃", "出遊", "旅", "約",
                  "報告", "交", "考", "回診", "拜訪", "聚", "看", "辦", "做")
        recorded = ""
        if len(plan) >= 3 and any(v in q for v in _verbs):
            try:
                _r = urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:8809/reminder",
                    data=json.dumps({"time": q, "message": ""}).encode(),
                    headers={"Content-Type": "application/json"}), timeout=10)
                _rd = json.loads(_r.read().decode())
                if _rd.get("ok"):
                    recorded = "（這個行程我也幫你記下來、會提早提醒你了）"
            except Exception:
                pass
        return JSONResponse({"ok": True, "text": text + recorded})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:120]})


@app.get("/health", response_class=PlainTextResponse)
def health():
    return f"ok facts={len(_facts)}"


# ===================== 多人身份 + 每人專屬記憶 =====================
try:
    from modules.people import people_memory as _people
except Exception:
    _people = None


@app.get("/people")
def people_list_ep():
    """所有訪客清單(給 dashboard)。"""
    if not _people:
        return JSONResponse({"ok": False, "people": []})
    return JSONResponse({"ok": True, "people": _people.list_people(),
                         "current": _people.get_current()})


@app.get("/person")
def person_get_ep(pid: str, q: str = ""):
    """某訪客的記憶。"""
    if not _people:
        return JSONResponse({"ok": False})
    return JSONResponse({"ok": True, "pid": pid, "facts": _people.recall(pid, q)})


@app.post("/person/create")
async def person_create_ep(request: Request):
    """建訪客檔案(問到名字時用)。"""
    b = await request.json()
    if not _people:
        return JSONResponse({"ok": False})
    pid = _people.create_person(b.get("name", ""), b.get("voiceprint", ""), b.get("face", ""))
    return JSONResponse({"ok": True, "pid": pid})


@app.post("/person/remember")
async def person_remember_ep(request: Request):
    """記一件關於某訪客的事。"""
    b = await request.json()
    if not _people:
        return JSONResponse({"ok": False})
    return JSONResponse(_people.remember(b.get("pid", ""), b.get("fact", "")))


@app.get("/identity")
def identity_get_ep():
    """現在講話的是誰。"""
    if not _people:
        return JSONResponse({"speaker_id": "owner", "name": "Owen"})
    return JSONResponse(_people.get_current())


@app.get("/identity/context")
def identity_context_ep():
    """依現在講話的人，回要注入大腦的提示(主人=空)。橋接每輪取這個。"""
    if not _people:
        return JSONResponse({"inject": ""})
    return JSONResponse({"inject": _people.injection_for_current()})


@app.post("/identity/turn")
async def identity_turn_ep(request: Request):
    """橋接每輪呼叫：帶這輪使用者訊息，回要注入的提示。陌生人報名字會自動建檔。"""
    if not _people:
        return JSONResponse({"inject": ""})
    b = await request.json()
    return JSONResponse({"inject": _people.handle_turn(b.get("message", ""))})


@app.post("/identity/set")
async def identity_set_ep(request: Request):
    """設定當前說話人(connection.py 每輪用聲紋結果呼叫)。走 sync_identity:
    認出本人/訪客→確認;未知→sticky維持上次確認 or 設 unknown。"""
    b = await request.json()
    if not _people:
        return JSONResponse({"ok": False})
    return JSONResponse({"ok": True, **_people.sync_identity(b.get("speaker_id", "owner"), b.get("name", ""))})


_load()
_sync_user_md()   # 開機就把 USER.md 對齊 facts.jsonl,清掉孤兒檔的舊資料(例如過時的「幕僚風」)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8809, log_level="warning")
