"""Skill loader.

A "skill" is a small module in this package with a single
`register(ctx)` function. It subscribes to sensor events via
`ctx.sensory.on_event(...)` and/or reacts via `ctx.send_command(...)`
/ `ctx.speak(...)`.

To add a new feature:
1. Drop a new `xxx_skill.py` file in this folder with a `register(ctx)` function.
2. Add its module name to `config/embodied.json` -> skills.enabled.
That's it - no other file needs to change.
"""

import importlib

from .. import config


def load_skills(ctx):
    loaded = []
    for name in config.ENABLED_SKILLS:
        module = importlib.import_module(f"modules.embodied.skills.{name}")
        module.register(ctx)
        loaded.append(name)
        print(f"🧩 [Skills] 已載入: {name}")
    return loaded
