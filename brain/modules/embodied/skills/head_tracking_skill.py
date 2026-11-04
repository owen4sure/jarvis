"""Turn toward whoever's talking - "頭部主動轉向說話者" wish-list item.

StackChan's firmware does the actual face detection on-device (cheap,
runs every frame on the ESP32-CAM) and publishes only the result as
`sensor/vision`:

    {"face_detected": true, "face_x": 0.0-1.0, "face_y": 0.0-1.0}

`face_x`/`face_y` are the detected face's center, normalized to the
camera frame (0,0 = top-left, 0.5,0.5 = center, 1,1 = bottom-right).
Sending normalized coordinates instead of raw image bytes keeps this
over MQTT/JSON like every other sensor topic - no video streaming.

This skill converts that into `LOOK_AT` servo commands: how far the
face is from center maps to how far the head should pan/tilt. To avoid
jitter, it only sends a new command when the target moved more than
`_DEADZONE_DEGREES`, and recenters once the face has been lost for
`_LOST_FRAMES_TO_RECENTER` consecutive frames.

Testable today without the camera via
`modules/embodied/mock_device.py vision` (see bottom of that file) -
it publishes a fake `sensor/vision` event so this skill's `LOOK_AT`
commands can be observed on `mock_device.py listen`.
"""

# Camera horizontal/vertical field of view, in degrees - how much the
# head needs to pan/tilt to keep a face at the frame's edge centered.
_FOV_PAN_DEGREES = 60
_FOV_TILT_DEGREES = 45

# Servo range matches HEAD_TILT_LEFT/RIGHT (±20) and beyond, but real
# physical limit is wider; clamp to a safe range.
_MAX_PAN = 45
_MAX_TILT = 30

# Don't re-send LOOK_AT for sub-degree jitter.
_DEADZONE_DEGREES = 3

# How many consecutive "face not detected" frames before recentering.
_LOST_FRAMES_TO_RECENTER = 5


def register(ctx):
    state = {"last_pan": 0, "last_tilt": 0, "lost_frames": 0, "centered": True}
    ctx.sensory.on_event("sensor/vision", _on_vision(ctx, state))


def _on_vision(ctx, state):
    def handler(payload):
        if not payload.get("face_detected"):
            state["lost_frames"] += 1
            if state["lost_frames"] >= _LOST_FRAMES_TO_RECENTER and not state["centered"]:
                ctx.send_command("HEAD_CENTER")
                state["last_pan"] = 0
                state["last_tilt"] = 0
                state["centered"] = True
            return

        state["lost_frames"] = 0

        face_x = payload.get("face_x", 0.5)
        face_y = payload.get("face_y", 0.5)

        pan = _clamp((face_x - 0.5) * _FOV_PAN_DEGREES, _MAX_PAN)
        tilt = _clamp((face_y - 0.5) * _FOV_TILT_DEGREES, _MAX_TILT)

        if (
            abs(pan - state["last_pan"]) < _DEADZONE_DEGREES
            and abs(tilt - state["last_tilt"]) < _DEADZONE_DEGREES
        ):
            return

        ctx.send_command("LOOK_AT", pan=round(pan), tilt=round(tilt))
        state["last_pan"] = pan
        state["last_tilt"] = tilt
        state["centered"] = False

    return handler


def _clamp(value, limit):
    return max(-limit, min(limit, value))
