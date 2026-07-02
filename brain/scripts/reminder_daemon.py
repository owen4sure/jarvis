"""Reminder Daemon - fires due `/remind` entries (config/reminders.json)
and checks `/watch` price/earthquake alerts (config/watchers.json), once
per minute.

Sends each due reminder/alert to every authorized Telegram user
(config/telegram.json -> allowed_user_ids; for private chats chat_id ==
user_id) and, if StackChan is reachable over MQTT, also speaks it via
`SPEAK`/TTS through the audio bridge's `/audio/{filename}` flow.

Run via: ./scripts/start_reminder_daemon.sh (launchd, see
~/Library/LaunchAgents/com.hermes.reminderdaemon.plist)
"""

import time

from modules.embodied.mqtt_bridge import MQTTBridge
from modules.embodied.command_mapper import send_command
from modules.embodied import config as embodied_config
from modules.embodied import tts
from modules.productivity import daily_content_skills, reminder_manager, watcher_manager
from modules.remote.telegram_handler import TelegramHandler

CHECK_INTERVAL_SECONDS = 60


def _speak_via_stackchan(bridge, message):
    try:
        wav_path = tts.synthesize(message)
        import os
        filename = os.path.basename(wav_path)
        url = f"http://{embodied_config.MQTT_HOST}:{embodied_config.AUDIO_BRIDGE_PORT}/audio/{filename}"
        send_command(bridge, "SPEAK", url=url)
    except Exception as e:
        print(f"⚠️ [ReminderDaemon] StackChan 播放失敗: {e}")


def _broadcast(telegram, bridge, message, channel="both"):
    """channel：both=Telegram+語音 / telegram=只傳訊息 / voice=只用 StackChan 語音講。"""
    # 文字送 Telegram（channel 是 both 或 telegram 時）
    if channel in ("both", "telegram"):
        for user_id in telegram.allowed_user_ids:
            try:
                telegram.send_message(user_id, message)
            except Exception as e:
                print(f"⚠️ [ReminderDaemon] Telegram 發送失敗: {e}")

    # StackChan 語音（channel 是 both 或 voice 時）；不在線就安靜略過（presence-aware）
    if channel in ("both", "voice"):
        try:
            from modules.embodied import notify
            notify.speak_if_present(message)
        except Exception as e:
            print(f"⚠️ [ReminderDaemon] StackChan 播放略過: {e}")
        # 舊 MQTT 韌體（legacy）：若還在用 MQTT 裝置，也試著推播
        if bridge and bridge.client.is_connected():
            _speak_via_stackchan(bridge, message)


def main():
    print("⏰ [Reminder Daemon] 啟動中...")

    telegram = TelegramHandler()

    bridge = MQTTBridge(client_id="reminder_daemon")
    try:
        bridge.connect()
        bridge.loop_start()
        print("✅ [MQTTBridge] 已連線")
    except Exception as e:
        print(f"⚠️ [MQTTBridge] 無法連線，提醒只會送到 Telegram。錯誤: {e}")
        bridge = None

    print("✅ [Reminder Daemon] 開始每分鐘檢查提醒...")

    while True:
        try:
            due = reminder_manager.get_due_reminders()
            for reminder in due:
                if reminder.get("skill"):
                    body = daily_content_skills.generate(reminder["skill"])
                else:
                    # 有提早提醒就點出「事件幾點、還有幾分鐘」，沒提早就直接講事項
                    _lead = int(reminder.get("lead_minutes") or 0)
                    if _lead > 0:
                        body = (f"⏰ 提醒：{reminder['time']} {reminder['message']}"
                                f"（還有 {_lead} 分鐘）")
                    else:
                        body = f"⏰ 提醒：{reminder['message']}"

                if body is None:
                    # skill decided there's nothing to report this time
                    # (e.g. bedtime_check when the user already went to bed)
                    continue

                print(f"🔔 [ReminderDaemon] 觸發提醒 #{reminder['id']}: {reminder.get('skill') or reminder['message']}")
                _broadcast(telegram, bridge, body, reminder.get("channel", "both"))
        except Exception as e:
            print(f"⚠️ [ReminderDaemon] 檢查提醒失敗: {e}")

        try:
            for alert_message in watcher_manager.check_watchers():
                print(f"🔔 [ReminderDaemon] 觸發警示: {alert_message}")
                _broadcast(telegram, bridge, alert_message)
        except Exception as e:
            print(f"⚠️ [ReminderDaemon] 檢查警示失敗: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
