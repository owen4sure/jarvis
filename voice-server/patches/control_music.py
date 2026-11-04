import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

control_music_desc = {
    "type": "function",
    "function": {
        "name": "control_music",
        "description": (
            "控制電腦上正在播放的音樂。當使用者說「暫停音樂／繼續播放／關掉音樂／停止音樂」時呼叫。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "stop"],
                    "description": "pause=暫停或繼續（切換），stop=停止音樂播放。",
                }
            },
            "required": ["action"],
        },
    },
}


@register_function("control_music", control_music_desc, ToolType.SYSTEM_CTL)
def control_music(conn, action: str):
    ep = "pause" if action == "pause" else "stop"
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://host.docker.internal:8810/{ep}", method="POST"), timeout=20)
        msg = "好，暫停一下～" if action == "pause" else "好，關掉囉"
        return ActionResponse(action=Action.RESPONSE, result="ok", response=msg)
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e),
                              response="控制音樂時電腦沒回應")
