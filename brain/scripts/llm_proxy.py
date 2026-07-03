"""
Hermes Shared LLM Proxy — 共用金鑰輪換代理
=================================================
一個本機 Gemini 透傳反向代理：把任何送進來的請求轉發到
generativelanguage.googleapis.com，並用既有的 KeyManager 注入「輪換中的」
API key。遇到 401/403/429 會把當前 key 標記 error 並自動換下一把重試。

目的：讓三個介面（Telegram / StackChan 語音 / NousResearch hermes-agent CLI）
共用「同一份」金鑰輪換狀態（config/keys.json）。任何元件只要把 LLM 端點指到
這個代理，就自動獲得金鑰輪換，不需各自實作。

用法：
    ./.venv/bin/python scripts/llm_proxy.py
    # 然後把 ~/.hermes/config.yaml 的 base_url 設成：
    #   http://127.0.0.1:8808/v1beta

環境變數：
    HERMES_PROXY_HOST   (預設 127.0.0.1)
    HERMES_PROXY_PORT   (預設 8808)
    HERMES_PROXY_UPSTREAM (預設 https://generativelanguage.googleapis.com)
"""
import asyncio
import os
import sys
import threading

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# 讓 scripts 目錄可被 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from key_manager import KeyManager  # noqa: E402

HOST = os.environ.get("HERMES_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("HERMES_PROXY_PORT", "8808"))
UPSTREAM = os.environ.get(
    "HERMES_PROXY_UPSTREAM", "https://generativelanguage.googleapis.com"
).rstrip("/")

# 觸發金鑰輪換的 HTTP 狀態碼（驗證失敗 / 額度用盡）
ROTATE_ON = {401, 403, 429}
# 上游暫時性錯誤：同一把金鑰短暫退避後重試（不輪換、不鎖 key）
RETRY_5XX = {500, 502, 503, 504}
MAX_5XX_RETRY = 1

# 不應轉發的逐跳 (hop-by-hop) 標頭
HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
}
# 客戶端帶進來的金鑰一律剝掉，改注入輪換金鑰
STRIP_AUTH = {"authorization", "x-goog-api-key"}

app = FastAPI(title="Hermes Shared LLM Proxy")

_km = KeyManager()
_km_lock = threading.Lock()
_client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=6.0))


def _resolve_ollama_url() -> str:
    if os.environ.get("OLLAMA_URL"):
        return os.environ["OLLAMA_URL"]
    try:
        import json as _j
        cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "stackchan.json")
        with open(cfg, encoding="utf-8") as f:
            u = _j.load(f).get("ollama_url")
            if u:
                return u
    except Exception:
        pass
    return "http://localhost:11434"


OLLAMA_URL = _resolve_ollama_url()


async def _ollama_model():
    """挑一個能對話的 Ollama 模型（排除 embed 嵌入模型，優先 qwen）。"""
    try:
        r = await _client.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        models = [m["name"] for m in r.json().get("models", [])]
        # 排除嵌入模型（nomic-embed-text 等不能 chat）
        chat = [m for m in models if "embed" not in m.lower()]
        # 優先 qwen，其次任何能對話的
        for pref in ("qwen2.5", "qwen", "llama", "gemma"):
            for m in chat:
                if pref in m.lower():
                    return m
        return chat[0] if chat else None
    except Exception:
        return None


def _extract_toolcall(text):
    """從文字裡抓出 {"name":..., "arguments":{...}} 形式的工具呼叫（Qwen 常把它漏成文字）。"""
    import json as _j
    i = text.find('"name"')
    if i < 0:
        return None
    start = text.rfind("{", 0, i)
    if start < 0:
        return None
    try:
        obj, _ = _j.JSONDecoder().raw_decode(text[start:])
        if isinstance(obj, dict) and "name" in obj:
            # arguments 有時是字串化的 JSON → 轉回 dict
            if isinstance(obj.get("arguments"), str):
                try:
                    obj["arguments"] = _j.loads(obj["arguments"])
                except Exception:
                    pass
            return obj
    except Exception:
        pass
    return None


