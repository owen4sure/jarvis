import json
import urllib.request

from plugins_func.register import register_function, ToolType, ActionResponse, Action

play_music_pc_desc = {
    "type": "function",
    "function": {
        "name": "play_music_on_computer",
        "description": (
            "當使用者想聽音樂、歌曲，或叫你放歌、播某首歌/某歌手時呼叫。"
            "會在使用者電腦上播放 YouTube 音樂。"
            "例如「放周杰倫的歌」「我想聽稻香」。如果使用者只說「來個金曲/隨便放/放首好聽的」沒指定,你就自己挑一首知名華語名曲填進 query(例如「周杰倫 七里香」「五月天 倔強」「鄧紫棋 泡沫」「告五人 愛人錯過」),不要只填「金曲」這種空泛詞。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "歌名/歌手/關鍵字，例如「周杰倫 稻香」「五月天 溫柔」「lofi 輕音樂」。",
                }
            },
            "required": ["query"],
        },
    },
}


@register_function("play_music_on_computer", play_music_pc_desc, ToolType.SYSTEM_CTL)
def play_music_on_computer(conn, query: str):
    try:
        req = urllib.request.Request(
            "http://host.docker.internal:8810/play",
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=35)
        return ActionResponse(
            action=Action.RESPONSE, result="ok",
            response=f"好，幫你在電腦上放{query}～",
        )
    except Exception as e:
        return ActionResponse(
            action=Action.RESPONSE, result=str(e),
            response="放音樂時電腦那邊沒回應，等一下再試喔",
        )
