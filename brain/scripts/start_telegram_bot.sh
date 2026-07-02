#!/bin/bash
# Start the Hermes Telegram Bot (long polling).
# Usage: ./scripts/start_telegram_bot.sh
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -u -m scripts.telegram_bot
