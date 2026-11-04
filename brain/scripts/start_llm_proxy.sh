#!/bin/bash
# Start the Hermes shared LLM key-rotation proxy.
# 所有 LLM 流量（Telegram / StackChan 語音 / hermes-agent）共用同一份金鑰輪換。
# Usage: ./scripts/start_llm_proxy.sh
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -u scripts/llm_proxy.py