async def _ollama_fallback(path: str, body: bytes, model: str = None, keep_tools: bool = False):
    """把 OpenAI 相容 chat 請求轉給本機 Ollama。
    用於 Gemini 全掛的後備，或使用者主動選了本機模型（傳入 model 指定）。
    keep_tools=True（使用者主動選本機）時保留工具，讓 Qwen 自己嘗試 function calling。
    無模型 / 非 chat 路徑 → 回 None（讓上層回 503）。"""
    if "/openai/" not in path or "chat/completions" not in path:
        return None
    if not model:
        model = await _ollama_model()
    if not model:
        return None
    try:
        import json as _json
        payload = _json.loads(body or b"{}")
        want_stream = bool(payload.get("stream"))
        payload["model"] = model
        payload["stream"] = False
        if not keep_tools:
            # 純後備（Gemini 掛）：只負責對話延續，移除工具避免報錯/拖慢。
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
        # 有工具時 Qwen 需要看得到工具 → 不另插死板 system，避免干擾工具判斷
        has_tools = keep_tools and payload.get("tools")
        # 強制後備也守住 Hermes 人格 + 繁體（qwen 常忽略原 system，故再插一條最前面）
        msgs = payload.get("messages") or []
        if not has_tools:
            # 純聊天時硬性守住人格+繁體（qwen 常忽略原 system）；有工具時不插，免得干擾工具判斷
            msgs.insert(0, {"role": "system", "content": (
                "你叫 Jarvis，是 Owen 的夥伴機器人。問你是誰只回「我是 Jarvis，你的夥伴」，"
                "絕不說自己是語言模型/AI/小助手。"
                "【絕對規則】你只能用繁體中文（台灣）回答，無論使用者問什麼，"
                "回覆一個英文單字、一個簡體字都不准出現，全部繁體中文，講話簡短口語。")})
            if msgs and msgs[-1].get("role") == "user":
                msgs[-1] = dict(msgs[-1])
                msgs[-1]["content"] = str(msgs[-1].get("content", "")) + "\n（請用繁體中文簡短回答）"
        payload["messages"] = msgs
        r = await _client.post(
            f"{OLLAMA_URL}/v1/chat/completions",
            json=payload, timeout=45.0,
        )
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})
        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls")
        # Qwen 常把工具呼叫漏成文字（夾模板雜訊）→ 抓回來轉成標準 tool_calls，xiaozhi 才用得到
        if has_tools and not tool_calls and content:
            tc = _extract_toolcall(content)
            if tc and tc.get("name"):
                tool_calls = [{"id": "call_local_0", "type": "function",
                               "function": {"name": tc["name"],
                                            "arguments": _json.dumps(tc.get("arguments") or {},
                                                                     ensure_ascii=False)}}]
                content = ""
                msg["content"] = None
                msg["tool_calls"] = tool_calls
                data["choices"][0]["message"] = msg
                data["choices"][0]["finish_reason"] = "tool_calls"
        if not want_stream:
            return Response(content=_json.dumps(data).encode("utf-8"),
                            status_code=r.status_code, media_type="application/json")
        # 串流模式：把單一回應包成一個 SSE chunk + [DONE]，否則 xiaozhi 串流解析會拿到空。
        delta = {"role": "assistant", "content": content}
        finish = "stop"
        if tool_calls:
            delta["tool_calls"] = tool_calls
            finish = "tool_calls"
        chunk = {
            "id": data.get("id", "fallback"), "object": "chat.completion.chunk",
            "created": data.get("created", 0), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

        async def _sse():
            yield ("data: " + _json.dumps(chunk) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")
    except Exception:
        return None


async def _glm_forward(path: str, body: bytes, model: str = "GLM5.1"):
    """把 OpenAI 相容 chat 請求轉給免費中轉（GLM/Kimi/Step/DeepSeek 等）。保留串流→第一個字快。
    非 chat 路徑 / 沒設金鑰 → 回 None（讓上層走原本 Gemini 路徑）。"""
    if "chat/completions" not in path or not _GLM_KEY:
        return None
    import json as _json
    try:
        payload = _json.loads(body or b"{}")
    except Exception:
        return None
    payload["model"] = model
    # 去掉 Gemini 專屬欄位（GLM 是 OpenAI 相容、不認這些，留著會 400）
    for _k in ("thinking_config", "thinkingConfig", "generationConfig", "safetySettings"):
        payload.pop(_k, None)
    # reasoning 模型（step/deepseek 等）連小問題都先推理一大串 → 拖慢。降到 low 少推理、快一點。
    payload["reasoning_effort"] = "low"
    url = f"{_GLM_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {_GLM_KEY}", "Content-Type": "application/json"}
    try:
        if payload.get("stream"):
            async def _gen():
                try:
                    async with _client.stream("POST", url, json=payload, headers=headers, timeout=90.0) as r:
                        async for chunk in r.aiter_raw():
                            yield chunk
                except Exception:
                    yield b"data: [DONE]\n\n"
            return StreamingResponse(_gen(), media_type="text/event-stream")
        r = await _client.post(url, json=payload, headers=headers, timeout=90.0)
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))
    except Exception:
        return None


