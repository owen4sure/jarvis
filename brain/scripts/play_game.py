"""
play_game — 在電腦上或用 StackChan 開一場遊戲
============================================================================
  ./.venv/bin/python -m scripts.play_game trivia 小明 阿華 小美
裝置在線就用機器人主持（語音/轉頭/彩燈），不在就在終端機文字版玩。
"""
import sys

from modules.embodied.gemini_client import GeminiClient
from modules.embodied import notify
from modules.games import trivia
from modules.games.host import RobotIO, ConsoleIO


def main():
    args = sys.argv[1:]
    if args and args[0] in ("trivia", "quiz", "問答"):
        args = args[1:]
    players = args or ["玩家1", "玩家2"]

    gemini = GeminiClient()
    if notify.robot_present():
        print("🤖 偵測到 StackChan，用機器人主持！")
        io = RobotIO(gemini=gemini)
    else:
        print("💻 沒偵測到 StackChan，用終端機文字版（到貨後同一指令會自動用機器人）")
        io = ConsoleIO()
    trivia.play(gemini, players, io=io)


if __name__ == "__main__":
    main()
