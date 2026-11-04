"""
liveliness — 待機生命感：讓 StackChan 沒在對話時也「活著」
============================================================================
裝置在線、且最近沒在互動時，每隔幾秒做一個自然的小動作：
  - 自動眨眼（開一次就會持續）
  - 視線游移 / 偶爾輕轉頭（小角度、慢速、安全範圍）
  - LED 像呼吸一樣的微光起伏
互動時（語音迴圈剛處理過）會自動讓位，不打斷對話。

啟動：./.venv/bin/python -m scripts.liveliness
（launchd com.hermes.liveliness 常駐）
"""
import math
import os
import random
import time

from modules.embodied import notify
from modules.embodied.stackchan_mcp_client import StackChanClient

VOICE_LAST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "memory", "voice_last.txt")
IDLE_GUARD_SECONDS = 25     # 互動後這段時間內不打擾
TICK_MIN, TICK_MAX = 7, 16  # 每隔幾秒一個小動作


def _seconds_since_interaction() -> float:
    try:
        with open(VOICE_LAST) as f:
            return time.time() - float(f.read().strip())
    except Exception:
        return 1e9


def _breathe_led(robot, t):
    # 柔和暖白，亮度像呼吸般起伏（sin 波）
    base = 0.5 + 0.5 * math.sin(t / 2.0)
    level = int(8 + base * 22)  # 8~30，很微弱不刺眼
    robot.set_all_leds(level, int(level * 0.85), int(level * 0.6))


def main():
    robot = StackChanClient()
    print("🌬️  [Liveliness] 待機生命感啟動")
    blink_on = False
    t0 = time.time()

    while True:
        try:
            present = notify.robot_present()
            if not present:
                blink_on = False
                time.sleep(20)
                continue

            # 一連上就打開自動眨眼
            if not blink_on:
                try:
                    robot.call_tool("set_blink", {"enabled": True})
                    blink_on = True
                except Exception:
                    pass

            # 互動中 → 讓位
            if _seconds_since_interaction() < IDLE_GUARD_SECONDS:
                time.sleep(3)
                continue

            # 隨機挑一個自然的待機動作
            action = random.random()
            if action < 0.5:
                # 視線游移 / 輕轉頭（小角度、慢速）
                yaw = random.randint(-25, 25)
                pitch = random.randint(42, 58)
                robot.move_head(yaw, pitch, speed=20)
            elif action < 0.7:
                # 回正中央，像「回神」
                robot.move_head(0, 50, speed=18)
            # LED 呼吸（每次 tick 都更新一點）
            _breathe_led(robot, time.time() - t0)

        except Exception as e:
            print(f"⚠️ [Liveliness] {e}")
        time.sleep(random.uniform(TICK_MIN, TICK_MAX))


if __name__ == "__main__":
    main()