def _next_key():
    """回 (index, key)。呼叫端必須記住 index，出錯時用 _report(code, index) 精準冷卻，
    避免併發下冷卻到別的請求正在用的 key。"""
    with _km_lock:
        return _km.get_key_with_index()


def _report(error_code: int, index=None) -> None:
    with _km_lock:
        _km.report_error(error_code, index)


def _build_headers(req: Request, api_key: str, openai_style: bool) -> dict:
    headers = {
        k: v for k, v in req.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() not in STRIP_AUTH
    }
    # 注入金鑰：依端點選對風格。
    #  - OpenAI 相容端點 (/v1beta/openai/...) 用 Authorization: Bearer
    #  - 原生 Gemini 端點用 x-goog-api-key（用 Bearer 會被當成 OAuth token → 401）
    if openai_style:
        headers["authorization"] = f"Bearer {api_key}"
    else:
        headers["x-goog-api-key"] = api_key
    # 強制上游不壓縮：壓縮的 SSE 串流會被 gzip 緩衝卡住，導致串流回應變空/hang
    headers["accept-encoding"] = "identity"
    return headers


def _build_url(path: str, req: Request) -> str:
    # 保留原始 path（含 :generateContent 的冒號，不可被 percent-encode）。
    # 移除查詢字串裡客戶端帶的 ?key=，避免覆蓋注入的金鑰。
    params = [(k, v) for k, v in req.query_params.multi_items() if k.lower() != "key"]
    url = httpx.URL(
        UPSTREAM,
        raw_path=("/" + path.lstrip("/")).encode("ascii"),
    )
    if params:
        url = url.copy_merge_params(params)
    return str(url)


@app.get("/healthz")
@app.get("/health")
async def healthz():
    return {"status": "ok", "upstream": UPSTREAM}


@app.get("/admin/keys")
async def admin_keys():
    """回報金鑰池狀態（不洩漏完整金鑰）"""
    with _km_lock:
        _km._refresh_keys()
        keys = _km.config.get("api_keys", [])
        current = _km.config.get("current_index", 0)
    out = []
    for i, k in enumerate(keys):
        key = k.get("key", "")
        out.append({
            "index": i,
            "preview": (key[:6] + "…" + key[-4:]) if len(key) > 10 else "***",
            "status": k.get("status"),
            "available_at": k.get("available_at"),
            "current": i == current,
        })
    return {"keys": out, "current_index": current, "total": len(keys)}


# ---------- 對話模型切換（不用重啟容器；只改 openai 對話路徑，不動視覺/ASR）----------
import os as _os_m
_MODEL_FILE = _os_m.path.join(_os_m.path.dirname(__file__), "..", "config", "model_override.json")
_MODELS = [
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "note": "最新・最快最聰明的Flash"},
    {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "note": "最頂・最會推理・較慢"},
    {"id": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite", "note": "極速・省・適合閒聊"},
    {"id": "gemini-3-pro-preview", "label": "Gemini 3 Pro", "note": "上一代頂級・聰明"},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "note": "穩定・已調校(預設)"},
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "note": "穩定・聰明・較慢"},
    {"id": "qwen2.5:7b", "label": "本機 Qwen 2.5 7B", "note": "🖥️ 離線・免費・私密（跑在你電腦上，支援工具但7B較不穩、較慢）", "local": True},
    {"id": "step-3.7-flash", "label": "Step 3.7 Flash（免費・最快）", "note": "🆓 免費中轉・帶工具約2s・這批最快（限流時會波動）", "relay": True},
    {"id": "Kimi-k2.6", "label": "Kimi K2.6（免費）", "note": "🆓 免費中轉・純聊天最快約1.7s・聰明", "relay": True},
    {"id": "GLM5.1", "label": "GLM 5.1（免費）", "note": "🆓 免費中轉・聰明・適合重任務/寫作", "relay": True},
    {"id": "Deepseek-v4-flash", "label": "DeepSeek V4 Flash（免費）", "note": "🆓 免費中轉・強・但速度較不穩", "relay": True},
]
_DEFAULT_MODEL = "gemini-2.5-flash"

# GLM（免費公益分組）：OpenAI 相容端點，走串流才快。金鑰/網址放 ~/.hermes/.env。
_GLM_KEY = ""
_GLM_BASE = "https://your-llm-relay.example.com"
try:
    for _l in open(os.path.expanduser("~/.hermes/.env"), encoding="utf-8"):
        if _l.startswith("GLM_API_KEY"):
            _GLM_KEY = _l.split("=", 1)[1].strip().strip('"')
        elif _l.startswith("GLM_BASE_URL"):
            _GLM_BASE = _l.split("=", 1)[1].strip().strip('"').rstrip("/")
except Exception:
    pass


