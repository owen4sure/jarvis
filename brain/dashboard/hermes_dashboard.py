"""
Hermes Dashboard — 觀測並編輯 Hermes 的一切。
記憶 / 功能 / 聊天記錄 / 進化 / 提醒 / 待辦 / 記帳 / 系統狀態。
啟動：python hermes_dashboard.py（port 8811）。
"""
import asyncio
import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
import yaml

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (JSONResponse, HTMLResponse, PlainTextResponse,
                               FileResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

HOME = os.path.expanduser("~")
HB = os.path.join(HOME, "Hermes_Brain")
FACTS = os.path.join(HOME, ".hermes/memories/facts.jsonl")
REMINDERS = os.path.join(HB, "config/reminders.json")
EXPENSES = os.path.join(HB, "config/expenses.json")
CHECKLISTS = os.path.join(HB, "config/checklists.json")
CHAT_LOG = os.path.join(HOME, ".hermes/memories/chat_history.jsonl")
CONFIG_YAML = os.path.join(HOME, "xiaozhi-server/data/.config.yaml")
TZ = zoneinfo.ZoneInfo("Asia/Taipei")
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Hermes Dashboard")

# ---------- 即時推播（SSE）：StackChan 一有動靜，所有開著的 dashboard 立刻反應 ----------
_subscribers = set()  # set[asyncio.Queue]


def _broadcast(event_type, data):
    """把事件推給所有連線中的 dashboard 瀏覽器。"""
    payload = {"type": event_type, "data": data,
               "ts": datetime.datetime.now(TZ).strftime("%H:%M:%S")}
    dead = []
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


@app.get("/api/stream")
async def stream(request: Request):
    """瀏覽器用 EventSource 連這裡，即時收到 chat/action/state 事件。"""
    q = asyncio.Queue(maxsize=64)
    _subscribers.add(q)

    async def gen():
        try:
            yield "data: " + json.dumps({"type": "hello"}) + "\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # 心跳保持連線
        finally:
            _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/keys")
def api_keys():
    """每把 Gemini 金鑰的詳細狀態（代理 8808）。"""
    try:
        return JSONResponse(json.load(urllib.request.urlopen(
            "http://127.0.0.1:8808/admin/keys", timeout=4)))
    except Exception:
        return JSONResponse({"keys": [], "total": 0})


@app.get("/api/model")
def api_model_get():
    """目前對話模型 + 可選清單（代理 8808）。"""
    try:
        return JSONResponse(json.load(urllib.request.urlopen(
            "http://127.0.0.1:8808/admin/model", timeout=4)))
    except Exception:
        return JSONResponse({"current": "?", "available": []})


@app.post("/api/model")
async def api_model_set(req: Request):
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8808/admin/model",
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"})
        return JSONResponse(json.loads(await _aopen(r, 5)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/soul")
def api_soul_get():
    """代理 8809 的 SOUL.md（Jarvis 個性/靈魂）。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/soul", timeout=5))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "text": ""})


@app.get("/api/email/pending")
def api_email_pending():
    """待寄匣（給確認卡片顯示）。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/email/pending", timeout=5))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "pending": []})


@app.post("/api/email/draft")
async def api_email_draft(req: Request):
    """編輯後重新擬稿（代理 8809）。仍然只是進待寄匣、不會寄。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/email/draft",
                                   data=json.dumps(b or {}).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
        return JSONResponse(json.load(urllib.request.urlopen(r, timeout=10)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/email/update")
async def api_email_update(req: Request):
    """卡片內就地編輯後儲存（代理 8809，不寄）。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/email/update",
                                   data=json.dumps(b or {}).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
        return JSONResponse(json.load(urllib.request.urlopen(r, timeout=10)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/email/send")
async def api_email_send(req: Request):
    """Owen 親手按「寄出」→ 才真的寄（代理 8809）。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/email/send",
                                   data=json.dumps({"id": (b or {}).get("id", "")}).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
        return JSONResponse(json.load(urllib.request.urlopen(r, timeout=35)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/email/cancel")
async def api_email_cancel(req: Request):
    """按「取消」→ 丟掉草稿、不寄（代理 8809）。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/email/cancel",
                                   data=json.dumps({"id": (b or {}).get("id", "")}).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
        return JSONResponse(json.load(urllib.request.urlopen(r, timeout=10)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/soul")
async def api_soul_set(req: Request):
    """編輯 Jarvis 個性 → 寫回 SOUL.md（8809）。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/soul",
                                   data=json.dumps({"text": (b or {}).get("text", "")}).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
        return JSONResponse(json.load(urllib.request.urlopen(r, timeout=5)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/pending")
def api_pending():
    """代理 8809 的待審批任務（前端用）。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/pending_tasks", timeout=5))
        return JSONResponse(d)
    except Exception:
        return JSONResponse({"ok": False, "pending": []})


@app.post("/api/approve")
async def api_approve(req: Request):
    """使用者按確認/拒絕 → 轉給 8809 執行。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/approve_task",
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.loads(await _aopen(r, 8))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/finance")
def api_finance():
    """代理 8809 理財總覽（含即時股價）。timeout 拉長因為要抓 Yahoo 報價。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/finance", timeout=15))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/expenses_range")
def api_expenses_range(start: str = "", end: str = ""):
    """代理 8809 花費區間明細查詢（記帳頁選日期範圍 → 看單筆消費明細＋總額）。"""
    try:
        q = urllib.parse.urlencode({"start": start, "end": end})
        d = json.load(urllib.request.urlopen(
            "http://127.0.0.1:8809/expenses_range?" + q, timeout=8))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/people")
def api_people():
    """代理 8809 認識的人(訪客)清單 + 現在講話的是誰。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/people", timeout=6))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "people": []})


@app.get("/api/person")
def api_person(pid: str = ""):
    """代理某訪客的記憶明細。"""
    try:
        d = json.load(urllib.request.urlopen(
            "http://127.0.0.1:8809/person?pid=" + urllib.parse.quote(pid), timeout=6))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "facts": []})


@app.get("/api/finance/analysis")
def api_finance_analysis():
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/finance/analysis", timeout=10))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/finance/stock_ai")
def api_finance_stock_ai():
    """AI 投資分析（每檔訊號 + 組合評估）。timeout 拉長因為要跑 Gemini。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/finance/stock_ai", timeout=75))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/finance/stock_news")
def api_finance_stock_news():
    """持股最新新聞（Gemini 接地搜尋）。timeout 長因為要搜尋。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/finance/stock_news", timeout=90))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/expense")
async def api_expense(req: Request):
    """快速記帳 → 轉 8809 /expense（會自動簡轉繁 + 預算提醒）。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/expense",
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.loads(await _aopen(r, 8))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/water")
def api_water():
    """代理 8809 最近 7 天喝水紀錄，dashboard 喝水面板用。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/water/recent?days=7", timeout=6))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "days": []})


@app.post("/api/water/add")
async def api_water_add(req: Request):
    """Dashboard 上手動點「+1杯」→ 轉 8809 /water/add。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/water/add",
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.loads(await _aopen(r, 6))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/finance/category_budget")
async def api_finance_catbudget(req: Request):
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/finance/category_budget",
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.loads(await _aopen(r, 8))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/finance/history")
def api_finance_history():
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/finance/history", timeout=8))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "history": []})


@app.get("/api/goal")
def api_goal():
    """資產目標(預設400萬/30歲)代理 → 走勢圖顯示目標進度條用。"""
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8809/goal", timeout=8))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.put("/api/finance/{key}")
async def api_finance_put(key: str, req: Request):
    """整批覆寫 income/fixed/holdings/expenses → 轉給 8809。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/finance/" + key,
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"},
                                   method="PUT")
        d = json.loads(await _aopen(r, 10))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/finance/op")
async def api_finance_op(req: Request):
    """單項即時改財務（cash/fire_target 等）→ 轉 8809 /finance/op。"""
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/finance/op",
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.loads(await _aopen(r, 10))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/finance/budget")
async def api_finance_budget(req: Request):
    b = await _safe_body(req)
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/finance/budget",
                                   data=json.dumps(b).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.loads(await _aopen(r, 10))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/event")
async def push_event(req: Request):
    """xiaozhi / 任何後台元件 POST 活動事件到這裡 → 即時廣播給 dashboard。
    例：{"type":"listening"} {"type":"thinking"} {"type":"action","data":"轉頭"}"""
    b = await _safe_body(req)
    _broadcast(b.get("type", "ping"), b.get("data"))
    return JSONResponse({"ok": True})


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


async def _safe_body(req):
    """安全解析 request body：壞 JSON / 空 body 回 {}，不讓端點 500 噴 traceback。"""
    try:
        raw = await req.body()
        if not raw:
            return {}
        return json.loads(raw)
    except Exception:
        return {}


async def _aopen(req, timeout=8):
    """在 executor 跑阻塞的 urllib，回 bytes。async 端點用這個，8809/8808 慢時才不會凍住
    整個事件迴圈（SSE、其他請求全卡）→ 這就是 dashboard 偶爾「跑不動」的根因。"""
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: urllib.request.urlopen(req, timeout=timeout).read())


def _write_json(path, data):
    """原子寫：先寫 .tmp 再 os.replace，避免寫到一半 crash / 並發寫把 JSON 截斷壞掉。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_facts():
    out = []
    try:
        with open(FACTS, encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if line:
                    try:
                        d = json.loads(line)
                        out.append({"id": i, "text": d.get("text", ""),
                                    "has_emb": bool(d.get("emb"))})
                    except Exception:
                        pass
    except Exception:
        pass
    return out


import fcntl as _fcntl
import glob as _glob
import shutil as _shutil

_FACTS_LOCK = os.path.join(os.path.dirname(FACTS), ".facts.lock")


def _read_facts_raw():
    """讀現在磁碟上的 facts（含 emb），回 list[(text, emb)]。"""
    out = []
    try:
        with open(FACTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    out.append((d.get("text", ""), d.get("emb")))
    except Exception:
        pass
    return out


def _backup_facts():
    """備份目前 facts（微秒戳避免同秒覆蓋，只留最近 8 份）。"""
    try:
        if not os.path.exists(FACTS):
            return
        bdir = os.path.join(os.path.dirname(FACTS), "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = datetime.datetime.now(TZ).strftime("%Y%m%d_%H%M%S_%f")
        _shutil.copy(FACTS, os.path.join(bdir, f"facts_{stamp}.jsonl"))
        olds = sorted(_glob.glob(os.path.join(bdir, "facts_*.jsonl")))
        for old in olds[:-8]:
            try:
                os.remove(old)
            except Exception:
                pass
    except Exception:
        pass


def _save_facts(facts_texts, force=False):
    """安全重寫 facts.jsonl。回 (ok, msg, count)。
    防呆：若新清單會刪掉「超過 3 筆且超過 30%」的記憶，除非 force 否則拒絕（防 DOM 抓漏誤刪）。
    用檔案鎖序列化 read-modify-write，避免和 8809/POST 競爭。"""
    os.makedirs(os.path.dirname(FACTS), exist_ok=True)
    # 清理空白、去重（保序）
    seen, clean = set(), []
    for t in facts_texts:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            clean.append(t)
    lockf = open(_FACTS_LOCK, "w")
    try:
        _fcntl.flock(lockf, _fcntl.LOCK_EX)
        current = _read_facts_raw()
        existing = {t: e for t, e in current}
        cur_n = len(current)
        # 防呆用「筆數」判斷（編輯會改文字但筆數不變；誤刪/抓漏會讓筆數驟降）。
        # 用 OR：掉超過 3 筆 或 掉超過 30% 都擋；且「原本有資料卻清成空」一律擋。
        if not force and cur_n > 0:
            emptied = len(clean) == 0
            big_drop = (cur_n - len(clean) > 3) or (len(clean) < cur_n * 0.7)
            if emptied or big_drop:
                return (False,
                        f"拒絕：記憶會從 {cur_n} 筆掉到 {len(clean)} 筆，疑似誤刪或抓漏。確認無誤請用 force。",
                        cur_n)
        _backup_facts()
        tmp = FACTS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for t in clean:
                emb = existing.get(t)
                if emb is None:
                    emb = _embed(t)
                f.write(json.dumps({"text": t, "emb": emb}, ensure_ascii=False) + "\n")
        os.replace(tmp, FACTS)  # 原子置換
        return (True, "ok", len(clean))
    finally:
        try:
            _fcntl.flock(lockf, _fcntl.LOCK_UN)
            lockf.close()
        except Exception:
            pass


def _embed(text):
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/embeddings",
            data=json.dumps({"model": "nomic-embed-text", "prompt": text}).encode(),
            headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=20)).get("embedding")
    except Exception:
        return None


def _reload_memory():
    """通知記憶端點(8809)重新載入 facts。"""
    try:
        urllib.request.urlopen("http://127.0.0.1:8809/reload", timeout=5)
    except Exception:
        pass


# ---------- 記憶 ----------
@app.get("/api/memory")
def get_memory():
    return JSONResponse({"facts": _load_facts()})


@app.post("/api/memory")
async def add_memory(req: Request):
    b = await _safe_body(req)
    text = (b or {}).get("text", "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty"})
    # 用鎖 append（避免和整批重寫競爭）；embedding 放執行緒避免卡事件迴圈
    emb = await asyncio.get_event_loop().run_in_executor(None, _embed, text)
    lockf = open(_FACTS_LOCK, "w")
    try:
        _fcntl.flock(lockf, _fcntl.LOCK_EX)
        os.makedirs(os.path.dirname(FACTS), exist_ok=True)
        with open(FACTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text, "emb": emb}, ensure_ascii=False) + "\n")
    finally:
        try:
            _fcntl.flock(lockf, _fcntl.LOCK_UN); lockf.close()
        except Exception:
            pass
    _reload_memory()
    return JSONResponse({"ok": True})


@app.put("/api/memory")
async def edit_memory(req: Request):
    """整批更新（前端傳回完整 text 列表）。有防呆，疑似誤刪會擋下來。"""
    b = await _safe_body(req)
    texts = b.get("texts", [])
    force = bool(b.get("force"))
    # embedding 可能阻塞 → 整個存檔丟執行緒，不卡事件迴圈
    ok, msg, count = await asyncio.get_event_loop().run_in_executor(
        None, _save_facts, texts, force)
    if ok:
        _reload_memory()
        return JSONResponse({"ok": True, "count": count})
    return JSONResponse({"ok": False, "error": msg, "current": count, "need_force": True})


# ---------- 結構化記憶（分區 markdown 文件）----------
MEMORY_DOC = os.path.join(os.path.dirname(FACTS), "memory_doc.md")
_DEFAULT_SECTIONS = ["👤 個人資料", "💼 工作 & 財務", "🎨 偏好 & 習慣",
                     "👥 人際關係", "🤖 對 Jarvis 的期待", "📌 其他"]


def _parse_doc(doc):
    """從 markdown 文件抽出每一條記憶（非標題、非空的行；去掉 -•* 項目符號）。"""
    facts = []
    for line in (doc or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = s.lstrip("-•*　 ").strip()
        if s:
            facts.append(s)
    return facts


def _facts_to_doc(facts):
    """沒有 doc 檔時，用現有 facts 生成一份預設分區文件（全部先放『其他』，使用者再自行歸類）。"""
    lines = []
    for sec in _DEFAULT_SECTIONS:
        lines.append("## " + sec)
        if sec.startswith("📌") and facts:
            for f in facts:
                lines.append("- " + f)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


@app.get("/api/memory/doc")
def get_memory_doc():
    """回結構化記憶文件（markdown）。沒有檔就用現有 facts 生成預設分區。"""
    try:
        if os.path.exists(MEMORY_DOC):
            with open(MEMORY_DOC, encoding="utf-8") as f:
                return JSONResponse({"ok": True, "doc": f.read()})
        facts = [x["text"] for x in _load_facts()]
        return JSONResponse({"ok": True, "doc": _facts_to_doc(facts)})
    except Exception as e:
        print(f"⚠️ [memory/doc GET] {e}")
        return JSONResponse({"ok": False, "doc": ""})


@app.put("/api/memory/doc")
async def put_memory_doc(req: Request):
    """存結構化記憶：存 markdown 原文 + 拆成 facts 重算向量（給 AI 語意搜尋）。"""
    b = await _safe_body(req)
    doc = b.get("doc", "")
    force = bool(b.get("force"))
    facts = _parse_doc(doc)
    # 系統開發筆記存在 system_notes.md（不在 memory_doc.md）。重建 facts.jsonl 時要把它們
    # 一起放回去，否則「dashboard 編輯個人記憶→自動存」會把系統筆記從 RAG 搜尋庫靜默清掉。
    _SYS_NOTES = os.path.join(os.path.dirname(FACTS), "system_notes.md")
    if os.path.exists(_SYS_NOTES):
        try:
            with open(_SYS_NOTES, encoding="utf-8") as f:
                facts = facts + _parse_doc(f.read())
        except Exception as e:
            print(f"⚠️ [memory/doc PUT] 讀系統筆記失敗: {e}")
    ok, msg, count = await asyncio.get_event_loop().run_in_executor(
        None, _save_facts, facts, force)
    if not ok:
        return JSONResponse({"ok": False, "error": msg, "current": count, "need_force": True})
    try:
        with open(MEMORY_DOC, "w", encoding="utf-8") as f:
            f.write(doc if doc.endswith("\n") else doc + "\n")
    except Exception as e:
        print(f"⚠️ [memory/doc PUT] {e}")
    _reload_memory()
    return JSONResponse({"ok": True, "count": count})


# ---------- 提醒 ----------
@app.get("/api/reminders")
def get_reminders():
    d = _read_json(REMINDERS, {"reminders": []})
    return JSONResponse({"reminders": d.get("reminders", [])})


@app.post("/api/reminder")
async def add_reminder_nl(req: Request):
    """從 dashboard 用自然語言新增提醒 → 轉 8809 /reminder（會解析時間）。"""
    b = await _safe_body(req)
    text = (b.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "text": "請輸入要提醒的事"})
    try:
        r = urllib.request.Request("http://127.0.0.1:8809/reminder",
                                   data=json.dumps({"time": text, "message": ""}).encode(),
                                   headers={"Content-Type": "application/json"})
        d = json.loads(await _aopen(r, 8))
        return JSONResponse(d)
    except Exception as e:
        return JSONResponse({"ok": False, "text": "新增提醒時出了點狀況，再試一次"})


@app.put("/api/reminders")
async def put_reminders(req: Request):
    b = await _safe_body(req)
    d = _read_json(REMINDERS, {"reminders": [], "next_id": 1})
    d["reminders"] = (b or {}).get("reminders", [])
    _write_json(REMINDERS, d)
    return JSONResponse({"ok": True})


# ---------- 客製化指令（說 X → 做 Y 的硬規則，命中直接執行不靠 LLM）----------
CUSTOM_CMDS = os.path.join(HB, "config/custom_commands.json")


def _read_cmds():
    d = _read_json(CUSTOM_CMDS, {"commands": [], "next_id": 1})
    if not isinstance(d, dict):
        d = {"commands": [], "next_id": 1}
    d.setdefault("commands", [])
    d.setdefault("next_id", 1)
    return d


@app.get("/api/custom_commands")
def get_custom_commands():
    return JSONResponse(_read_cmds())


@app.post("/api/custom_commands")
async def add_custom_command(req: Request):
    b = await _safe_body(req)
    trigger = str((b or {}).get("trigger", "")).strip()
    query = str((b or {}).get("query", "")).strip()
    if not trigger:
        return JSONResponse({"ok": False, "text": "請填觸發詞（你要說的那句話）"})
    if not query:
        return JSONResponse({"ok": False, "text": "請填要播放的音樂（描述或歌名）"})
    d = _read_cmds()
    # 不可變更新：建新清單，不就地改原物件
    new_cmd = {
        "id": d["next_id"],
        "trigger": trigger,
        "action": "play_music",
        "params": {"query": query},
        "note": str((b or {}).get("note", "")).strip(),
        "reply": str((b or {}).get("reply", "")).strip()
                 or f"好，幫你放「{query}」～",
    }
    d = {"commands": d["commands"] + [new_cmd], "next_id": d["next_id"] + 1}
    _write_json(CUSTOM_CMDS, d)
    return JSONResponse({"ok": True, "command": new_cmd})


@app.put("/api/custom_commands/{cid}")
async def edit_custom_command(cid: int, req: Request):
    b = await _safe_body(req)
    d = _read_cmds()
    found = False
    new_list = []
    for c in d["commands"]:
        if c.get("id") == cid:
            found = True
            trigger = str((b or {}).get("trigger", c.get("trigger", ""))).strip()
            query = str((b or {}).get("query",
                        (c.get("params") or {}).get("query", ""))).strip()
            new_list.append({
                "id": cid,
                "trigger": trigger or c.get("trigger", ""),
                "action": "play_music",
                "params": {"query": query},
                "note": str((b or {}).get("note", c.get("note", ""))).strip(),
                "reply": str((b or {}).get("reply", c.get("reply", ""))).strip()
                         or f"好，幫你放「{query}」～",
            })
        else:
            new_list.append(c)
    if not found:
        return JSONResponse({"ok": False, "text": "找不到這條指令"})
    _write_json(CUSTOM_CMDS, {"commands": new_list, "next_id": d["next_id"]})
    return JSONResponse({"ok": True})


@app.delete("/api/custom_commands/{cid}")
def delete_custom_command(cid: int):
    d = _read_cmds()
    new_list = [c for c in d["commands"] if c.get("id") != cid]
    _write_json(CUSTOM_CMDS, {"commands": new_list, "next_id": d["next_id"]})
    return JSONResponse({"ok": True})


# ---------- 待辦 ----------
@app.get("/api/todos")
def get_todos():
    d = _read_json(CHECKLISTS, {})
    items = []
    if isinstance(d, dict):
        for lst in d.values():
            if isinstance(lst, list):
                items += [x.get("item", x) if isinstance(x, dict) else x for x in lst]
    return JSONResponse({"todos": items})


@app.put("/api/todos")
async def put_todos(req: Request):
    b = await _safe_body(req)
    items = (b or {}).get("todos", [])
    d = _read_json(CHECKLISTS, {})
    if not isinstance(d, dict):
        d = {}
    # 保留既有 done 狀態（依 item 文字對應），只改 "out" 這個清單，不動其他清單
    done_map = {}
    for x in (d.get("out") or []):
        if isinstance(x, dict):
            done_map[x.get("item")] = x.get("done", False)
    d["out"] = [{"item": i, "done": done_map.get(i, False)} for i in items]
    _write_json(CHECKLISTS, d)
    return JSONResponse({"ok": True})


# ---------- 記帳 ----------
@app.get("/api/expenses")
def get_expenses():
    d = _read_json(EXPENSES, {"expenses": []})
    exp = d.get("expenses", []) if isinstance(d, dict) else d
    total = sum(e.get("amount", 0) for e in exp)
    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    today_total = sum(e.get("amount", 0) for e in exp if e.get("date") == today)
    return JSONResponse({"expenses": exp, "total": total, "today_total": today_total,
                         "budget": d.get("monthly_budget") if isinstance(d, dict) else None})


# ---------- 功能/工具 ----------
def _list_tools():
    """用 yaml 正確讀出啟用的工具清單。"""
    try:
        with open(CONFIG_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        fns = cfg.get("Intent", {}).get("function_call", {}).get("functions", [])
        return [str(t) for t in fns] if isinstance(fns, list) else []
    except Exception:
        return []


@app.get("/api/functions")
def get_functions():
    tools = _list_tools()
    desc = {
        "change_role": "切換角色", "get_weather_free": "查天氣(免費)",
        "play_music_on_computer": "電腦放音樂", "remember_fact": "記住事情",
        "control_music": "控制音樂", "set_reminder": "設提醒", "add_expense": "記帳",
        "query_reminders": "查提醒", "query_expenses": "查花費", "set_timer": "計時器",
        "get_news": "查新聞", "add_todo": "加待辦", "list_todo": "查待辦",
        "convert_currency": "匯率換算", "dispatch_task": "派任務給後台AI",
        "dance": "跳舞", "handle_exit_intent": "結束對話",
        "save_to_desktop": "存檔到桌面", "find_nearby": "找附近地點",
    }
    devtools = ["set_head_angles(轉頭)", "set_avatar(表情)", "led(燈光)",
                "take_photo(拍照)", "set_volume(音量)", "set_blink(眨眼)", "live(即時視覺)"]
    return JSONResponse({
        "tools": [{"name": t, "desc": desc.get(t, t)} for t in tools],
        "device_tools": devtools,
    })


# ---------- 聊天記錄 ----------
@app.get("/api/chat")
def get_chat():
    out = []
    try:
        with open(CHAT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return JSONResponse({"chats": out[-100:][::-1]})


def _t2t(s):
    """簡體 → 繁體（台灣慣用「台」不轉「臺」）。所有紀錄都用繁體。"""
    if not isinstance(s, str) or not s:
        return s
    try:
        from zhconv import convert
        return convert(s, "zh-tw").replace("臺", "台")
    except Exception:
        return s


@app.post("/api/chat/log")
async def log_chat(req: Request):
    """StackChan / Telegram 對話時呼叫，記錄一輪。只保留近 7 天（最多 500 筆）。"""
    b = await _safe_body(req)
    rec = {
        "time": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "source": (b or {}).get("source") or "StackChan",
        "user": _t2t((b or {}).get("user", "")),
        "assistant": _t2t((b or {}).get("assistant", "")),
    }
    os.makedirs(os.path.dirname(CHAT_LOG), exist_ok=True)
    # 讀現有 + 加新 + 砍掉 7 天前的、最多留 500 筆 → 整批重寫（不會無限長大）
    cutoff = (datetime.datetime.now(TZ) - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    recs = []
    try:
        with open(CHAT_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    recs.append(rec)
    recs = [r for r in recs if str(r.get("time", "")) >= cutoff][-500:]
    tmp = CHAT_LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, CHAT_LOG)
    _broadcast("chat", rec)   # 即時推給 dashboard → 講話它馬上出現
    return JSONResponse({"ok": True})


# ---------- Live 視覺（看 StackChan 鏡頭即時畫面）----------
CAM_FILE = os.path.join(HOME, "xiaozhi-server/data/last_camera.jpg")
LIVE_FLAG = os.path.join(HOME, "xiaozhi-server/data/live_mode.flag")


@app.get("/api/camera")
def camera():
    if os.path.exists(CAM_FILE):
        return FileResponse(CAM_FILE, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})
    return Response(status_code=204)


@app.post("/api/live")
async def set_live(req: Request):
    b = await _safe_body(req)
    on = bool(b.get("on"))
    try:
        if on:
            with open(LIVE_FLAG, "w") as f:
                f.write(datetime.datetime.now(TZ).isoformat())
        elif os.path.exists(LIVE_FLAG):
            os.remove(LIVE_FLAG)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return JSONResponse({"ok": True, "live": on})


@app.get("/api/live")
def get_live():
    return JSONResponse({"live": os.path.exists(LIVE_FLAG)})


# ---------- 存檔到桌面（StackChan 把內容寫成檔案）----------
DESKTOP = os.path.join(HOME, "Desktop")


@app.post("/api/save_file")
async def save_file(req: Request):
    """StackChan 呼叫：把內容存成檔案放到桌面。"""
    b = await _safe_body(req)
    name = (b.get("filename") or "hermes_note").strip()
    content = b.get("content") or ""
    # 安全：限制內容大小（避免被灌爆磁碟）
    if not isinstance(content, str) or len(content) > 2_000_000:
        return JSONResponse({"ok": False, "error": "內容太大或格式不對"})
    # 安全：只允許檔名（去掉路徑），去掉開頭的點（避免寫成隱藏檔/.bashrc 等），預設 .txt
    name = os.path.basename(name).replace("/", "_").replace("\\", "_").lstrip(".").strip()
    if not name:
        name = "hermes_note"
    if "." not in name:
        name += ".txt"
    # 避免覆蓋：同名加時間
    target = os.path.join(DESKTOP, name)
    if os.path.exists(target):
        stem, ext = os.path.splitext(name)
        target = os.path.join(DESKTOP, f"{stem}_{datetime.datetime.now(TZ).strftime('%H%M%S')}{ext}")
    try:
        os.makedirs(DESKTOP, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return JSONResponse({"ok": True, "path": target, "name": os.path.basename(target)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------- 進化/統計 ----------
# ========== hermes-agent 大腦觀測（單一大腦的後端細節都從這來）==========
import sqlite3 as _sqlite3
import socket as _socket

HERMES_HOME = os.path.expanduser("~/.hermes")
HERMES_STATE_DB = os.path.join(HERMES_HOME, "state.db")
HERMES_SKILLS = os.path.join(HERMES_HOME, "skills")
HERMES_CONFIG = os.path.join(HERMES_HOME, "config.yaml")


def _hermes_config():
    try:
        with open(HERMES_CONFIG, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _hermes_env():
    """讀 ~/.hermes/.env（MCP token 等密鑰在這），給 ${ENV} 變數解析用。"""
    env = dict(os.environ)
    try:
        with open(os.path.join(HERMES_HOME, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
    return env


def _hermes_mcp_servers():
    """讀 hermes-agent 掛了哪些 MCP server（工具怎麼接的真相）。"""
    cfg = _hermes_config()
    out = []
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        if not isinstance(spec, dict):
            continue
        # 解析 headers 裡的 ${ENV} 變數（例如 Authorization 的 token）→ 從 ~/.hermes/.env
        _env = _hermes_env()
        hdrs = {}
        for k, v in (spec.get("headers") or {}).items():
            if isinstance(v, str) and "${" in v:
                import re as _re
                v = _re.sub(r"\$\{([^}]+)\}", lambda m: _env.get(m.group(1), ""), v)
            hdrs[k] = v
        out.append({"name": name, "url": spec.get("url", ""),
                    "enabled": spec.get("enabled", True), "headers": hdrs})
    return out


def _hermes_skills():
    """列 hermes-agent 的技能（含它自動長出來的 → 「成長」的證據）。"""
    out = []
    try:
        for root, _dirs, files in os.walk(HERMES_SKILLS):
            for fn in files:
                if fn.upper() in ("SKILL.MD", "SKILL.YAML", "SKILL.YML"):
                    p = os.path.join(root, fn)
                    name = os.path.basename(os.path.dirname(p))
                    desc = ""
                    try:
                        with open(p, encoding="utf-8") as f:
                            head = f.read(400)
                        for line in head.splitlines():
                            s = line.strip().lstrip("#").strip()
                            if s and not s.startswith("---") and "name:" not in s:
                                desc = s[:80]
                                break
                    except Exception:
                        pass
                    out.append({"name": name, "desc": desc,
                                "path": p.replace(os.path.expanduser("~"), "~"),
                                "mtime": os.path.getmtime(p)})
    except Exception:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _hermes_sessions(limit=20):
    """從 state.db 撈最近對話 session（語音/Telegram/CLI 統一紀錄）。"""
    out = []
    try:
        con = _sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True, timeout=3)
        con.row_factory = _sqlite3.Row
        cur = con.execute("SELECT * FROM sessions ORDER BY rowid DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            d = {k: r[k] for k in cols}
            row = {k: d.get(k) for k in d
                   if k in ("id", "source", "name", "title", "platform",
                            "started_at", "updated_at", "message_count", "summary")}
            # 從 id 前綴推來源管道（voice-owen=語音/StackChan, telegram=Telegram）
            _sid = str(d.get("id", ""))
            if _sid.startswith("voice-owen"):
                row["channel"] = "🎤 語音/StackChan"
            elif "telegram" in _sid.lower() or d.get("source") == "telegram":
                row["channel"] = "✈️ Telegram"
            else:
                row["channel"] = d.get("source") or "其他"
            out.append(row)
        con.close()
    except Exception as e:
        return [{"error": str(e)[:60]}]
    return out


def _hermes_counts():
    c = {"sessions": 0, "messages": 0}
    try:
        con = _sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True, timeout=3)
        c["sessions"] = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        c["messages"] = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        con.close()
    except Exception:
        pass
    return c


def _port_alive(port):
    try:
        with _socket.create_connection(("127.0.0.1", int(port)), timeout=0.4):
            return True
    except Exception:
        return False


@app.get("/api/evolution")
def get_evolution():
    """進化/成長總覽：記憶、學到的規則、對話量、技能（含自動長出的）、session 統計。"""
    facts = _load_facts()
    rules = [f for f in facts if any(k in f["text"] for k in ("當", "就代表", "規則", "=", "就要"))]
    chats = 0
    try:
        with open(CHAT_LOG, encoding="utf-8") as f:
            chats = sum(1 for _ in f)
    except Exception:
        pass
    skills = _hermes_skills()
    counts = _hermes_counts()
    return JSONResponse({
        "fact_count": len(facts),
        "learned_rules": [r["text"] for r in rules],
        "chat_count": chats,
        "tools_count": len(_list_tools()),
        "skill_count": len(skills),
        "skills_recent": skills[:12],
        "hermes_sessions": counts["sessions"],
        "hermes_messages": counts["messages"],
        "brain": "hermes-agent" if _hermes_mcp_servers() else "gemini-direct",
    })


@app.get("/api/architecture")
def get_architecture():
    """整個系統怎麼接的（架構真相），每節點含 port/角色/連到哪/後端檔案，可點進去看。"""
    mcp = _hermes_mcp_servers()
    nodes = [
        {"id": "device", "label": "StackChan 機器人", "role": "硬體（耳朵/嘴巴/臉）",
         "detail": "ESP32-S3+韌體，WebSocket+Opus 連 xiaozhi", "port": "—", "file": "esp/stackchan-fw"},
        {"id": "telegram", "label": "Telegram", "role": "文字管道（手機隨時聊）",
         "detail": "@owenstackchanbot，直接連 hermes-agent gateway，跟語音同一個大腦", "port": "—",
         "file": "~/.hermes (gateway)"},
        {"id": "xiaozhi", "label": "xiaozhi-server", "role": "語音 I/O（ASR/TTS/VAD/表情）",
         "detail": "Docker，語音轉文字交給大腦，再把回覆唸出來", "port": "8000", "file": "xiaozhi-server/patches/"},
        {"id": "bridge", "label": "語音橋接", "role": "注入固定 session（連續對話）",
         "detail": "xiaozhi→這→hermes-agent，給穩定 session id 讓對話連續、延遲低", "port": "8643",
         "file": "scripts/voice_brain_bridge.py"},
        {"id": "brain", "label": "hermes-agent", "role": "大腦（推理/工具/技能/進化/學習）",
         "detail": "OpenAI 相容 API（8642），語音經橋接走這", "port": "8642", "file": "~/.hermes/hermes-agent"},
        {"id": "proxy", "label": "LLM Proxy", "role": "Gemini 金鑰輪換+多模型後備",
         "detail": "大腦的底層模型呼叫走這，過載自動換", "port": "8808", "file": "scripts/llm_proxy.py"},
        {"id": "devtool", "label": "裝置控制通道", "role": "大腦→機器人(轉頭/表情/音量)",
         "detail": "hermes-life robot_* 工具→xiaozhi 8003 /mcp/device_tool→裝置 MCP session（韌體 force 直連後 8767 不再使用）", "port": "8003", "file": "xiaozhi-server/patches/http_server.py"},
        {"id": "mcp_life", "label": "hermes-life MCP", "role": "生活+裝置工具",
         "detail": "財務/記帳/提醒/音樂/天氣/機器人控制，轉呼叫8809/8810/8003", "port": "8769", "file": "scripts/hermes_life_mcp.py"},
        {"id": "mem", "label": "記憶服務", "role": "統一記憶+財務/提醒後端",
         "detail": "facts.jsonl 單一真相，USER.md 自動同步給 CLI", "port": "8809", "file": "scripts/hermes_memory_endpoint.py"},
        {"id": "music", "label": "音樂服務", "role": "Chrome YouTube 播放",
         "detail": "放歌/切歌/暫停/停止", "port": "8810", "file": "scripts/music_service.py"},
        {"id": "dash", "label": "Dashboard（你在這）", "role": "超級大本營",
         "detail": "觀測+編輯記憶/工具/進化/對話", "port": "8811", "file": "dashboard/hermes_dashboard.py"},
    ]
    edges = [
        {"from": "device", "to": "xiaozhi", "label": "語音"},
        {"from": "xiaozhi", "to": "bridge", "label": "文字+session"},
        {"from": "bridge", "to": "brain", "label": "→大腦"},
        {"from": "telegram", "to": "brain", "label": "文字→大腦"},
        {"from": "brain", "to": "proxy", "label": "底層模型"},
        {"from": "brain", "to": "devtool", "label": "裝置工具"},
        {"from": "devtool", "to": "xiaozhi", "label": "device MCP"},
        {"from": "brain", "to": "mcp_life", "label": "生活工具"},
        {"from": "mcp_life", "to": "mem", "label": "財務/提醒"},
        {"from": "mcp_life", "to": "music", "label": "音樂"},
        {"from": "brain", "to": "mem", "label": "記憶讀寫"},
        {"from": "dash", "to": "mem", "label": "看/改記憶"},
        {"from": "dash", "to": "brain", "label": "看進化"},
    ]
    health = {n["id"]: (_port_alive(n["port"]) if n["port"].isdigit() else None) for n in nodes}
    # Telegram 沒有 port → 改判：.env 有 token 且 gateway(8642)活 = 接上了
    try:
        _env = _hermes_env()
        _tg_on = bool(_env.get("TELEGRAM_BOT_TOKEN")) and _port_alive("8642")
        health["telegram"] = _tg_on
    except Exception:
        health["telegram"] = None
    # device：xiaozhi(8000)活著就當機器人那條通道在
    health["device"] = _port_alive("8000")
    return JSONResponse({"nodes": nodes, "edges": edges, "health": health, "mcp_servers": mcp})


_TOOLS_CACHE = {"ts": 0, "data": None}


def _mcp_parse_sse(raw):
    """從 streamable-http 回應（可能是 SSE）撈出第一個 JSON-RPC 結果。"""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


def _mcp_list_tools(url, headers=None):
    """正規 MCP streamable-http handshake：initialize → tools/list。回工具清單。"""
    H = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    H.update(headers or {})
    try:
        # 1) initialize
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                           "capabilities": {}, "clientInfo": {"name": "dashboard", "version": "1"}}}
        req = urllib.request.Request(url, method="POST",
                                     data=json.dumps(init).encode(), headers=H)
        resp = urllib.request.urlopen(req, timeout=5)
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        _mcp_parse_sse(resp.read().decode("utf-8", "ignore"))
        H2 = dict(H)
        if sid:
            H2["Mcp-Session-Id"] = sid
        # 2) notifications/initialized
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            urllib.request.urlopen(urllib.request.Request(
                url, method="POST", data=json.dumps(note).encode(), headers=H2), timeout=4)
        except Exception:
            pass
        # 3) tools/list
        tl = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        req3 = urllib.request.Request(url, method="POST",
                                      data=json.dumps(tl).encode(), headers=H2)
        j = _mcp_parse_sse(urllib.request.urlopen(req3, timeout=5).read().decode("utf-8", "ignore"))
        tools = []
        for t in ((j or {}).get("result") or {}).get("tools", []):
            tools.append({"name": t.get("name"),
                          "desc": (t.get("description") or "").replace("\n", " ")[:100]})
        return tools
    except Exception as e:
        return [{"name": "(連線失敗)", "desc": str(e)[:50]}]


@app.get("/api/tools_all")
def get_tools_all():
    """所有工具的真相：每個來自哪個 MCP、怎麼接的（含描述）。快取 60 秒。"""
    import time as _time
    if _TOOLS_CACHE["data"] and (_time.time() - _TOOLS_CACHE["ts"] < 60):
        return JSONResponse(_TOOLS_CACHE["data"])
    groups = []
    for srv in _hermes_mcp_servers():
        tools = _mcp_list_tools(srv["url"], srv.get("headers"))
        groups.append({"source": srv["name"], "via": "MCP", "url": srv["url"],
                       "enabled": srv["enabled"], "tools": tools, "count": len(tools)})
    data = {"groups": groups}
    _TOOLS_CACHE.update({"ts": _time.time(), "data": data})
    return JSONResponse(data)


@app.get("/api/hermes/sessions")
def get_hermes_sessions():
    """hermes-agent 的對話 session（語音/Telegram/CLI 統一紀錄）。"""
    return JSONResponse({"sessions": _hermes_sessions(30)})


@app.get("/api/capabilities")
def get_capabilities():
    """Jarvis 的進階能力清單 + 開關狀態（dashboard 顯示，讓你知道有哪些、開了沒）。"""
    cfg = _hermes_config()
    deleg = cfg.get("delegation", {})
    skills = cfg.get("skills", {})
    pt = cfg.get("platform_toolsets", {})
    voice_full = "hermes-cli" in (pt.get("api_server") or [])
    # 「有沒有真的開放給對話管道」= toolset 名稱要在 telegram 或 api_server 的清單裡，
    # 不能只看 config 裡的全域開關（那個開了但沒掛給任何平台，對話裡也用不到，之前這裡誤判過）。
    _deleg_wired = any("delegation" in (pt.get(p) or []) for p in ("telegram", "api_server"))
    _cron_wired = any("cronjob" in (pt.get(p) or []) for p in ("telegram", "api_server"))
    import shutil as _sh
    cua = bool(_sh.which("cua-driver") or os.path.exists(os.path.expanduser("~/.local/bin/cua-driver")))
    caps = [
        {"name": "自主建技能", "desc": "做完任務自動把流程存成技能，下次更快",
         "on": voice_full, "detail": f"提醒間隔每 {skills.get('creation_nudge_interval', 10)} 輪"},
        {"name": "子代理委派", "desc": "複雜任務拆給多個分身平行處理，共用你的財務/記憶工具",
         "on": bool(deleg.get("orchestrator_enabled")) and _deleg_wired,
         "detail": f"最多 {deleg.get('max_concurrent_children', 3)} 個分身・{'已開放給對話' if _deleg_wired else '尚未掛給任何管道'}"},
        {"name": "自我排程", "desc": "跟 Jarvis 說「每天提醒我...」它自己會建立排程，用 hermes-agent 內建心跳執行",
         "on": _cron_wired, "detail": "已開放給對話" if _cron_wired else "尚未掛給任何管道"},
        {"name": "跨模型容錯", "desc": "主要模型服務掛掉時，自動切換到備援模型繼續對話",
         "on": bool(cfg.get("fallback_providers")),
         "detail": (f"備援：{(cfg.get('fallback_providers') or [{}])[0].get('model','-')}"
                    if cfg.get("fallback_providers") else "未設定")},
        {"name": "電腦操作", "desc": "控制你的桌面、操作 App",
         "on": cua and voice_full, "detail": "driver 已裝" + ("" if cua else "（未裝）") + "，需 macOS 權限(輔助使用+螢幕錄製)"},
        {"name": "瀏覽器自動化", "desc": "自動上網點擊填表", "on": voice_full,
         "detail": "工具在語音工具集內"},
        {"name": "網路搜尋", "desc": "即時上網查最新", "on": True, "detail": "search_web"},
        {"name": "Email", "desc": "Jarvis 讀信/摘要/找信/回信",
         "on": (os.path.exists(os.path.expanduser("~/.config/himalaya/config.toml"))
                and "PASTE_APP_PASSWORD" not in open(os.path.expanduser("~/.config/himalaya/config.toml")).read()),
         "detail": "you@example.com（讀/找/摘要）"},
        {"name": "智慧家電 HomeAssistant", "desc": "控制燈/開關/感測器", "on": False,
         "detail": "需 HomeAssistant 主機+token"},
        {"name": "Telegram", "desc": "在 Telegram 跟 Jarvis 對話", "on": "telegram" in pt,
         "detail": "需 bot token"},
    ]
    return JSONResponse({"capabilities": caps,
                         "voice_full_toolset": voice_full,
                         "skills_count": len(_hermes_skills())})


_JOBS_DIR = os.path.expanduser("~/Hermes_Brain/config")


@app.get("/api/jobs")
def get_jobs():
    """工作雷達榜單(job_scout.py 產出) + 過濾掉 dashboard ✕ 過的。"""
    try:
        with open(os.path.join(_JOBS_DIR, "job_matches.json"), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return JSONResponse({"ok": True, "updated": None, "jobs": []})
    try:
        with open(os.path.join(_JOBS_DIR, "job_dislikes.json"), encoding="utf-8") as f:
            dislikes = json.load(f)
    except Exception:
        dislikes = {}
    jobs = [j for j in (d.get("top") or []) if j.get("key") not in dislikes
            and f"{j.get('company','')}|{j.get('title','')}".lower() not in dislikes]
    return JSONResponse({"ok": True, "updated": d.get("updated"), "jobs": jobs})


@app.post("/api/jobs/dismiss")
async def dismiss_job(req: Request):
    """✕ 掉一個職缺:永不再出現,且之後 LLM 精評會把它當「口味負樣本」。"""
    body = await req.json()
    key = str((body or {}).get("key", "")).strip().lower()
    title = str((body or {}).get("title", ""))
    if not key:
        return JSONResponse({"ok": False, "error": "missing key"}, status_code=400)
    path = os.path.join(_JOBS_DIR, "job_dislikes.json")
    try:
        with open(path, encoding="utf-8") as f:
            dislikes = json.load(f)
    except Exception:
        dislikes = {}
    dislikes[key] = {"title": title,
                     "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dislikes, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return JSONResponse({"ok": True, "count": len(dislikes)})


_RESUME_DIR = os.path.expanduser("~/Hermes_Brain/config/resumes")


def _extract_pdf_text(path):
    try:
        import pypdf
        r = pypdf.PdfReader(path)
        return "\n".join((p.extract_text() or "") for p in r.pages).strip()
    except Exception:
        return ""


@app.post("/api/jobs/resume")
async def upload_resume(file: UploadFile = File(...), lang: str = Form("")):
    """上傳履歷(PDF/txt)→ 抽文字 → LLM 生成配對用的履歷輪廓 + 搜尋關鍵字,
    存進 job_profile.json。之後掃描/評分/深度分析都改用這份(換人用只要重傳)。
    lang='zh'/'en' 明確指定這是中文還是英文履歷(存進對應 slot,投遞時 104 外商/英文缺
    用英文、台灣中文缺用中文)。沒指定才自動偵測。兩份履歷可分開上傳、各存一個 slot。"""
    os.makedirs(_RESUME_DIR, exist_ok=True)
    name = os.path.basename(file.filename or "resume")
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "檔案太大(上限8MB)"}, status_code=400)
    save_path = os.path.join(_RESUME_DIR, name)
    with open(save_path, "wb") as f:
        f.write(raw)
    # 抽文字
    if name.lower().endswith(".pdf"):
        text = _extract_pdf_text(save_path)
    else:
        text = raw.decode("utf-8", "ignore")
    if len(text) < 40:
        return JSONResponse({"ok": False,
                             "error": "讀不到履歷文字(掃描版PDF?請改貼純文字或換檔)"}, status_code=400)
    # LLM 生成輪廓 + 搜尋關鍵字
    prompt = (
        "讀這份履歷,產出職缺配對引擎要用的 JSON:\n"
        '{"profile":"120字內描述他的程度/領域/硬技能/適合什麼職位(繁中)",'
        '"terms":["3-6個用來搜職缺的關鍵字"],"lang":"zh或en(履歷主要語言)"}\n\n'
        "履歷:\n" + text[:6000] + "\n\n只回 JSON。")
    profile, terms = "", []
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8808/v1beta/openai/chat/completions",
            data=json.dumps({"model": "gemini-3.1-flash-lite",
                             "messages": [{"role": "user", "content": prompt}],
                             "temperature": 0.2}).encode(),
            headers={"Content-Type": "application/json"}), timeout=50)
        import re as _re
        txt = json.loads(r.read())["choices"][0]["message"]["content"]
        obj = json.loads(_re.search(r"\{[\s\S]*\}", txt).group(0))
        profile = str(obj.get("profile", ""))[:900]
        terms = [str(t)[:40] for t in (obj.get("terms") or [])][:6]
        detected_lang = obj.get("lang", "")
    except Exception:
        profile = text[:600]
        detected_lang = ("zh" if any("一" <= c <= "鿿" for c in text[:200]) else "en")
    # slot:前端明確按了「中文/英文履歷」就用它;沒按才用 LLM 偵測的語言
    slot_lang = lang.strip().lower() if lang.strip().lower() in ("zh", "en") else \
        (detected_lang if detected_lang in ("zh", "en") else "en")
    # 存進 profile(保留使用者原本的自訂條件 custom/analyzed 與另一個語言 slot)
    prof = _load_saved_profile()
    prof["resume_profile"] = profile
    prof["resume_terms"] = terms
    prof["resume_filename"] = name
    prof[("resume_zh" if slot_lang == "zh" else "resume_en")] = save_path
    prof["resume_slot"] = slot_lang
    prof["resume_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    _save_profile(prof)
    return JSONResponse({"ok": True, "profile": profile, "terms": terms,
                         "filename": name, "lang": lang})


def _load_saved_profile():
    try:
        with open(os.path.join(_JOBS_DIR, "job_profile.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_profile(prof):
    path = os.path.join(_JOBS_DIR, "job_profile.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prof, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


@app.get("/api/jobs/prefs")
def get_job_prefs():
    try:
        with open(os.path.join(_JOBS_DIR, "job_profile.json"), encoding="utf-8") as f:
            return JSONResponse({"ok": True, **json.load(f)})
    except Exception:
        return JSONResponse({"ok": True, "custom": "", "analyzed": ""})


@app.post("/api/jobs/prefs")
async def set_job_prefs(req: Request):
    """存自訂求職條件:先讓 LLM 把使用者的白話整理成配對引擎可用的標準
    (硬性條件/加分/排除),存起來後 job_scout 的精評會以此為最高優先。"""
    body = await req.json()
    custom = str((body or {}).get("text", "")).strip()
    analyzed = ""
    if custom:
        try:
            prompt = (
                "使用者描述了他想要的工作。把它整理成職缺配對引擎用的標準,分三類條列:"
                "【硬性條件】(不符合就不推)/【加分】/【排除】(看到就跳過)。"
                "保留他的原意、不要腦補他沒說的。50-120字。\n\n使用者說:\n" + custom)
            r = urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8808/v1beta/openai/chat/completions",
                data=json.dumps({"model": "gemini-3.1-flash-lite",
                                 "messages": [{"role": "user", "content": prompt}],
                                 "temperature": 0.2}).encode(),
                headers={"Content-Type": "application/json"}), timeout=40)
            analyzed = json.loads(r.read())["choices"][0]["message"]["content"].strip()[:500]
        except Exception:
            analyzed = ""  # LLM 掛了就存原文,配對引擎直接吃原文一樣能用
    # 合併寫入:保留履歷輪廓/檔案路徑等既有欄位,只更新求職條件
    data = _load_saved_profile()
    data.update({"custom": custom, "analyzed": analyzed,
                 "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
    _save_profile(data)
    return JSONResponse({"ok": True, "custom": custom, "analyzed": analyzed})


@app.post("/api/jobs/apply")
async def apply_job(req: Request):
    """一鍵投遞:背景開 job_apply.py(可見 Chrome+persistent 登入),自動 Easy Apply
    +上傳履歷;沒把握的欄位會停下來留視窗給 Owen。立即回應,進度看 apply log。"""
    import subprocess
    body = await req.json()
    url = str((body or {}).get("url", "")).strip()
    title = str((body or {}).get("title", ""))
    is_foreign = "1" if (body or {}).get("is_foreign") else "0"
    if not url.startswith("http"):
        return JSONResponse({"ok": False, "error": "bad url"}, status_code=400)
    subprocess.Popen(
        [os.path.expanduser("~/Hermes_Brain/.venv/bin/python"), "-u",
         os.path.expanduser("~/Hermes_Brain/scripts/job_apply.py"), url, title, is_foreign],
        cwd=os.path.expanduser("~/Hermes_Brain"),
        stdout=open(os.path.expanduser("~/Hermes_Brain/memory/logs/job_apply.log"), "a"),
        stderr=subprocess.STDOUT)
    return JSONResponse({"ok": True, "message": "投遞視窗開啟中"})


@app.get("/api/jobs/apply_log")
def get_apply_log():
    try:
        with open(os.path.join(_JOBS_DIR, "job_apply_log.json"), encoding="utf-8") as f:
            return JSONResponse({"ok": True, **json.load(f)})
    except Exception:
        return JSONResponse({"ok": True, "applies": []})


_JD_CACHE = os.path.join(_JOBS_DIR, "job_jd_cache.json")


def _fetch_jd(job):
    """抓單一職缺的完整 JD 原文。LinkedIn 用 guest jobPosting API,Yourator 抓職缺頁。"""
    import re as _re
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120 Safari/537.36")
    try:
        if job.get("source") == "LinkedIn" and job.get("jd_id"):
            url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['jd_id']}"
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": ua}), timeout=18
            ).read().decode("utf-8", "ignore")
            m = _re.search(r'show-more-less-html__markup[^>]*>([\s\S]*?)</div>', raw)
            if m:
                t = _re.sub(r'<[^>]+>', ' ', m.group(1))
                return _re.sub(r'\s+', ' ', t).strip()[:4000]
        # Yourator / 其他:抓職缺頁的 JSON-LD JobPosting.description(最完整)
        if job.get("url", "").startswith("http"):
            raw = urllib.request.urlopen(
                urllib.request.Request(job["url"], headers={"User-Agent": ua}), timeout=18
            ).read().decode("utf-8", "ignore")
            for block in _re.findall(r'<script type="application/ld\+json">([\s\S]*?)</script>', raw):
                try:
                    obj = json.loads(block)
                except Exception:
                    continue
                for cand in (obj if isinstance(obj, list) else [obj]):
                    desc = isinstance(cand, dict) and cand.get("description")
                    if desc:
                        t = _re.sub(r'<[^>]+>', ' ', desc)
                        t = t.replace("&nbsp;", " ").replace("&amp;", "&")
                        return _re.sub(r'\s+', ' ', t).strip()[:4000]
            m = _re.search(r'<meta name="description" content="([^"]{40,})"', raw)
            if m:
                return m.group(1)[:4000]
    except Exception:
        pass
    return ""


@app.post("/api/jobs/jd")
async def get_job_jd(req: Request):
    """抓某職缺的完整 JD + 翻成中文(整段,不只標題)。有快取避免重抓重譯。"""
    body = await req.json()
    key = str((body or {}).get("key", "")).strip().lower()
    if not key:
        return JSONResponse({"ok": False, "error": "missing key"}, status_code=400)
    cache = {}
    try:
        with open(_JD_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        pass
    if key in cache:
        return JSONResponse({"ok": True, "cached": True, **cache[key]})
    # 從榜單找這筆職缺
    try:
        with open(os.path.join(_JOBS_DIR, "job_matches.json"), encoding="utf-8") as f:
            jobs = (json.load(f).get("top") or [])
    except Exception:
        jobs = []
    job = next((j for j in jobs if j.get("key") == key), None)
    if not job:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)

    def _fetch_and_translate():
        """阻塞的抓 JD + 翻譯,丟 threadpool 跑,別卡 event loop。回 (jd, jd_zh)。"""
        jd = _fetch_jd(job)
        if not jd:
            return "", ""
        # 已是中文就不翻
        zh_ratio = sum(1 for c in jd if "一" <= c <= "鿿") / max(len(jd), 1)
        if zh_ratio > 0.25:
            return jd, jd
        try:
            prompt = ("把這段職缺說明完整翻成通順的繁體中文,保留條列結構,不要加註解或省略:\n\n" + jd)
            r = urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8808/v1beta/openai/chat/completions",
                data=json.dumps({"model": "gemini-3.1-flash-lite",
                                 "messages": [{"role": "user", "content": prompt}],
                                 "temperature": 0.1}).encode(),
                headers={"Content-Type": "application/json"}), timeout=70)
            return jd, json.loads(r.read())["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return jd, f"(翻譯失敗:{str(e)[:40]},以下為原文)\n\n" + jd

    jd, jd_zh = await asyncio.to_thread(_fetch_and_translate)
    if not jd:
        return JSONResponse({"ok": True, "jd": "", "jd_zh": "",
                             "note": "這個職缺抓不到說明內文,點「查看職缺」看原頁"})
    entry = {"jd": jd, "jd_zh": jd_zh}
    cache[key] = entry
    # 快取別無限長大
    if len(cache) > 300:
        for k in list(cache)[:-300]:
            cache.pop(k, None)
    tmp = _JD_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _JD_CACHE)
    return JSONResponse({"ok": True, **entry})


_SAVED_PATH = os.path.join(_JOBS_DIR, "job_saved.json")


def _load_saved():
    try:
        with open(_SAVED_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_saved(d):
    tmp = _SAVED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _SAVED_PATH)


@app.get("/api/jobs/saved")
def list_saved():
    d = _load_saved()
    jobs = sorted(d.values(), key=lambda x: x.get("saved_ts", ""), reverse=True)
    return JSONResponse({"ok": True, "jobs": jobs})


@app.post("/api/jobs/save")
async def save_job(req: Request):
    """⭐ 關注/取消關注。關注時把職缺(含 JD)存進獨立的追蹤清單,可反覆查看。"""
    body = await req.json()
    key = str((body or {}).get("key", "")).strip().lower()
    if not key:
        return JSONResponse({"ok": False, "error": "missing key"}, status_code=400)
    saved = _load_saved()
    if key in saved:  # 再按一次 = 取消關注
        saved.pop(key, None)
        _write_saved(saved)
        return JSONResponse({"ok": True, "saved": False})
    # 從榜單找完整職缺
    try:
        with open(os.path.join(_JOBS_DIR, "job_matches.json"), encoding="utf-8") as f:
            job = next((j for j in (json.load(f).get("top") or [])
                        if j.get("key") == key), None)
    except Exception:
        job = None
    if not job:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    entry = dict(job)
    entry["jd"] = await asyncio.to_thread(_fetch_jd, job)  # 阻塞抓取丟 threadpool,別卡 event loop
    entry["saved_ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    saved[key] = entry
    _write_saved(saved)
    return JSONResponse({"ok": True, "saved": True, "count": len(saved)})


@app.post("/api/jobs/research")
async def research_job(req: Request):
    """🔬 對追蹤中的職缺做深度研究:網路查公司風評/薪資行情 + 用你的履歷對照 JD
    做差距分析與補強建議 + 面試準備方向。結果存進追蹤清單,反覆看不用重跑。
    force=true 可要求重新研究。"""
    body = await req.json()
    key = str((body or {}).get("key", "")).strip().lower()
    force = bool((body or {}).get("force"))
    saved = _load_saved()
    entry = saved.get(key)
    if not entry:
        return JSONResponse({"ok": False, "error": "先關注這個職缺"}, status_code=400)
    if entry.get("research") and not force:
        return JSONResponse({"ok": True, "cached": True, "research": entry["research"],
                             "research_ts": entry.get("research_ts")})
    # 履歷輪廓:呼叫 _resume_profile()(動態讀上傳履歷),不是 import 靜態 RESUME_PROFILE
    # 常數——後者永遠是預設值,上傳新履歷後研究會拿錯人的背景。
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.expanduser("~/Hermes_Brain"))
        from scripts.job_scout import _resume_profile
        RESUME_PROFILE = _resume_profile()
    except Exception:
        RESUME_PROFILE = "(履歷輪廓載入失敗)"
    prof = {}
    try:
        with open(os.path.join(_JOBS_DIR, "job_profile.json"), encoding="utf-8") as f:
            prof = json.load(f)
    except Exception:
        pass
    custom = (prof.get("analyzed") or prof.get("custom") or "").strip()
    jd = (entry.get("jd") or "")[:2500]
    prompt = (
        f"我在考慮應徵這個職缺,幫我上網研究並給我一份完整的求職情報。用繁體中文,分成清楚的段落標題:\n\n"
        f"職缺:{entry.get('title')}\n公司:{entry.get('company')}\n地點:{entry.get('loc')}\n"
        f"薪資(若有):{entry.get('salary') or '未列'}\n"
        f"職缺說明(JD):\n{jd or '(無)'}\n\n"
        f"我的背景:\n{RESUME_PROFILE}\n"
        + (f"我額外的求職條件:{custom}\n" if custom else "")
        + "\n只用你的網路搜尋能力(search_web)查資料後,【直接把完整內容打字回覆給我】。\n"
        "嚴禁:不要開瀏覽器自動化、不要執行 shell、不要 do_on_computer、不要 dispatch_task、"
        "不要把結果存成檔案——這些都會失敗。答案就直接寫在你的回覆裡。\n"
        "產出這五個部分:\n"
        "① 公司風評與文化(Glassdoor/PTT/求職版的員工評價、優缺點、離職率風向)\n"
        "② 這個職位的薪資行情(用台灣市場實際數字)\n"
        "③ JD 需求 vs 我的背景:我哪些條件已符合、哪些有落差\n"
        "④ 針對落差,我該怎麼補強/在履歷面試怎麼包裝(具體可行動)\n"
        "⑤ 面試準備方向與可能會問的問題\n"
        "務必實際查網路,不要空泛。結論導向、講重點。")
    def _looks_bad(t):
        # flash-lite 偶發:幻覺說自己不能上網 / 亂呼叫不存在的工具 → 判定壞回應要重試
        return (not t or len(t) < 120
                or ("不能" in t and ("上網" in t or "查" in t) and "資料" in t)
                or "dispatch_task" in t or "工具清單" in t)

    api_key = _hermes_env().get("API_SERVER_KEY", "hermes-voice-local")

    def _ask():
        r = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8642/v1/chat/completions",
            data=json.dumps({"model": "hermes",
                             "messages": [{"role": "user", "content": prompt}],
                             "stream": False}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"}), timeout=180)
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()

    def _ask_with_retry():
        r = ""
        for _attempt in range(3):   # 壞回應自動重試(flash-lite 不穩)
            r = _ask()
            if not _looks_bad(r):
                break
        return r

    research = ""
    try:
        # 阻塞的 LLM 呼叫(最壞 3×180s)丟到 threadpool,別卡死整個 dashboard event loop
        research = await asyncio.to_thread(_ask_with_retry)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"研究失敗:{str(e)[:60]}"}, status_code=502)
    if _looks_bad(research):
        return JSONResponse({"ok": False, "error": "AI 這次沒查成(模型不穩),再按一次深度分析"}, status_code=502)
    entry["research"] = research
    entry["research_ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    saved[key] = entry
    _write_saved(saved)
    return JSONResponse({"ok": True, "research": research, "research_ts": entry["research_ts"]})


@app.post("/api/jobs/rescan")
def rescan_jobs():
    """手動重掃(不推播,只更新榜單)。同步跑,前端等它(約 30-60s)。"""
    import subprocess
    try:
        r = subprocess.run(
            [os.path.expanduser("~/Hermes_Brain/.venv/bin/python"), "-u", "-m", "scripts.job_scout"],
            cwd=os.path.expanduser("~/Hermes_Brain"),
            capture_output=True, text=True, timeout=150)
        return JSONResponse({"ok": r.returncode == 0, "log": (r.stdout or "")[-400:]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:100]})


@app.get("/api/heartbeat")
def get_heartbeat():
    """Jarvis 的主動關懷心跳狀態：排程、下次判斷時間、上次判斷結果（開口了還是安靜）。
    讀 hermes-agent 自己的 cron jobs.json + 最新一次執行的 output（純讀、不影響排程本身）。"""
    jobs_path = os.path.join(HERMES_HOME, "cron", "jobs.json")
    try:
        with open(jobs_path, encoding="utf-8") as f:
            jobs = (json.load(f) or {}).get("jobs", [])
    except Exception:
        return JSONResponse({"ok": False, "job": None})
    job = next((j for j in jobs if j.get("name") == "Jarvis 主動關懷"), None)
    if not job:
        return JSONResponse({"ok": True, "job": None})
    last_decision, last_spoke = None, None
    out_dir = os.path.join(HERMES_HOME, "cron", "output", job.get("id", ""))
    try:
        files = sorted(os.listdir(out_dir))
        if files:
            content = open(os.path.join(out_dir, files[-1]), encoding="utf-8").read()
            resp = content.split("## Response", 1)[-1].strip()
            last_spoke = "[SILENT]" not in resp
            last_decision = resp if last_spoke else "（判斷安靜，沒有開口）"
    except Exception:
        pass
    return JSONResponse({"ok": True, "job": {
        "schedule": job.get("schedule_display"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "enabled": job.get("enabled"),
        "last_decision": last_decision,
        "last_spoke": last_spoke,
    }})


# ---------- 系統狀態 ----------
@app.get("/api/status")
def get_status():
    def _ping(url, timeout=2):
        # 用 HTTP 狀態碼判斷，不靠字串比對。404/405 代表服務活著只是沒這路徑。
        try:
            r = urllib.request.urlopen(url, timeout=timeout)
            return 200 <= getattr(r, "status", 200) < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except Exception:
            return False
    def _proc_alive(pattern):
        try:
            import subprocess
            return subprocess.run(["pgrep", "-f", pattern],
                                  capture_output=True, timeout=2).returncode == 0
        except Exception:
            return False
    keys = None
    try:
        k = json.load(urllib.request.urlopen("http://127.0.0.1:8808/admin/keys", timeout=3))
        ks = k.get("keys", [])
        keys = {"active": sum(1 for x in ks if x.get("status") == "active"), "total": len(ks)}
    except Exception:
        pass
    return JSONResponse({
        "services": {
            "🧠 金鑰代理 (8808)": _ping("http://127.0.0.1:8808/healthz"),
            "💾 記憶 (8809)": _ping("http://127.0.0.1:8809/health"),
            "🎵 音樂 (8810)": _ping("http://127.0.0.1:8810/health"),
            "🖥️ 控制台 (8811)": True,
            # Telegram 已搬進 hermes-agent gateway（不再是獨立 bot）→ 判斷 gateway(8642)活 + token 有設
            "💬 Telegram Bot": _port_alive("8642") and bool(_hermes_env().get("TELEGRAM_BOT_TOKEN")),
            "🤖 語音伺服器 / StackChan": _ping("http://127.0.0.1:8003") or _proc_alive("xiaozhi-esp32-server"),
            "⏰ 提醒精靈": _proc_alive("reminder_daemon"),
        },
        "keys": keys,
        "time": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
    })


_ARCH_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>JARVIS 架構總覽</title>
<style>
 body{margin:0;background:#070b14;color:#cfe8ff;font-family:system-ui,sans-serif;padding:20px}
 h1{font-weight:600;color:#5af4ff;text-shadow:0 0 12px #5af4ff66;text-align:center}
 .sub{text-align:center;color:#8aa;margin-bottom:24px;font-size:14px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px;max-width:1200px;margin:0 auto}
 .card{background:#0d1626;border:1px solid #1d3a52;border-radius:12px;padding:16px;box-shadow:0 0 18px #0af2}
 .card h2{margin:0 0 4px;font-size:18px;display:flex;align-items:center;gap:8px}
 .what{font-size:12px;color:#7fb;margin-bottom:12px;line-height:1.5}
 .tag{display:inline-block;background:#13283a;border:1px solid #2af5;color:#9df;border-radius:6px;padding:3px 9px;margin:3px;font-size:12px}
 .grp{font-size:12px;color:#ffcf5c;margin:8px 0 2px}
 .fact{font-size:13px;padding:5px 8px;border-left:2px solid #5af4ff;margin:4px 0;background:#0a1220;border-radius:0 6px 6px 0}
 .c-mem{border-color:#5af4ff}.c-skill{border-color:#5affd0}.c-cmd{border-color:#ffcf5c}.c-dyn{border-color:#ff8a5a}.c-chat{border-color:#9b8bff}
 a{color:#5af4ff}
</style></head><body>
<h1>🦾 JARVIS 系統架構總覽</h1>
<div class="sub">一眼看懂：記憶 / 技能 / 觸發 / 即時資料 / 聊天 —— 各司其職、互不混淆</div>
<div class="grid">
 <div class="card c-mem"><h2>🧠 記憶 <span style="font-size:12px;color:#7fb" id="memcount"></span></h2>
   <div class="what"><b>固定不變的事實＋規則</b>，AI 知道後會照著行動，可語意搜尋。<br>✅ 只放固定的　❌ 不放會變的、不放對話紀錄</div>
   <div id="facts">載入中…</div></div>
 <div class="card c-skill"><h2>🔧 技能 / 工具</h2>
   <div class="what"><b>AI 能「執行」的能力</b>（24 項），對話中自己判斷該用哪個。</div>
   <div class="grp">🎵 音樂</div><span class="tag">播放電腦音樂</span><span class="tag">控制播放</span>
   <div class="grp">💰 理財記帳</div><span class="tag">查理財</span><span class="tag">更新財務</span><span class="tag">記花費</span><span class="tag">查花費</span><span class="tag">匯率換算</span>
   <div class="grp">⏰ 提醒待辦計時</div><span class="tag">設提醒</span><span class="tag">查提醒</span><span class="tag">加待辦</span><span class="tag">查待辦</span><span class="tag">計時器</span>
   <div class="grp">🌤️ 查詢</div><span class="tag">天氣</span><span class="tag">新聞</span><span class="tag">農曆</span><span class="tag">找附近</span>
   <div class="grp">💻 電腦/自我擴充</div><span class="tag">操作電腦</span><span class="tag">存桌面</span><span class="tag">派任務</span><span class="tag">自建新功能</span>
   <div class="grp">🎭 互動/記憶</div><span class="tag">跳舞</span><span class="tag">切換角色</span><span class="tag">記住事實</span></div>
 <div class="card c-cmd"><h2>⚡ 指令觸發</h2>
   <div class="what"><b>關鍵詞 / 指令 → 固定行為</b>。記憶裡的規則由 AI 執行；明確指令直接觸發。</div>
   <div class="grp">🗣️ 語音關鍵詞（記憶規則驅動）</div>
   <div class="fact">說「開工」→ 播《a late night RNB experience in Brooklyn》+ 進專注模式</div>
   <div class="grp">💬 Telegram 指令</div>
   <span class="tag">/remember</span><span class="tag">/expense</span><span class="tag">/research</span><span class="tag">/convert</span><span class="tag">/contact</span><span class="tag">/watch</span><span class="tag">/budget</span></div>
 <div class="card c-dyn"><h2>📊 動態資料（即時抓，不存記憶）</h2>
   <div class="what"><b>會變的數字 → 每次即時查</b>，存了就過時。記憶絕不放這些。</div>
   <span class="tag">💰 理財總覽(Yahoo股價)</span><span class="tag">🧾 本期收支</span><span class="tag">📈 報酬率</span><span class="tag">💵 餘額/預算</span><span class="tag">🍱 今日花費</span>
   <div style="margin-top:8px;font-size:12px"><a href="/">→ 回主控台看即時數字</a></div></div>
 <div class="card c-chat"><h2>💬 聊天紀錄</h2>
   <div class="what"><b>對話過程</b>。連續性靠對話 context（短期），紀錄存這顯示用 —— <b>不進記憶</b>。</div>
   <div style="font-size:13px;color:#9ab">每輪對話 → 聊天頁顯示，不會被當「事實」記住。</div>
   <div style="margin-top:8px;font-size:12px"><a href="/">→ 回主控台看對話記錄</a></div></div>
</div>
<script>
fetch('/api/memory').then(r=>r.json()).then(d=>{
 const fs=(d.facts||d||[]); const el=document.getElementById('facts');
 document.getElementById('memcount').textContent='('+fs.length+' 筆)';
 el.innerHTML=fs.map(f=>'<div class="fact">'+((f.text||f)+'').replace(/</g,'&lt;').slice(0,90)+'</div>').join('')||'(空)';
}).catch(()=>{document.getElementById('facts').textContent='讀取失敗';});
</script></body></html>"""


@app.get("/map", response_class=HTMLResponse)
def architecture_map():
    return HTMLResponse(_ARCH_HTML)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        # no-store：每次刷新都拿最新版，避免瀏覽器抓到舊的 dashboard（刷新沒變的元兇）
        return HTMLResponse(f.read(), headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache", "Expires": "0"})


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8811, log_level="warning")
