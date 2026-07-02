"""
notify — presence-aware 輸出：StackChan 在旁邊就用講的，不在就靠文字
============================================================================
解決「StackChan 不在旁邊」：任何主動訊息（提醒、簡報、警示）一律送 Telegram
（你人在外面也看得到），若實體裝置正連著 gateway，就「同時」在裝置上唸出來。
裝置不在 → 安靜略過，不會卡住、不會報錯。
"""
import json

_robot = None


def _client():
    global _robot
    if _robot is None:
        from .stackchan_mcp_client import StackChanClient
        _robot = StackChanClient()
    return _robot


def robot_present() -> bool:
    """實體 StackChan 是否正連著 gateway。"""
    try:
        st = _client().get_status()
        blob = json.dumps(st.get("result", st))
        return '"connected": true' in blob or '"connected":true' in blob
    except Exception:
        return False


def speak_if_present(text: str) -> bool:
    """把要講的話丟進 8809 待播語音佇列 → 已連線的 StackChan(xiaozhi WebSocket)會每隔幾秒來撈、
    用 TTS 主動開口講出來。回傳是否成功入列。
    （舊版走 stackchan-mcp gateway 的 robot_present()/say() 對 WebSocket 裝置無效，故改走佇列。）"""
    try:
        import urllib.request as _u
        import json as _j
        data = _j.dumps({"text": text}).encode()
        r = _u.urlopen(_u.Request(
            "http://127.0.0.1:8809/push_voice", data=data,
            headers={"Content-Type": "application/json"}), timeout=4)
        return bool(_j.loads(r.read()).get("ok"))
    except Exception:
        return False
