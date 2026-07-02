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
