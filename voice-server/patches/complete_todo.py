import json
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

complete_todo_desc = {
    "type": "function",
    "function": {
        "name": "complete_todo",
        "description": (
            "把一個待辦事項標記完成／劃掉。當使用者說「X做完了」「完成X」「X搞定了」"
            "「把待辦X劃掉」「那個待辦好了」之類，呼叫這個工具。"
            "query 填那個待辦的關鍵字（例如「倒垃圾」「交報告」「買牛奶」）。"
            "系統會在待辦清單裡找最相符的劃掉。"
            "（這是完成/刪待辦；要『新增待辦』是 add_todo，要『看待辦』是 list_todo。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "做完的待辦關鍵字，例如「倒垃圾」「交報告」。"},
            },
            "required": ["query"],
        },
    },
}


@register_function("complete_todo", complete_todo_desc, ToolType.SYSTEM_CTL)
def complete_todo(conn, query: str):
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            "http://host.docker.internal:8809/todo_complete",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"}), timeout=10).read().decode("utf-8"))
        if d.get("ok"):
            return ActionResponse(action=Action.REQLLM,
                                  result=(f"（已把待辦「{d.get('done')}」標記完成劃掉。"
                                          f"自然給使用者一句鼓勵/確認即可。）"), response=None)
        return ActionResponse(action=Action.REQLLM,
                              result=f"（{d.get('reason', '待辦清單裡找不到相符的')}。照這個自然跟使用者說，請他講清楚是哪一項。）",
                              response=None)
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e),
                              response="標記待辦時系統沒回應，等等再說一次")
