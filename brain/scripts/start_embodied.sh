#!/bin/bash
# Start the Hermes Embodied Daemon (MQTT bridge + audio bridge + skills).
# Usage: ./scripts/start_embodied.sh
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -u -m scripts.embodied_daemon
