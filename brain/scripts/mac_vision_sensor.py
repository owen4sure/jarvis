"""Mac camera → sensor/vision MQTT bridge.

Captures frames from the Mac's built-in FaceTime HD camera (or any
AVFoundation device) using ffmpeg, asks Gemini Vision to detect a face,
then publishes the result as a `sensor/vision` MQTT event — the exact
same protocol `head_tracking_skill.py` already listens to.

This means head tracking works on the Mac *right now*, before StackChan
arrives. When StackChan arrives with its own ESP32-CAM, just stop this
script and let the firmware publish `sensor/vision` directly; no Hermes
code needs changing.

Prerequisites:
  - ffmpeg installed: `brew install ffmpeg` (already present if `which ffmpeg` works)
  - Gemini API key in config/keys.json (already configured)
  - Mac camera access granted to Terminal / the shell running this script
    (macOS will prompt on first run)

Run:
  ./.venv/bin/python -m scripts.mac_vision_sensor            # default: every 2s
  ./.venv/bin/python -m scripts.mac_vision_sensor --interval 1   # faster
  ./.venv/bin/python -m scripts.mac_vision_sensor --device 1     # use external camera

As a launchd service (always-on):
  Use the same pattern as com.hermes.telegrambot.plist — see WHEN_HARDWARE_ARRIVES.md.
  Only needed if you want head tracking to start at boot before StackChan arrives.
"""

import argparse
import os
import subprocess
import sys
import time
from typing import Optional

FRAME_PATH = "/tmp/hermes_vision_frame.jpg"
DEFAULT_INTERVAL = 2.0
FFMPEG_TIMEOUT = 5


def _capture_frame(device_index: int = 0) -> Optional[bytes]:
    """Capture one JPEG frame from the specified AVFoundation video device."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "avfoundation",
        "-framerate", "1",
        "-i", f"{device_index}:none",
        "-frames:v", "1",
        "-q:v", "5",
        FRAME_PATH,
    ]
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=True,
        )
        with open(FRAME_PATH, "rb") as f:
            return f.read()
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")
        if "Permission denied" in stderr or "AVFoundation" in stderr:
            print(
                "⚠️ [MacVisionSensor] 相機存取被拒。請到「系統偏好設定 > 安全性與隱私 > 相機」"
                "授權終端機/iTerm 存取相機後重新執行。"
            )
        else:
            print(f"⚠️ [MacVisionSensor] ffmpeg 擷取失敗: {stderr[-300:]}")
        return None
    except subprocess.TimeoutExpired:
        print("⚠️ [MacVisionSensor] ffmpeg 超時，跳過本幀")
        return None
    except FileNotFoundError:
        print(
            "⚠️ [MacVisionSensor] 找不到 ffmpeg。請先執行 `brew install ffmpeg`。"
        )
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Mac camera → sensor/vision MQTT bridge")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="Seconds between captures (default 2)")
    parser.add_argument("--device", type=int, default=0,
                        help="AVFoundation video device index (default 0 = FaceTime HD)")
    args = parser.parse_args()

    # Import inside main so the script is importable without triggering MQTT connection.
    from modules.embodied.gemini_client import GeminiClient
    from modules.embodied.mqtt_bridge import MQTTBridge

    bridge = MQTTBridge(client_id="hermes_mac_vision")
    bridge.connect()
    bridge.loop_start()
    time.sleep(1)

    gemini = GeminiClient()
    print(
        f"👁️ [MacVisionSensor] 啟動中，裝置 [{args.device}]，"
        f"每 {args.interval}s 偵測一次... Ctrl+C 結束"
    )

    try:
        while True:
            start = time.monotonic()
            image_bytes = _capture_frame(args.device)
            if image_bytes:
                result = gemini.detect_face_in_image(image_bytes)
                bridge.publish("sensor/vision", result)
                status = "✅ 偵測到人臉" if result["face_detected"] else "⬜ 無人臉"
                print(
                    f"👁️ {status}  face_x={result['face_x']:.2f}"
                    f"  face_y={result['face_y']:.2f}"
                )
            elapsed = time.monotonic() - start
            wait = max(0.0, args.interval - elapsed)
            time.sleep(wait)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.loop_stop()
        if os.path.exists(FRAME_PATH):
            os.remove(FRAME_PATH)
        print("👁️ [MacVisionSensor] 已停止")


if __name__ == "__main__":
    main()
