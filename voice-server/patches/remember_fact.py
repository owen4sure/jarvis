import json
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

remember_fact_desc = {
    "type": "function",
    "function": {
        "name": "remember_fact",
        "description": (
            "把一件值得長期記住的事寫進 Owen 的長期記憶。"
            "【要主動呼叫——不需要他說「記住」】只要你在對話中得知這類資訊就自己存："
            "他的穩定偏好（喜歡/討厭什麼、習慣）、計畫與目標（要去哪、想做什麼、報名了什麼）、"
            "重要事件與日期（面試、考試、約會、生日、紀念日、看醫生）、重要決定、"
            "人際關係（家人/朋友/同事/寵物的名字與事）、工作與生活的重要事實、他反覆提到或很在意的事。"
            "當然他明確說「記住…」「別忘了…」時更要存。"
            "【判準】這件事過幾週後可能還用得到、或能讓你更懂他 → 就存。"
            "【不要存】純粹當下會過去的情緒或狀態（「我好累」「現在有點餓」「今天好熱」）、"
            "無意義的閒聊口水、你不確定重不重要的瑣事、他只是在『問』你記不記得（疑問句）。"
            "原則：陪聊歸陪聊（不用存），但一聽到真正重要、之後用得到的資訊，就默默幫他記下來。"
            "可以一次對話存多筆不同的事。"
            "【鐵律·只記真的】只能存使用者『親口說過的確切事實』，原話照記、不要加油添醋或推測；"
            "你不確定、聽不清楚、或只是自己腦補的，一律不要存（捏造假記憶會害慘他，絕對禁止）。"
            "【時間一律轉絕對日期】fact 裡不要寫『下個月/下週/明天』這種相對時間（過陣子會被讀錯）。"
            "用系統給你的今天日期換算成絕對寫法，例如今天 2026-06-28，"
            "『下個月要去日本』就寫成『Owen 2026 年 7 月要去日本京都玩』。"
            "【有時效的事就設 expire_date】旅行、約會、考試、回診、演唱會這種『過了就沒意義』的事件，"
            "把 expire_date 設成它過期的日期（事件當天或之後幾天，格式 YYYY-MM-DD），系統到期會自動清掉、不會永遠留著。"
            "長期不變的事（偏好、習慣、生日、人際關係、工作）不要設 expire_date，永久保留。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "要記住的事實，用完整一句話、含主詞、時間用絕對日期，照使用者原話記，例如「Owen 2026 年 7 月要去日本京都玩」「Owen 2026-06-30 要回診牙醫」「Owen 報名了多益考試」。",
                },
                "expire_date": {
                    "type": "string",
                    "description": "選填。只有『有時效會過期的事件』(旅行/約會/考試/回診)才設，格式 YYYY-MM-DD，設成事件過期的日期，到期系統會自動清掉。長期不變的事(偏好/生日/人際)不要設、留空。",
                },
            },
            "required": ["fact"],
        },
    },
}


@register_function("remember_fact", remember_fact_desc, ToolType.SYSTEM_CTL)
def remember_fact(conn, fact: str, expire_date: str = ""):
    try:
        payload = {"fact": fact}
        if expire_date:
            payload["expire"] = expire_date
        req = urllib.request.Request(
            "http://host.docker.internal:8809/remember",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        # 用 REQLLM 讓模型自然接話：明確叫你記的就簡短確認，聊天中主動記的就自然帶過、別打斷。
        return ActionResponse(
            action=Action.REQLLM,
            result=(f"（已把「{fact}」存進長期記憶。若使用者是明確叫你記住，"
                    f"就簡短回一句「好，記下了」；若是聊天中你主動記的，"
                    f"自然接續原本的話題就好、不用特別說「我記住了」。）"),
            response=None,
        )
    except Exception as e:
        return ActionResponse(
            action=Action.REQLLM,
            result=f"（記憶系統沒回應，這次沒存成功：{e}。自然接話即可，別提存取失敗。）",
            response=None,
        )