def _is_local_model(mid):
    return any(m["id"] == mid and m.get("local") for m in _MODELS)


def _is_relay_model(mid):
    return any(m["id"] == mid and m.get("relay") for m in _MODELS)


def _load_model_override():
    try:
        with open(_MODEL_FILE, encoding="utf-8") as f:
            return (__import__("json").load(f).get("model") or _DEFAULT_MODEL)
    except Exception:
        return _DEFAULT_MODEL


_model_override = _load_model_override()


@app.get("/admin/model")
async def admin_model_get():
    return {"current": _model_override, "default": _DEFAULT_MODEL, "available": _MODELS}


@app.post("/admin/model")
async def admin_model_set(request: Request):
    global _model_override
    try:
        b = await request.json()
    except Exception:
        b = {}
    m = (b or {}).get("model", "").strip()
    if m not in [x["id"] for x in _MODELS]:
        return JSONResponse(status_code=400, content={"ok": False, "error": "未知的模型"})
    _model_override = m
    try:
        _os_m.makedirs(_os_m.path.dirname(_MODEL_FILE), exist_ok=True)
        with open(_MODEL_FILE, "w", encoding="utf-8") as f:
            __import__("json").dump({"model": m}, f)
    except Exception:
        pass
    return {"ok": True, "current": _model_override}


def _has_tool_history(messages):
    """訊息裡有沒有『工具回合』(tool 角色 或 assistant 帶 tool_calls)。有就是多輪,不走原生轉譯。"""
    for m in messages or []:
        if m.get("role") == "tool":
            return True
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return True
    return False


def _flatten_tool_history(messages):
    """把多輪工具歷史(assistant 帶 tool_calls + tool 結果)攤平成純文字。
    【為什麼】flash-lite 等 3.x 思考模型,多輪歷史裡的 functionCall 一定要帶 thought_signature,
    但經 xiaozhi round-trip 會掉 → 400「missing thought_signature」→ 所有工具查詢失敗。
    攤平後根本沒有 functionCall part → 不需要簽章 → 任何模型(含flash-lite)都不會 400,還更快。"""
    out, results = [], []
    for m in messages or []:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            txt = (m.get("content") or "").strip()
            if txt:
                out.append({"role": "assistant", "content": txt})
            continue  # 丟掉 tool_calls 結構(就是它需要簽章)
        if role == "tool":
            c = m.get("content")
            if c:
                results.append(str(c).strip())
            continue
        out.append(m)
    if results:
        out.append({"role": "user",
                    "content": ("（系統查到的結果：" + "；".join(results)
                                + "。請直接根據這些結果用繁體中文口語回答；裡面的數字/金額一字不差照抄，"
                                + "絕對不要改、不要四捨五入、不要自己算別的數字，也不要編造結果裡沒有的事。"
                                + "不要再呼叫任何工具。）")})
    return out


def _oai_to_native(payload, budget):
    """openai chat 請求 → Gemini 原生 generateContent（含工具歷史：tool_calls→functionCall、tool→functionResponse）。"""
    import json as _j
    msgs = payload.get("messages", [])
    # tool_call_id → 工具名（tool 結果訊息常只帶 id，要回查名字）
    id2name = {}
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if tc.get("id") and (tc.get("function") or {}).get("name"):
                    id2name[tc["id"]] = tc["function"]["name"]
    contents, sys_parts = [], []
    for m in msgs:
        role, content = m.get("role"), m.get("content")
        if role == "system":
            if content:
                sys_parts.append(content if isinstance(content, str) else _j.dumps(content))
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": content if isinstance(content, str) else (content or "")}]})
        elif role == "assistant":
            parts = []
            if content:
                parts.append({"text": content})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                try:
                    _args = _j.loads(fn.get("arguments") or "{}")
                except Exception:
                    _args = {}
                _part = {"functionCall": {"name": fn.get("name"), "args": _args}}
                # 還原 thoughtSignature(編在 id 裡)→ 帶回給 Gemini,多輪 function call 才不會 400
                _tid = tc.get("id") or ""
                if _tid.startswith("ts:"):
                    _part["thoughtSignature"] = _tid[3:]
                parts.append(_part)
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            name = m.get("name") or id2name.get(m.get("tool_call_id")) or "tool"
            if isinstance(content, str) and content.strip().startswith("{"):
                try:
                    resp = _j.loads(content)
                except Exception:
                    resp = {"result": content}
            else:
                resp = {"result": content}
            contents.append({"role": "user", "parts": [{"functionResponse": {"name": name, "response": resp}}]})
    native = {"contents": contents}
    if sys_parts:
        native["system_instruction"] = {"parts": [{"text": "\n".join(sys_parts)}]}
    if payload.get("tools"):
        fds = []
        for t in payload["tools"]:
            f = t.get("function", {})
            fd = {"name": f.get("name"), "description": f.get("description", "")}
            if f.get("parameters"):
                fd["parameters"] = f["parameters"]
            fds.append(fd)
        native["tools"] = [{"function_declarations": fds}]
    gc = {"thinkingConfig": {"thinkingBudget": budget}}
    if "temperature" in payload:
        gc["temperature"] = payload["temperature"]
    if payload.get("max_tokens"):
        gc["maxOutputTokens"] = payload["max_tokens"]
    native["generationConfig"] = gc
    return native


