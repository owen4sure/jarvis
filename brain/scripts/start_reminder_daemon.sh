#!/bin/bash
# Start the Hermes Reminder Daemon (checks config/reminders.json every minute).
# Usage: ./scripts/start_reminder_daemon.sh
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -u -m scripts.reminder_daemon
