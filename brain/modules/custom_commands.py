"""
客製化指令（硬觸發）：使用者自訂「說 X → 做 Y」的規則。
跟記憶不同——記憶是「固定事實」，這裡是「觸發→動作」的指令。
比對到觸發詞就【直接執行動作】，不靠 LLM 注意到某條記憶（那會時靈時不靈）。
"""
import json
import os
import urllib.request

_CFG = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                    "config", "custom_commands.json")
MUSIC_URL = "http://127.0.0.1:8810/play"


def load():
    try:
        with open(_CFG, encoding="utf-8") as f:
            return json.load(f).get("commands", [])
    except Exception:
        return []


def match(text):
    """訊息是否觸發某條客製化指令。短訊息含觸發詞才算（避免長句裡剛好提到就誤觸）。"""
    t = (text or "").strip().strip("！。.!?？ ")
    if not t:
        return None
    for c in load():
        trig = str(c.get("trigger", "")).strip()
        if not trig:
            continue
        # 訊息就是觸發詞、或很短且含觸發詞（如「開工」「開工了」「開工囉」）
        if t == trig or (trig in t and len(t) <= len(trig) + 4):
            return c
    return None


def execute(cmd):
    """執行客製化指令的動作。回傳要回給使用者的話；失敗回 None（讓上層走一般流程）。"""
    if not cmd:
        return None
    action = cmd.get("action")
    if action == "play_music":
        try:
            q = (cmd.get("params") or {}).get("query", "")
            req = urllib.request.Request(
                MUSIC_URL, data=json.dumps({"query": q}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=35)
            return cmd.get("reply") or f"好，幫你放「{q}」～"
        except Exception:
            return None
    # 之後可擴充其他 action（do_on_computer、set_reminder…）
    return None


def rules_text():
    """把所有客製化指令整理成一段給 LLM 看的明確規則（語音/Telegram 注入用）。"""
    cmds = load()
    if not cmds:
        return ""
    lines = ["【客製化指令｜使用者自訂的硬規則，命中就照做、別問】"]
    for c in cmds:
        act = c.get("action")
        if act == "play_music":
            q = (c.get("params") or {}).get("query", "")
            lines.append(f"・使用者說「{c.get('trigger')}」→ 立刻播放「{q}」"
                         + (f"（{c.get('note')}）" if c.get("note") else ""))
    return "\n".join(lines)
