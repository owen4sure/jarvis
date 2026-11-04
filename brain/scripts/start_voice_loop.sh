#!/bin/bash
# Start the StackChan hands-free voice loop (device audio hook -> brain -> say).
# Usage: ./scripts/start_voice_loop.sh
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -u -m scripts.stackchan_voice_loop
