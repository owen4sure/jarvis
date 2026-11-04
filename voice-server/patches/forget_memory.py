import json
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

forget_memory_desc = {
    "type": "function",
    "function": {
        "name": "forget_memory",
        "description": (
            "把一筆『記錯的、過時的、使用者要你忘掉』的長期記憶刪掉。"
            "當使用者說「忘記X」「把X忘掉」「那個你記錯了」「我沒有說過X」「我沒有X」"
            "「刪掉關於X的記憶」「X已經不是了」之類，呼叫這個工具修正記憶。"
            "query 填使用者要你忘記的那件事的關鍵描述（例如「養貓」「喜歡吃壽司」「下個月去日本」）。"
            "系統會語意比對找出最相符的那筆記憶刪掉。"
            "（這是修正記憶用的；如果只是要『新增』記憶，那是 remember_fact。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要忘記／刪掉的記憶關鍵描述，例如「養貓」「愛吃壽司」「在台北上班」。",
                }
            },
            "required": ["query"],
        },
    },
}


@register_function("forget_memory", forget_memory_desc, ToolType.SYSTEM_CTL)
def forget_memory(conn, query: str):
    try:
        req = urllib.request.Request(
            "http://host.docker.internal:8809/forget",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        if d.get("ok"):
            return ActionResponse(
                action=Action.REQLLM,
                result=(f"（已經把記憶「{d.get('forgot')}」忘掉了。"
                        f"自然地跟使用者確認一聲就好，例如「好，我把那個忘掉了」。）"),
                response=None,
            )
        return ActionResponse(
            action=Action.REQLLM,
            result=("（記憶庫裡找不到夠相符的那筆，沒刪。請跟使用者說你不太確定是哪一條，"
                    "請他講清楚一點是哪件事要忘記。）"),
            response=None,
        )
    except Exception as e:
        return ActionResponse(
            action=Action.RESPONSE, result=str(e),
            response="記憶系統剛剛沒回應，等等再跟我說一次要忘記什麼",
        )
