"""
語音大腦橋接（xiaozhi ↔ hermes-agent）
=====================================
xiaozhi 用標準 openai client，沒辦法帶自訂 header。但 hermes-agent 需要
X-Hermes-Session-Id 才能維持「連續同一個 session」（否則每輪都開新 session →
每輪重做 session 初始化 + 標題生成 = 慢很多）。

這個橋接坐在中間：xiaozhi → 8643(這裡) → 8642(hermes-agent)，
按 device-id 給每台裝置一個穩定 session id，並補上 Authorization。
串流(SSE)原樣轉發，讓 TTS 能邊收邊唸。

xiaozhi 設定 base_url 指到 http://host.docker.internal:8643/v1
"""
import json
import os
import re
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

HERMES_API = os.environ.get("HERMES_API", "http://127.0.0.1:8642")
API_KEY = os.environ.get("API_SERVER_KEY", "hermes-voice-local")
# session 基底；實際 session id 會在閒置後輪替，避免單一 session 無限長大、跨話題混淆。
VOICE_SESSION_BASE = os.environ.get("VOICE_SESSION_ID", "voice-owen")
# 閒置超過這麼久(秒) → 視為新一段對話，換新 session（乾淨開始 + 重載最新記憶）。
IDLE_GAP = int(os.environ.get("VOICE_IDLE_GAP", "1500"))   # 25 分鐘

app = FastAPI(title="Voice Brain Bridge")
_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=8.0))

# 每一輪用全新 session → hermes-agent 不累積髒 session(時間答錯/舊話題亂入/答案重用全根治)。
# 跟進對話的上下文由 xiaozhi 送的 messages 提供(已驗證有效)，不靠 hermes-agent 的 session 記憶。
_state = {"n": 0}


def _current_session():
    """每次請求回一個全新 session id → 大腦每輪乾淨開始，不被上一輪污染。"""
    _state["n"] += 1
    # 用計數器 + 進程啟動時間，保證每輪唯一
    return f"{VOICE_SESSION_BASE}-{int(time.time())}-{_state['n']}"


@app.get("/health")
async def health():
    return {"status": "ok", "upstream": HERMES_API,
            "session_base": VOICE_SESSION_BASE, "turns": _state["n"]}


async def _deterministic_cancel_reminder(last: str):
    """高頻 CRUD 確定性攔截:flash-lite 會嘴上說「取消了」但根本沒呼叫工具(假成功,實測抓到)。
    「取消/刪掉/不用...提醒」這類明確指令直接打 8809 做掉+回結果,不經模型。
    回 None = 不攔截(找不到相符時也交回模型,讓它反問)。"""
    import re
    t = (last or "").strip()
    if not t or len(t) > 24 or "?" in t or "？" in t:
        return None
    m = re.search(r"(?:取消|刪掉|刪除|不用)(?:那個|這個|剛剛|剛才)?(.{0,12}?)(?:的)?提醒", t)
    if not m:
        return None
    q = m.group(1).strip()
    try:
        r = await _client.post("http://127.0.0.1:8809/reminder_cancel",
                               json={"query": q}, timeout=6)
        d = r.json()
    except Exception:
        return None
    if d.get("ok"):
        return f"好，「{d.get('cancelled', '')}」的提醒取消了。"
    if d.get("multiple"):
        return d.get("reason")
    return None


# 收緊的記行程判斷(與 plugin expense-fastpath 同款,避免問句/非排程句誤記):
# 觸發要有未來日期詞或「提醒」,不是問句、不是花費句、不太長。/reminder 為最終 gate。
_DAY_MARK = re.compile(
    r"明天|後天|大後天|下週[一二三四五六日天]?|下星期[一二三四五六日天]?|"
    r"下周[一二三四五六日天]?|本週[一二三四五六日天]|這週[一二三四五六日天]|"
    r"本周[一二三四五六日天]|星期[一二三四五六日天]|禮拜[一二三四五六日天]|"
    r"週[一二三四五六日天]|\d{1,2}\s*月\s*\d{1,2}")
_EVENT_INTENT = re.compile(
    r"比賽|球賽|會議|開會|約會|預約|有約|約了|約人|面試|聚餐|報告|繳費|生日|演唱會|"
    r"回診|看醫生|吃飯|見面|活動|典禮|婚禮|出差|考試|截止|報名|訂位|上課|演出|表演|"
    r"聚會|派對|健檢|體檢|婚宴|開幕|回台|返鄉")
_Q_MARK = re.compile(r"[?？]|嗎|呢|幾點|幾號|幾月|什麼時候|何時|哪天|多久|是不是|如何")
_EXPENSE_HINT = re.compile(r"花了|花費|塊錢|\d+\s*元|\d+\s*塊")


