"""
StackChan MCP Client — 給 Hermes_Brain (Python 3.9) 用的輕量機器人控制客戶端
============================================================================
stackchan-mcp 的 Streamable HTTP MCP (JSON-RPC) 客戶端，只用 `requests`，
不依賴 mcp SDK（那需要 Python 3.10+，而 Hermes_Brain 是 3.9）。

讓 Hermes_Brain 的語音/技能路徑能直接驅動實體機器人：
    c = StackChanClient()
    c.say("你好")
    c.set_avatar("happy")
    c.set_all_leds(0, 255, 0)

設計原則：
  - 連線/握手失敗或裝置未連線時「不丟例外打斷大腦」，回傳 dict 帶 error，
    讓呼叫端自行決定要不要忽略（家裡沒插電也不該讓助理崩潰）。
  - 每次呼叫自動處理 initialize → session-id → tools/call。
"""
import json
import os
import threading
import uuid

import requests

_CFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "stackchan.json",
)


def _load_cfg() -> dict:
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_jsonrpc(resp: requests.Response) -> dict:
    """回應可能是純 JSON，也可能是 SSE (text/event-stream)；兩者都處理。"""
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


class StackChanClient:
    def __init__(self, base_url: str = None, token: str = None, timeout: float = 20.0):
        cfg = _load_cfg()
        host = cfg.get("mcp_http_host", "127.0.0.1")
        port = cfg.get("mcp_http_port", 8767)
        self.base_url = base_url or f"http://{host}:{port}/mcp"
        self.token = token or cfg.get("token", "")
        self.timeout = timeout
        self._session_id = None
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _ensure_session(self) -> None:
        if self._session_id:
            return
        init = {
            "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "hermes-brain", "version": "1.0"},
            },
        }
        r = requests.post(self.base_url, headers=self._headers(),
                          data=json.dumps(init), timeout=self.timeout)
        r.raise_for_status()
        self._session_id = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        # 完成握手
        requests.post(self.base_url, headers=self._headers(),
                      data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                      timeout=self.timeout)

    def call_tool(self, name: str, arguments: dict = None) -> dict:
        """呼叫一個機器人工具。回傳 {'ok': bool, 'result'/'error': ...}。"""
        with self._lock:
            try:
                self._ensure_session()
                req = {
                    "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
                r = requests.post(self.base_url, headers=self._headers(),
                                  data=json.dumps(req), timeout=self.timeout)
                if r.status_code == 404:
                    # session 過期 → 重新握手一次
                    self._session_id = None
                    self._ensure_session()
                    r = requests.post(self.base_url, headers=self._headers(),
                                      data=json.dumps(req), timeout=self.timeout)
                data = _parse_jsonrpc(r)
                if "error" in data:
                    return {"ok": False, "error": data["error"]}
                return {"ok": True, "result": data.get("result", {})}
            except Exception as e:
                return {"ok": False, "error": repr(e)}

    def list_tools(self) -> list:
        with self._lock:
            self._ensure_session()
            req = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/list", "params": {}}
            r = requests.post(self.base_url, headers=self._headers(),
                              data=json.dumps(req), timeout=self.timeout)
            data = _parse_jsonrpc(r)
            return [t["name"] for t in data.get("result", {}).get("tools", [])]

    # ── 便利方法 ───────────────────────────────────────────────
    def say(self, text: str, voice: str = "macsay") -> dict:
        # 預設用 macOS 中文 TTS 引擎（macsay）；繁中自然、免架伺服器
        return self.call_tool("say", {"text": text, "voice": voice})

    def set_avatar(self, face: str) -> dict:
        return self.call_tool("set_avatar", {"face": face})

    def move_head(self, yaw: float, pitch: float, speed: int = 50) -> dict:
        return self.call_tool("move_head", {"yaw": yaw, "pitch": pitch, "speed": speed})

    def set_all_leds(self, r: int, g: int, b: int) -> dict:
        return self.call_tool("set_all_leds", {"r": r, "g": g, "b": b})

    def clear_leds(self) -> dict:
        return self.call_tool("clear_leds", {})

    def get_status(self) -> dict:
        return self.call_tool("get_status", {})


if __name__ == "__main__":
    c = StackChanClient()
    print("tools:", c.list_tools()[:8], "...")
    print("get_status:", json.dumps(c.get_status(), ensure_ascii=False)[:300])
    print("say (no device → expect graceful error):",
          json.dumps(c.say("測試"), ensure_ascii=False)[:300])
