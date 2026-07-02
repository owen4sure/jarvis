"""Intent -> hardware command mapping.

This is the implementation of the table in
`specs/phase_04_embodied_soul/hardware_protocol_spec.md`.
Skills and the reasoning loop call `send_command(bridge, "STATUS_HAPPY")`
instead of building MQTT topics/payloads by hand, so the wire format
can change in one place without touching every skill.
"""

# intent -> (topic_suffix, default_payload)
INTENT_TABLE = {
    "STATUS_HAPPY": ("cmd/expression", {"emotion": "happy", "duration_ms": 1500}),
    "STATUS_SAD": ("cmd/expression", {"emotion": "sad", "duration_ms": 1500}),
    "STATUS_ANGRY": ("cmd/expression", {"emotion": "angry", "duration_ms": 1500}),
    "THINKING": ("cmd/expression", {"emotion": "thinking", "duration_ms": 0}),
    "STANDBY": ("cmd/expression", {"emotion": "neutral", "duration_ms": 0}),

    "LOOK_AT": ("cmd/servo", {"pan": 0, "tilt": 0}),
    "HEAD_TILT_LEFT": ("cmd/servo", {"pan": -20, "tilt": 0}),
    "HEAD_TILT_RIGHT": ("cmd/servo", {"pan": 20, "tilt": 0}),
    "HEAD_CENTER": ("cmd/servo", {"pan": 0, "tilt": 0}),
    "NOD": ("cmd/servo", {"gesture": "nod"}),
    "SHAKE_HEAD": ("cmd/servo", {"gesture": "shake"}),

    "LED_GREEN_BLINK": ("cmd/led", {"color": "#00FF00", "mode": "blink"}),
    "LED_RED_BLINK": ("cmd/led", {"color": "#FF0000", "mode": "blink"}),
    "EYE_LED_ON": ("cmd/led", {"color": "#FFFFFF", "mode": "solid"}),
    "LED_OFF": ("cmd/led", {"color": "#000000", "mode": "off"}),

    "ALERT": ("cmd/led", {"color": "#FF0000", "mode": "blink"}),

    "SPEAK": ("cmd/audio", {"url": ""}),

    "SYNC_REQUEST": ("sync/request", {}),
}


def send_command(bridge, intent, **overrides):
    """Send a high-level intent to StackChan.

    Extra keyword args override/extend the default payload, e.g.
    send_command(bridge, "LOOK_AT", pan=30, tilt=-10)
    send_command(bridge, "SPEAK", url="http://192.168.1.102:8800/audio/abc.wav")
    """
    if intent not in INTENT_TABLE:
        raise ValueError(f"Unknown intent: {intent}")

    topic_suffix, default_payload = INTENT_TABLE[intent]
    payload = {**default_payload, **overrides}
    bridge.publish(topic_suffix, payload)
    return topic_suffix, payload
