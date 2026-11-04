"""Basic presence/interaction reactions - the "hello world" of skills.

- Button press -> happy expression + green LED blink.
- Touch sensor -> spoken greeting via TTS.
"""


def register(ctx):
    ctx.sensory.on_event("sensor/button", _on_button(ctx))
    ctx.sensory.on_event("sensor/touch", _on_touch(ctx))


def _on_button(ctx):
    def handler(payload):
        if payload.get("event") == "press":
            ctx.send_command("STATUS_HAPPY")
            ctx.send_command("LED_GREEN_BLINK")
    return handler


def _on_touch(ctx):
    def handler(payload):
        if payload.get("value"):
            ctx.speak("嗨，你在摸我嗎？")
    return handler
