"""
llm_fallback — Gemini 全掛掉時的本機後備 + 統一的「大腦連不上」訊號
============================================================================
策略（由上到下）：
  1. Gemini（主力，金鑰輪換）— 由 gemini_client 處理。
  2. 本機 Ollama（若有抓過模型就自動啟用）— 完全離線也能簡單回應。
  3. 都不行 → 丟 BrainUnavailable，讓上層改用「友善訊息 + 稍後重試佇列」。

要啟用離線後備：`ollama pull qwen2.5:3b`（或任何小模型）即可，本模組會自動偵測。
"""
import json
import os

import requests

_CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "config", "stackchan.json")


def _ollama_url() -> str:
    """後備模型伺服器網址。優先順序：環境變數 > 設定檔 ollama_url > 本機。
    要接公司 Mac Mini：把 config/stackchan.json 的 "ollama_url" 設成
    例如 http://100.x.x.x:11434（Tailscale IP）即可。"""
    if os.environ.get("OLLAMA_URL"):
        return os.environ["OLLAMA_URL"]
    try:
        with open(_CFG, encoding="utf-8") as f:
            u = json.load(f).get("ollama_url")
            if u:
                return u
    except Exception:
        pass
    return "http://localhost:11434"


OLLAMA_URL = _ollama_url()
_CACHED_MODEL = None  # 偵測結果快取（None 表示尚未測 / 無）


class BrainUnavailable(Exception):
    """所有 LLM 後端都無法回應（Gemini 掛 + 無本機後備）。"""


def ollama_model(refresh: bool = False):
    """回傳一個可用的 Ollama 模型名稱，沒有就回 None。"""
    global _CACHED_MODEL
    if _CACHED_MODEL is not None and not refresh:
        return _CACHED_MODEL or None
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        models = [m["name"] for m in r.json().get("models", [])]
        _CACHED_MODEL = models[0] if models else ""
    except Exception:
        _CACHED_MODEL = ""
    return _CACHED_MODEL or None


def _contents_to_prompt(contents) -> str:
    """把 gemini 的 contents（字串 + Part 混合）攤平成純文字 prompt。
    非字串（圖片/音訊 Part）會被略過——本機文字模型吃不下。"""
    parts = [c for c in contents if isinstance(c, str)]
    return "\n\n".join(parts).strip()


def ollama_generate(contents) -> str:
    """用 Ollama（本機或遠端）產生回覆。沒有模型或失敗就丟 BrainUnavailable。"""
    model = ollama_model()
    if not model:
        raise BrainUnavailable("no ollama model")
    prompt = _contents_to_prompt(contents)
    if not prompt:
        raise BrainUnavailable("nothing text to send to fallback")
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=90,
        )
        r.raise_for_status()
        text = (r.json().get("response") or "").strip()
        if not text:
            raise BrainUnavailable("empty ollama response")
        return f"（後備模型）{text}"
    except BrainUnavailable:
        raise
    except Exception as e:
        raise BrainUnavailable(f"ollama failed: {e}")


def _n8n_cfg():
    """回 (webhook_url, token)；沒設定回 (None, None)。
    在 config/stackchan.json 設 "n8n_brain_url"（與選用的 "n8n_brain_token"）。"""
    try:
        with open(_CFG, encoding="utf-8") as f:
            c = json.load(f)
            return c.get("n8n_brain_url"), c.get("n8n_brain_token")
    except Exception:
        return None, None


def n8n_generate(contents) -> str:
    """打 n8n 的 Webhook，讓 n8n 用它接好的本地模型回覆。
    n8n 工作流請回傳純文字，或 JSON 含 text/response/output/answer/reply 任一欄。"""
    url, token = _n8n_cfg()
    if not url:
        raise BrainUnavailable("no n8n_brain_url configured")
    prompt = _contents_to_prompt(contents)
    if not prompt:
        raise BrainUnavailable("nothing text to send to fallback")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(url, json={"prompt": prompt}, headers=headers, timeout=120)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            data = r.json()
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                for k in ("text", "response", "output", "answer", "reply", "content"):
                    if data.get(k):
                        return f"（後備·n8n）{str(data[k]).strip()}"
            text = str(data).strip()
        else:
            text = r.text.strip()
        if not text:
            raise BrainUnavailable("empty n8n response")
        return f"（後備·n8n）{text}"
    except BrainUnavailable:
        raise
    except Exception as e:
        raise BrainUnavailable(f"n8n failed: {e}")


def fallback_generate(contents) -> str:
    """統一後備：先試 n8n webhook（若有設），再試 ollama（本機/遠端）。
    全都不行就丟 BrainUnavailable，讓上層走『友善訊息 + 排隊重試』。"""
    last = None
    for fn in (n8n_generate, ollama_generate):
        try:
            return fn(contents)
        except BrainUnavailable as e:
            last = e
    raise BrainUnavailable(str(last) if last else "no fallback configured")
