"""
trivia — 多人現場問答遊戲（StackChan 當主持人）
============================================================================
玩法：機器人出題 → 轉頭看著某位玩家 → 聽他口頭回答 → AI 判對錯 → 計分 +
表情/彩燈反應 → 換下一位。全部跑完宣布冠軍、放彩燈慶祝。

題目與評分都用 Gemini（共用金鑰輪換 + 本機後備），所以離線也能玩。
"""
from .host import assign_seats, ConsoleIO


def _gen_question(gemini, topic, used):
    prompt = (
        f"出一題「{topic}」的問答題，適合現場朋友口頭搶答。"
        f"避免和這些重複：{('、'.join(used))[:200]}。"
        "回 JSON：{\"question\":\"題目\",\"answer\":\"簡短標準答案\"}。"
        "題目要口語、一句話、有明確答案。"
    )
    data = gemini.generate_json(prompt, fallback={"question": "台灣最高的山是哪一座？", "answer": "玉山"})
    return data.get("question", "").strip(), data.get("answer", "").strip()


def _judge(gemini, question, correct, answer):
    if not answer:
        return False
    # 寬鬆語意判斷（口語、近似都算對）
    prompt = (
        f"題目：{question}\n標準答案：{correct}\n玩家回答：{answer}\n"
        "玩家回答在語意上是否正確（口語、近似、同義都算對）？"
        "回 JSON：{\"correct\": true 或 false}"
    )
    data = gemini.generate_json(prompt, fallback={"correct": False})
    return bool(data.get("correct"))


def play(gemini, player_names, io=None, rounds_per_player=2, topic="綜合常識"):
    """主持一場多人問答。io 預設為 ConsoleIO（終端機）；真機器人傳 RobotIO。"""
    io = io or ConsoleIO()
    players = assign_seats(player_names)
    io.announce(f"歡迎來到 Hermes 問答挑戰！今天的主題是「{topic}」，"
                f"共 {len(players)} 位玩家，每人 {rounds_per_player} 題，準備好了嗎？", emotion="excited")
    io.react("party")

    used = []
    total_rounds = rounds_per_player * len(players)
    for rnd in range(total_rounds):
        player = players[rnd % len(players)]
        q, ans = _gen_question(gemini, topic, used)
        used.append(q)

        io.look_at(player)
        io.announce(f"第 {rnd + 1} 題，{player.name} 請聽題：{q}", emotion="thinking")
        reply = io.ask(player, prompt=None, timeout_ms=7000)

        if _judge(gemini, q, ans, reply):
            player.score += 1
            io.announce(f"答對了！{player.name} 得一分！", emotion="happy")
            io.react("correct")
        else:
            io.announce(f"可惜～正確答案是「{ans}」。", emotion="sad")
            io.react("wrong")
        io.scoreboard(players)

    # 結算
    top = max(p.score for p in players)
    winners = [p.name for p in players if p.score == top]
    io.react("win")
    if len(winners) == 1:
        io.announce(f"遊戲結束！冠軍是 {winners[0]}，拿下 {top} 分，太厲害了！", emotion="excited")
    else:
        io.announce(f"遊戲結束！{('、'.join(winners))} 同分 {top} 分，並列冠軍！", emotion="excited")
    io.react("party")
    return {p.name: p.score for p in players}