def _native_to_oai(data, model):
    """Gemini 原生回應 → openai chat 格式（含 functionCall → tool_calls）。"""
    import json as _j
    cands = data.get("candidates") or []
    text, tool_calls = "", []
    if cands:
        for i, p in enumerate((cands[0].get("content") or {}).get("parts") or []):
            if p.get("text"):
                text += p["text"]
            if p.get("functionCall"):
                fc = p["functionCall"]
                # Gemini 思考後的 function call 會帶 thoughtSignature，下一輪必須原樣帶回(否則 400)。
                # openai 格式沒這欄位 → 把它編進 tool_call id 帶出去,下一輪再從 id 還原。
                _sig = p.get("thoughtSignature") or p.get("thought_signature")
                _id = ("ts:" + _sig) if _sig else ("call_%d" % i)
                tool_calls.append({"id": _id, "type": "function",
                                   "function": {"name": fc.get("name"),
                                                "arguments": _j.dumps(fc.get("args") or {}, ensure_ascii=False)}})
    msg = {"role": "assistant", "content": text or None}
    finish = "stop"
    if tool_calls:
        msg["tool_calls"] = tool_calls
        finish = "tool_calls"
    return msg, finish


async def _try_native_openai(payload, want_stream, model, budget):
    """把 openai chat 請求改走原生端點（拿到小思考預算 → 又快又會路由）。失敗回 None → 回退原 openai 路徑。"""
    import json as _j
    try:
        # 【多模型自動容錯】免費版過載超善變(同模型幾分鐘前好端端、現在全503),所以依序試多個模型,
        # 用第一個通的。主模型帶原本思考預算+簽章;備援模型用 budget 0(求快)並清掉前一個的簽章。
        # 全部過載才回 None → 呼叫處退本機 Ollama(永不503)。
        _try_models = [model, "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash"]
        rjson = None
        model_used = model
        _seen = set()
        for _i, _m in enumerate(_try_models):
            if not _m or _m in _seen:
                continue
            _seen.add(_m)
            if _i == 0:
                _nb = _oai_to_native(payload, budget)
            else:
                _nb = _oai_to_native(payload, 0)            # 備援不思考求快
                for _c in _nb.get("contents", []):
                    for _p in _c.get("parts", []):
                        _p.pop("thoughtSignature", None)    # 換模型→清前一個模型的簽章
            try:
                key_index, api_key = _next_key()
                r = await _client.post(
                    f"{UPSTREAM}/v1beta/models/{_m}:generateContent",
                    content=_j.dumps(_nb).encode(),
                    headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                    timeout=(7.0 if _i == 0 else 6.0))
            except Exception:
                continue                                    # 超時/連線錯 → 試下一個模型
            if r.status_code == 200:
                rjson = r.json()
                model_used = _m
                if _i > 0:
                    print(f"ℹ️ [proxy] {model} 過載→改用 {_m}")
                break
            if r.status_code in ROTATE_ON:
                with _km_lock:
                    _km.report_error(r.status_code, key_index)
        if rjson is None:
            return None                                     # 全模型過載 → 呼叫處退 Ollama
        model = model_used
        msg, finish = _native_to_oai(rjson, model)
        if not want_stream:
            data = {"id": "native", "object": "chat.completion", "created": 0, "model": model,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
            return Response(content=_j.dumps(data).encode(), media_type="application/json")
        chunk = {"id": "native", "object": "chat.completion.chunk", "created": 0, "model": model,
                 "choices": [{"index": 0, "delta": msg, "finish_reason": finish}]}

        async def _sse():
            yield ("data: " + _j.dumps(chunk) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return StreamingResponse(_sse(), media_type="text/event-stream")
    except Exception as e:
        print(f"⚠️ [proxy] 原生轉譯失敗,回退 openai: {e}")
        return None


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(path: str, request: Request):
    body = await request.body()
    # 本機模型：使用者選了本機 Ollama 模型 → 對話請求直接路由到本機（不送 Gemini）。
    # 視覺/ASR 等非對話路徑本機模型做不到 → 仍走 Gemini。
    if (_is_local_model(_model_override) and "/openai/" in path
            and "chat/completions" in path):
        fb = await _ollama_fallback(path, body, _model_override, keep_tools=True)
        if fb is not None:
            return fb
    # 免費中轉模型（GLM/Kimi/Step/DeepSeek）：使用者在 dashboard 選了 → chat 請求轉給中轉端點（保留串流才快）。
    if (_is_relay_model(_model_override) and "/openai/" in path
            and "chat/completions" in path):
        gf = await _glm_forward(path, body, model=_model_override)
        if gf is not None:
            return gf
    # 對話模型切換：把 openai 對話請求的 model 改寫成 dashboard 選的(_model_override)。
    # 【改成對所有 gemini/gemma 都生效】之前只改 gemini-2.5-flash → dashboard 選別的不會套用。
    # 現在不管 hermes-agent 送什麼 gemini 模型，一律換成使用者在 dashboard 選的那個。
    if body and "/openai/" in path and _model_override and _model_override != _DEFAULT_MODEL:
        try:
            import json as _mj
            _mb = _mj.loads(body)
            _cur = str(_mb.get("model", ""))
            if ("gemini" in _cur or "gemma" in _cur) and _cur != _model_override:
                _mb["model"] = _model_override
                body = _mj.dumps(_mb).encode()
        except Exception:
            pass
    # Hermes 加速：gemini-2.5 系列預設開「thinking」會先內部推理才回字（對話延遲 ~6s）。
    # 對話/視覺/ASR 不需要每次深度推理 → 自動關掉 thinking，回應從 6s 降到 ~1s。
    if body and ("chat/completions" in path or "generateContent" in path or "openai" in path):
        try:
            import json as _tj
            _tb = _tj.loads(body)
            # openai 風格 = 有 messages（不靠 path 的斜線，避免前綴差異漏判）
            _is_openai = ("messages" in _tb) or ("chat/completions" in path)
            # 【相容性修復】hermes-agent 會送 thinking_config（Gemini 原生欄位），但 Gemini 的
            # OpenAI 相容端點不認 → 回 400「Unknown name thinking_config」。openai 路徑一律濾掉，
            # thinking 改由下面 reasoning_effort/generationConfig 控制。
            if _is_openai and ("thinking_config" in _tb or "thinkingConfig" in _tb):
                _tb.pop("thinking_config", None)
                _tb.pop("thinkingConfig", None)
                body = _tj.dumps(_tb).encode()
            _model = str(_tb.get("model", "")) + " " + path
            # 只看「最後一句使用者訊息」判斷要不要思考 → 不被系統人格裡的「英文/日文/韓文」字眼誤觸
            _msgs = _tb.get("messages") or _tb.get("contents") or []
            _last = _msgs[-1] if isinstance(_msgs, list) and _msgs else _msgs
            _txt = _tj.dumps(_last, ensure_ascii=False)
            # 要「思考」才會做對的情況：翻譯，以及【計算/統計類】（這類關了思考會路由錯、算錯）。
            # 其餘（閒聊、查帳、設提醒…）關思考求快。只看最後一句使用者訊息判斷。
            _needs_think = any(k in _txt for k in (
                "翻譯", "翻译", "translate", "日文", "英文", "韓文", "法文", "翻成",
                "幾倍", "几倍", "倍", "佔比", "占比", "比例", "平均", "排序", "加總", "加总",
                "哪天", "哪筆", "哪笔", "最多", "最少", "幾個", "几个", "總共", "总共",
                "算一下", "算算", "統計", "统计", "佔", "占"))
            if (("gemini-2.5" in _model) or ("gemini-3" in _model) or "gemini" in path) and "lite" not in _model:
                if _is_openai:
                    # openai 端點只吃 none / 全開（low/medium 會 400）。要思考的就「不設」→ 保留全思考
                    # （路由準、但慢）；其餘設 none（快）。
                    if "reasoning_effort" not in _tb and not _needs_think:
                        _tb["reasoning_effort"] = "none"
                        body = _tj.dumps(_tb).encode()
                else:
                    # 原生端點能給「小額思考預算」→ 計算類用 512（又快又準），其餘 0（最快）。
                    gc = _tb.setdefault("generationConfig", {})
                    if "thinkingConfig" not in gc:
                        gc["thinkingConfig"] = {"thinkingBudget": 512 if _needs_think else 0}
                        body = _tj.dumps(_tb).encode()
        except Exception as _e:
            print(f"⚠️ [proxy] thinking 注入失敗: {_e}")

    # 【thought_signature 400 根治】回覆階段(帶工具歷史)→ 把 functionCall+工具結果攤平成純文字,
    # 移除 tools。這樣沒有需要 thought_signature 的 functionCall part → flash-lite 也不會 400。
    # 放在所有路徑(原生/非原生)之前,兩條路都吃到攤平後的乾淨訊息。
    if body and "chat/completions" in path:
        try:
            import json as _flj
            _fpl = _flj.loads(body)
            if _has_tool_history(_fpl.get("messages") or []):
                _fpl["messages"] = _flatten_tool_history(_fpl["messages"])
                _fpl.pop("tools", None)
                _fpl.pop("tool_choice", None)
                # 【錢不能錯】回覆階段(把工具資料,尤其財務數字,講成口語)改用更穩的 2.5-flash。
                # flash-lite 解析複雜財務長文會捏造金額(實測 86420 vs 正確 492524)。
                # 路由/閒聊(沒攤平的請求)不動,還是用快的 flash-lite。
                _fpl["model"] = "gemini-2.5-flash"
                body = _flj.dumps(_fpl).encode()
        except Exception as _fe:
            print(f"⚠️ [proxy] 攤平工具歷史失敗(不影響): {_fe}")

    # ★語音/聊天的 LLM 呼叫一律改走原生端點 + 小思考預算 → 又快又會路由對；
    #   回覆階段(帶工具歷史)也走原生＝非串流一次回完整 → 中途沒有「流」可被逾時切斷（根治截斷）。
    # （openai 端點關思考會路由錯、全開又要 20 秒；原生端點能給小預算，實測 3.3 秒又對。）
    # 含圖片(看圖,content是清單)或本機模型不走；任何失敗都回退原 openai 路徑，壞不了現有語音。
    if body and "chat/completions" in path and not _is_local_model(_model_override):
        try:
            import json as _nj
            _pl = _nj.loads(body)
            _msgs = _pl.get("messages") or []
            _mdl = str(_pl.get("model", "")) or _DEFAULT_MODEL
            # 純文字才走原生轉譯（含工具歷史 OK）；含圖片(content 是清單)走原路徑。
            # 【帶 tools 的請求一律走 passthrough】native 路徑對「帶工具定義」的 agent 請求會壞掉
            #   (不呼叫工具、變慢 30-70s)。現在語音都走 hermes-agent(每次都帶工具)→ 一律 passthrough，
            #   任何聰明模型(3.5-flash/2.5-flash)都能正常用工具又快。lite 也排除(同理)。
            _has_tools = bool(_pl.get("tools"))
            _all_text = all(isinstance(m.get("content"), (str, type(None))) for m in _msgs)
            if (("gemini-2.5" in _mdl) or ("gemini-3" in _mdl)) and "lite" not in _mdl \
                    and not _has_tools and _msgs and _all_text:
                _last = _nj.dumps(_msgs[-1], ensure_ascii=False)
                if _has_tool_history(_msgs):
                    # 工具跑完後的「回覆階段」：只是把資料講成口語、不必思考 → 0 求快。
                    _bud = 0
                elif any(k in _last for k in (
                    "幾倍", "几倍", "倍", "佔比", "占比", "比例", "平均", "排序", "加總", "加总",
                    "哪天", "哪筆", "哪笔", "最多", "最少", "幾個", "几个", "總共", "总共",
                    "算一下", "算算", "統計", "统计", "翻譯", "翻译", "translate")):
                    _bud = 0      # 全關思考:flash-lite 開思考+工具會要 thought_signature,多輪掉了就400。計算由 calc 工具做不靠模型思考
                else:
                    _bud = 0      # 全關思考:避免 thought_signature 400(flash-lite),也更快。路由靠強人格+工具描述
                _res = await _try_native_openai(_pl, bool(_pl.get("stream")), _mdl, _bud)
                if _res is not None:
                    return _res
                # 【沒上限後備】原生(flash+flash-lite)都掛(過載/額度用完/超時)→ 先退免費中轉
                # step-3.7-flash(強、沒上限、雖慢但不會掛),再退本機 Ollama。這樣 Jarvis 永遠不會因 Gemini 額度用完而掛。
                _rf = await _glm_forward(path, body, model="step-3.7-flash")
                if _rf is not None:
                    print("ℹ️ [proxy] Gemini 過載→退免費中轉 step-3.7-flash(沒上限)")
                    return _rf
                _fb = await _ollama_fallback(path, body, keep_tools=True)
                if _fb is not None:
                    print("ℹ️ [proxy] Gemini+中轉都不行→退本機 Ollama(求快)")
                    return _fb
        except Exception as _e2:
            print(f"⚠️ [proxy] 原生路徑判斷失敗,走原 openai: {_e2}")

    # 最多試 2 把金鑰就放棄(原本試「全部」金鑰 × 重試 × 舊的120s超時 = 卡你 2 分半)。
    max_attempts = min(2, max(1, len(_km.config.get("api_keys", []))))
    last_status = 502
    last_text = b""

    # 【passthrough 過載換模型】flash-lite 走這條(工具才會對)，但這條原本 503 只重試同模型。
    # 加:遇 503 過載就把 body 的 model 換成下一個 fallback 重試(永不因單一模型過載而掛)。
    _fb_models = ["gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash"]
    _model_tried = set()
    if body and "chat/completions" in path:
        try:
            import json as _bmj
            _curm = str(_bmj.loads(body).get("model", ""))
            _model_tried.add(_curm)
        except Exception:
            pass

    def _switch_model():
        """把 body 的 model 換成下一個還沒試過的 fallback；成功回 True。"""
        nonlocal body
        try:
            import json as _smj
            _b = _smj.loads(body)
            for _m in _fb_models:
                if _m not in _model_tried:
                    _b["model"] = _m
                    _model_tried.add(_m)
                    body = _smj.dumps(_b).encode()
                    print(f"ℹ️ [proxy] passthrough 過載→換模型 {_m}")
                    return True
        except Exception:
            pass
        return False

    for attempt in range(max_attempts):
        try:
            key_index, api_key = _next_key()
        except RuntimeError as e:
            # 所有 Gemini key 都掛了 → 試本機 Ollama 後備（僅 OpenAI 相容 chat 路徑）
            fb = await _ollama_fallback(path, body)
            if fb is not None:
                return fb
            return JSONResponse(
                status_code=503,
                content={"error": {"message": str(e), "type": "no_active_key"}},
            )

        url = _build_url(path, request)
        headers = _build_headers(request, api_key, openai_style="/openai/" in path)

        # 同一把金鑰：遇到上游暫時性 5xx 先退避重試幾次，再決定要不要往下走
        resp = None
        for t in range(MAX_5XX_RETRY + 1):
            upstream_req = _client.build_request(
                request.method, url, headers=headers, content=body
            )
            try:
                resp = await _client.send(upstream_req, stream=True)
            except httpx.RequestError as e:
                last_status, last_text, resp = 502, str(e).encode(), None
                break
            if resp.status_code in RETRY_5XX and t < MAX_5XX_RETRY:
                await resp.aread()
                await resp.aclose()
                last_status, last_text = resp.status_code, b""
                await asyncio.sleep(0.8 * (t + 1))
                continue
            break

        if resp is None:
            # 連線層錯誤：先試換模型(過載/連不上)，再換金鑰
            if _switch_model():
                continue
            continue  # 連線層錯誤 → 換下一把

        # 重試後仍 503 過載 → 換模型再試一輪(同一把金鑰，換模型最有效)
        if resp.status_code in RETRY_5XX:
            await resp.aread()
            await resp.aclose()
            last_status, last_text = resp.status_code, b""
            if _switch_model():
                # 重新用新模型送一次（不消耗 attempt 配額：手動再送）
                upstream_req = _client.build_request(request.method, url, headers=headers, content=body)
                try:
                    resp = await _client.send(upstream_req, stream=True)
                except httpx.RequestError:
                    continue
            else:
                continue

        if resp.status_code in ROTATE_ON:
            # 該金鑰失效 / 額度用盡(429) → 標記並換下一把；連最後一把都掛掉時，
            # 不要把 429 回給使用者，而是讓迴圈結束 → 走下面的本機 Ollama 後備。
            await resp.aread()
            await resp.aclose()
            _report(resp.status_code, key_index)  # 精準冷卻「這次真正用的」key
            last_status, last_text = resp.status_code, b""
            continue

        # 成功（或非可輪換錯誤、或最後一次嘗試）：把回應串流回客戶端
        resp_headers = {
            k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP
        }

        async def _stream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            _stream(),
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )

    # 所有金鑰都試過仍失敗（連線錯誤/額度用盡）→ 最後再試一次本機後備
    fb = await _ollama_fallback(path, body)
    if fb is not None:
        return fb
    return Response(
        content=last_text or b'{"error":"all keys exhausted"}',
        status_code=last_status,
        media_type="application/json",
    )


if __name__ == "__main__":
    import uvicorn

    print(f"🔑 [Hermes LLM Proxy] {HOST}:{PORT}  →  {UPSTREAM}")
    print(f"   把 ~/.hermes/config.yaml 的 base_url 指到  http://{HOST}:{PORT}/v1beta")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
