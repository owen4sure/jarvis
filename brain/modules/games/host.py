"""
host — 遊戲主持的 I/O 層 + 計分 + 多人座位
============================================================================
GameIO 抽象掉「主持人怎麼說話/怎麼聽答案/怎麼看著某個玩家」：
  - RobotIO：真的用 StackChan（expressive_say + listen + move_head 轉頭看人 + LED）
  - ConsoleIO：用 print/input（沒硬體時也能在終端機玩、測流程）

這樣同一套遊戲邏輯，硬體到貨用機器人玩，現在用鍵盤就能測。
"""
import random
import time


class Player:
    def __init__(self, name, seat_yaw=0):
        self.name = name
        self.seat_yaw = seat_yaw   # 機器人轉頭看這個玩家的角度
        self.score = 0


def assign_seats(names):
    """把玩家平均分配到 -40~40 度，讓機器人轉頭「看」不同人。"""
    n = len(names)
    if n == 1:
        return [Player(names[0], 0)]
    span = 80
    return [Player(nm, int(-40 + span * i / (n - 1))) for i, nm in enumerate(names)]


class GameIO:
    def announce(self, text, emotion=None): raise NotImplementedError
    def look_at(self, player): pass
    def ask(self, player, prompt, timeout_ms=6000): raise NotImplementedError
    def react(self, kind): pass          # kind: correct/wrong/win/think/party
    def scoreboard(self, players): pass


class ConsoleIO(GameIO):
    """終端機版：沒硬體也能玩/測。answers 可預先塞模擬答案做自動測試。"""
    def __init__(self, scripted_answers=None):
        self._scripted = list(scripted_answers or [])

    def announce(self, text, emotion=None):
        tag = f"[{emotion}] " if emotion else ""
        print(f"🤖 {tag}{text}")

    def look_at(self, player):
        print(f"   （轉頭看向 {player.name}）")

    def ask(self, player, prompt, timeout_ms=6000):
        if self._scripted:
            ans = self._scripted.pop(0)
            print(f"🙋 {player.name}: {ans}")
            return ans
        try:
            return input(f"🙋 {player.name} 回答> ").strip()
        except EOFError:
            return ""

    def react(self, kind):
        print({"correct": "   ✅ 叮咚！",
               "wrong": "   ❌ 嗶——",
               "win": "   🎉🎉 恭喜獲勝！",
               "party": "   🌈（彩燈閃爍）",
               "think": "   🤔（思考中）"}.get(kind, ""))

    def scoreboard(self, players):
        s = "  ".join(f"{p.name}:{p.score}" for p in players)
        print(f"📊 目前比分 → {s}")


class RobotIO(GameIO):
    """真機器人版：說話帶表情、轉頭看人、聽答案、彩燈反應。"""
    def __init__(self, robot=None, gemini=None):
        from modules.embodied.stackchan_mcp_client import StackChanClient
        self.robot = robot or StackChanClient()
        self.gemini = gemini

    def announce(self, text, emotion=None):
        from modules.embodied.expression import expressive_say
        expressive_say(self.robot, text, emotion=emotion)

    def look_at(self, player):
        try:
            self.robot.move_head(player.seat_yaw, 50, speed=40)
        except Exception:
            pass

    def ask(self, player, prompt, timeout_ms=6000):
        self.look_at(player)
        if prompt:
            self.announce(prompt)
        try:
            r = self.robot.call_tool("listen", {"duration_ms": timeout_ms,
                                                "motion": "face-only", "language": "zh"})
            res = r.get("result", {})
            # listen 回傳結構依 gateway 而定，盡量抓出文字
            content = res.get("content") if isinstance(res, dict) else None
            if content and isinstance(content, list):
                import json
                for c in content:
                    txt = c.get("text", "")
                    try:
                        j = json.loads(txt)
                        if isinstance(j, dict) and j.get("text"):
                            return j["text"]
                    except Exception:
                        if txt:
                            return txt
            return ""
        except Exception:
            return ""

    def react(self, kind):
        led = {"correct": (0, 220, 0), "wrong": (220, 0, 0),
               "win": (255, 180, 0), "party": None, "think": (0, 160, 160)}
        try:
            if kind == "party":
                for _ in range(3):
                    self.robot.set_all_leds(random.randint(0,255), random.randint(0,255), random.randint(0,255))
                    time.sleep(0.25)
            elif kind in led:
                r, g, b = led[kind]
                self.robot.set_all_leds(r, g, b)
            face = {"correct": "happy", "wrong": "sad", "win": "happy",
                    "party": "happy", "think": "thinking"}.get(kind)
            if face:
                self.robot.set_avatar(face)
        except Exception:
            pass

    def scoreboard(self, players):
        s = "，".join(f"{p.name} {p.score} 分" for p in players)
        self.announce(f"目前比分：{s}")
