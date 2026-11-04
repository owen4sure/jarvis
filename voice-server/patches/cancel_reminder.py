import json
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

cancel_reminder_desc = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": (
            "取消／刪掉一個已經設好的提醒。當使用者說「取消X的提醒」「不用提醒我X了」"
            "「把那個提醒刪掉」「那個提醒不要了」之類，呼叫這個工具。"
            "query 填那個提醒的關鍵字（例如「開會」「吃藥」「面試」）；"
            "如果使用者只說「取消提醒」沒指定哪個，query 可以留空，系統會在只有一個提醒時直接取消。"
            "（這是刪提醒；要『新設』提醒是 set_reminder。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "要取消的提醒關鍵字，例如「開會」「吃藥」。沒指定可留空。"},
            },
            "required": [],
        },
    },
}


@register_function("cancel_reminder", cancel_reminder_desc, ToolType.SYSTEM_CTL)
def cancel_reminder(conn, query: str = ""):
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            "http://host.docker.internal:8809/reminder_cancel",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"}), timeout=10).read().decode("utf-8"))
        if d.get("ok"):
            return ActionResponse(action=Action.REQLLM,
                                  result=(f"（已取消提醒「{d.get('cancelled')}」。"
                                          f"自然跟使用者說一聲取消好了即可。）"), response=None)
        return ActionResponse(action=Action.REQLLM,
                              result=f"（{d.get('reason', '找不到相符的提醒')}。照這個自然跟使用者說，請他講清楚是哪一個。）",
                              response=None)
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e),
                              response="取消提醒時系統沒回應，等等再說一次")
