"""Hermes RAG 記憶 provider：每次對話用語意搜尋撈相關記憶（呼叫 Mac 的 8809 /query）。"""
import asyncio
import json
import urllib.request

from ..base import MemoryProviderBase, logger

TAG = __name__
QUERY_URL = "http://host.docker.internal:8809/query"


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config)

    async def save_memory(self, msgs, session_id=None):
        # 顯式記憶由 remember_fact 工具處理；此處不自動寫，避免雜訊
        return None

    async def query_memory(self, query: str) -> str:
        if not query:
            return ""

        def _do():
            req = urllib.request.Request(
                QUERY_URL,
                data=json.dumps({"query": query}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            return json.load(urllib.request.urlopen(req, timeout=15)).get("memory", "")

        try:
            mem = await asyncio.get_event_loop().run_in_executor(None, _do)
            if mem:
                logger.bind(tag=TAG).info(f"RAG 撈到記憶: {mem[:60]}")
            return mem or ""
        except Exception as e:
            logger.bind(tag=TAG).warning(f"hermes_rag query error: {e}")
            return ""
