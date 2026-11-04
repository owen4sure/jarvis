import json
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

delete_expense_desc = {
    "type": "function",
    "function": {
        "name": "delete_expense",
        "description": (
            "刪掉一筆記錯的花費。當使用者說「刪掉那筆X的花費」「剛剛那筆X記錯了」"
            "「那筆X不對，刪掉」「我沒有花那筆X」之類，呼叫這個工具修正花費紀錄。"
            "query 填那筆花費的關鍵字（品項、分類或金額，例如「咖啡」「晚餐」「150」）。"
            "系統會在最近的花費裡找最相符的那筆刪掉。"
            "（這是刪花費；要『記一筆新花費』是 add_expense。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "要刪掉的花費關鍵字：品項/分類/金額，例如「咖啡」「晚餐」「150」。"},
            },
            "required": ["query"],
        },
    },
}


@register_function("delete_expense", delete_expense_desc, ToolType.SYSTEM_CTL)
def delete_expense(conn, query: str):
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            "http://host.docker.internal:8809/expense_delete",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"}), timeout=10).read().decode("utf-8"))
        if d.get("ok"):
            return ActionResponse(action=Action.REQLLM,
                                  result=(f"（已刪掉花費「{d.get('deleted')}」。"
                                          f"自然跟使用者確認刪好了即可。）"), response=None)
        return ActionResponse(action=Action.REQLLM,
                              result=f"（{d.get('reason', '最近花費裡找不到相符的')}。照這個自然跟使用者說，請他講清楚是哪一筆。）",
                              response=None)
    except Exception as e:
        return ActionResponse(action=Action.RESPONSE, result=str(e),
                              response="刪花費時系統沒回應，等等再說一次")