async def _deterministic_add_reminder(last: str):
    """記行程確定性攔截:flash-lite 常回「我幫你記下來了」卻沒呼叫工具(實測籃球賽假成功)。
    要有未來日期詞或「提醒」+行程意圖、非問句、非花費句 → 打 /reminder,成功回 nice。"""
    t = (last or "").strip()
    if not t or len(t) > 40 or _Q_MARK.search(t) or _EXPENSE_HINT.search(t):
        return None
    if "取消" in t or "刪" in t or "不用" in t:   # 那是取消,別當新增
        return None
    remind_me = ("提醒我" in t) or ("提醒你" in t)
    has_trigger = remind_me or bool(_DAY_MARK.search(t))
    if not (has_trigger and (_EVENT_INTENT.search(t) or remind_me)):
        return None
    try:
        r = await _client.post("http://127.0.0.1:8809/reminder",
                               json={"time": t, "message": "", "channel": "both"}, timeout=6)
        d = r.json()
    except Exception:
        return None
    if d.get("ok") and (d.get("time") or d.get("repeat")):
        return d.get("nice") or d.get("text") or "好，記下了。"
    return None


def _openai_reply(text: str, stream: bool):
    """把一句確定性答案包成 OpenAI 相容回應(SSE 或 JSON),讓 xiaozhi/TTS 照常吃。"""
    now = int(time.time())
    if not stream:
        return Response(content=json.dumps({
            "id": f"det-{now}", "object": "chat.completion", "created": now,
            "model": "hermes-deterministic",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}),
            media_type="application/json")

    async def _sse():
        chunk = {"id": f"det-{now}", "object": "chat.completion.chunk", "created": now,
                 "model": "hermes-deterministic",
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                              "finish_reason": None}]}
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        done = dict(chunk)
        done["choices"] = [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    return StreamingResponse(_sse(), media_type="text/event-stream")


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    body = await request.body()
    # 補上 hermes-agent 需要的 header：固定 session（連續對話）+ 授權。
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "authorization",
                                    "x-hermes-session-id")}
    headers["Authorization"] = f"Bearer {API_KEY}"
    # 只對對話請求給全新 session（健康檢查等不算一輪對話）
    if "chat/completions" in path or "responses" in path:
        headers["X-Hermes-Session-Id"] = _current_session()
        # 訪客講話時，注入「你在跟誰講話 + 他的記憶 + 別洩漏Owen金額」提示給大腦(主人則空、不注入)。
        # 帶這輪訊息進去：陌生人報名字會自動建檔。
        try:
            data = json.loads(body)
            msgs = data.get("messages")
            last = msgs[-1].get("content", "") if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict) else ""
            # 【提速】expense_auto 結果沒人用 → fire-and-forget，不再擋每輪最多 3s。
            # 記帳去重(8809, 90s窗)保證模型若也記到不會雙記；跟 identity/turn 並行發出。
            import asyncio as _aio

            async def _bg_expense(_m=last):
                try:
                    await _client.post("http://127.0.0.1:8809/expense_auto", json={"message": _m}, timeout=8)
                except Exception:
                    pass
            _aio.get_event_loop().create_task(_bg_expense())
            r = await _client.post("http://127.0.0.1:8809/identity/turn", json={"message": last}, timeout=2)
            inject = r.json().get("inject", "")
            if inject and isinstance(msgs, list):
                msgs.insert(0, {"role": "system", "content": inject})
                body = json.dumps(data).encode()
            # 確定性 CRUD 攔截(取消提醒):命中就直接做+直接回,不進模型(根治 flash-lite
            # 「嘴上說取消了但沒呼叫工具」的假成功)。只有主人可攔(inject 非空=訪客,不攔)。
            if not inject:
                det = await _deterministic_cancel_reminder(last) \
                    or await _deterministic_add_reminder(last)
                if det:
                    return _openai_reply(det, bool(data.get("stream")))
        except Exception:
            pass
    else:
        headers["X-Hermes-Session-Id"] = f"{VOICE_SESSION_BASE}-misc"
    url = f"{HERMES_API}/{path}"
    if request.url.query:
        url += "?" + request.url.query

    # 串流請求（chat/completions stream）→ SSE 原樣邊收邊轉，TTS 才能早點出聲。
    req = _client.build_request(request.method, url, content=body, headers=headers)
    resp = await _client.send(req, stream=True)
    if "text/event-stream" in resp.headers.get("content-type", ""):
        async def _gen():
            async for chunk in resp.aiter_raw():
                yield chunk
            await resp.aclose()
        return StreamingResponse(_gen(), status_code=resp.status_code,
                                 media_type="text/event-stream")
    data = await resp.aread()
    await resp.aclose()
    return Response(content=data, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8643, log_level="warning")
