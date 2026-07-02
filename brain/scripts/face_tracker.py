"""臉追蹤(找人):喚醒抬頭後,讓 StackChan 轉頭對準講話的人的臉。

流程(配合容器端):
- 容器 poller 喚醒抬頭時寫 data/find_face.flag,並在沒講話時連續 take_photo → 更新 last_camera.jpg。
- 這個 Mac 服務:看到 find_face.flag + last_camera.jpg 有新畫面 → 用 OpenCV 找最大的臉 →
  算臉偏離畫面中心多少 → 換算成 yaw/pitch 角度 → 寫 head_cmd.json → 容器 poller 0.3s 內轉頭。
- 對準後(臉接近中心)或逾時 → 清掉 find_face.flag,停止追蹤。

幾何(實測校準,可用 env 改):yaw 正=右、pitch 45=水平/高=抬頭。相機在頭上,所以臉在畫面右邊
=要往右轉(除非鏡像)。增益/正負號用 env 調:HERMES_FACE_YAW_GAIN / HERMES_FACE_PITCH_GAIN。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import cv2

DATA = Path("/Users/chenyouwei/xiaozhi-server/data")
IMG = DATA / "last_camera.jpg"
FLAG = DATA / "find_face.flag"
CMD = DATA / "head_cmd.json"

# 校準參數(env 可覆寫)。GAIN=畫面邊緣對應多少度;SIGN=方向(鏡像就設 -1)。
YAW_GAIN = float(os.environ.get("HERMES_FACE_YAW_GAIN", "32"))    # 臉在最邊→轉約32度
YAW_SIGN = float(os.environ.get("HERMES_FACE_YAW_SIGN", "1"))
PITCH_GAIN = float(os.environ.get("HERMES_FACE_PITCH_GAIN", "20"))
PITCH_SIGN = float(os.environ.get("HERMES_FACE_PITCH_SIGN", "1"))
PITCH_BASE = float(os.environ.get("HERMES_FACE_PITCH_BASE", "45"))  # 抬頭基準
CENTER_TOL = 0.10   # 臉中心在畫面中央±10%內 → 算對準,停止
TRACK_TIMEOUT = 8   # 一次找人最多追幾秒

_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def _largest_face(gray):
    faces = _cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(50, 50))
    if len(faces) == 0:
        return None
    # 取面積最大的(最近/最主要的人)
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return x + w / 2.0, y + h / 2.0, w, h


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def track_once() -> bool:
    """找一次臉並轉頭。回 True=已對準(可停),False=還在追/沒找到。"""
    try:
        img = cv2.imread(str(IMG))
        if img is None:
            return False
        H, W = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        face = _largest_face(gray)
        if face is None:
            return False
        fx, fy, fw, fh = face
        dx = (fx - W / 2.0) / W          # -0.5..0.5
        dy = (fy - H / 2.0) / H
        if abs(dx) < CENTER_TOL and abs(dy) < CENTER_TOL:
            return True                  # 臉在中央 → 對準了
        yaw = _clamp(YAW_SIGN * dx * 2 * YAW_GAIN, -80, 80)
        pitch = _clamp(PITCH_BASE - PITCH_SIGN * dy * 2 * PITCH_GAIN, 8, 80)
        CMD.write_text(json.dumps({"yaw": int(round(yaw)), "pitch": int(round(pitch))}))
        return False
    except Exception:
        return False


def main():
    print("👁️ [FaceTracker] 啟動,等喚醒找人訊號")
    last_handled = 0
    while True:
        try:
            if FLAG.exists():
                started = float(FLAG.read_text() or time.time())
                if started != last_handled:
                    # 一次新的「找人」:追到對準或逾時
                    t0 = time.time()
                    while time.time() - t0 < TRACK_TIMEOUT:
                        if not FLAG.exists():
                            break
                        if track_once():
                            break
                        time.sleep(0.5)
                    last_handled = started
                    try:
                        FLAG.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception as e:
            print(f"⚠️ [FaceTracker] {e}")
        time.sleep(0.4)


if __name__ == "__main__":
    main()
