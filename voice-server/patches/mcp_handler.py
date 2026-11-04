"""设备端MCP客户端支持模块"""

import json
import asyncio
import re
from concurrent.futures import Future
from core.utils.util import get_vision_url, sanitize_tool_name
from core.utils.auth import AuthToken
from config.logger import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()


class MCPClient:
    """设备端MCP客户端，用于管理MCP状态和工具"""

    def __init__(self):
        self.tools = {}  # sanitized_name -> tool_data
        self.name_mapping = {}
        self.ready = False
        self.call_results = {}  # To store Futures for tool call responses
        self.next_id = 100  # 從 100 開始，避免和保留的 init(id=1)/tools-list(id=2) 撞號→回應被誤路由
        self.lock = asyncio.Lock()
        self._cached_available_tools = None  # Cache for get_available_tools

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def get_available_tools(self) -> list:
        # Check if the cache is valid
        if self._cached_available_tools is not None:
            return self._cached_available_tools

        # If cache is not valid, regenerate the list
        result = []
        for tool_name, tool_data in self.tools.items():
            function_def = {
                "name": tool_name,
                "description": tool_data["description"],
                "parameters": {
                    "type": tool_data["inputSchema"].get("type", "object"),
                    "properties": tool_data["inputSchema"].get("properties", {}),
                    "required": tool_data["inputSchema"].get("required", []),
                },
            }
            result.append({"type": "function", "function": function_def})

        self._cached_available_tools = result  # Store the generated list in cache
        return result

    async def is_ready(self) -> bool:
        async with self.lock:
            return self.ready

    async def set_ready(self, status: bool):
        async with self.lock:
            self.ready = status

    async def add_tool(self, tool_data: dict):
        async with self.lock:
            sanitized_name = sanitize_tool_name(tool_data["name"])
            self.tools[sanitized_name] = tool_data
            self.name_mapping[sanitized_name] = tool_data["name"]
            self._cached_available_tools = (
                None  # Invalidate the cache when a tool is added
            )

    async def get_next_id(self) -> int:
        async with self.lock:
            current_id = self.next_id
            self.next_id += 1
            return current_id

    async def register_call_result_future(self, id: int, future: Future):
        async with self.lock:
            self.call_results[id] = future

    async def resolve_call_result(self, id: int, result: any):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_result(result)

    async def reject_call_result(self, id: int, exception: Exception):
        async with self.lock:
            if id in self.call_results:
                future = self.call_results.pop(id)
                if not future.done():
                    future.set_exception(exception)

    async def cleanup_call_result(self, id: int):
        async with self.lock:
            if id in self.call_results:
                self.call_results.pop(id)


async def send_mcp_message(conn: "ConnectionHandler", payload: dict):
    """Helper to send MCP messages, encapsulating common logic."""
    if not conn.features.get("mcp"):
        logger.bind(tag=TAG).warning("客户端不支持MCP，无法发送MCP消息")
        return

    message = json.dumps({"type": "mcp", "payload": payload})

    try:
        await conn.websocket.send(message)
        logger.bind(tag=TAG).debug(f"成功发送MCP消息: {message}")
    except Exception as e:
        logger.bind(tag=TAG).error(f"发送MCP消息失败: {e}")


async def handle_mcp_message(
    conn: "ConnectionHandler", mcp_client: MCPClient, payload: dict
):
    """处理MCP消息,包括初始化、工具列表和工具调用响应等"""
    logger.bind(tag=TAG).debug(f"处理MCP消息: {str(payload)[:100]}")

    if not isinstance(payload, dict):
        logger.bind(tag=TAG).error("MCP消息缺少payload字段或格式错误")
        return

    # Handle result
    if "result" in payload:
        result = payload["result"]
        msg_id = int(payload.get("id", 0))

        # Check for tool call response first
        if msg_id in mcp_client.call_results:
            logger.bind(tag=TAG).debug(
                f"收到工具调用响应，ID: {msg_id}, 结果: {result}"
            )
            await mcp_client.resolve_call_result(msg_id, result)
            return

        if msg_id == 1:  # mcpInitializeID
            logger.bind(tag=TAG).debug("收到MCP初始化响应")
            server_info = result.get("serverInfo")
            if isinstance(server_info, dict):
                name = server_info.get("name")
                version = server_info.get("version")
                logger.bind(tag=TAG).debug(
                    f"客户端MCP服务器信息: name={name}, version={version}"
                )

            await asyncio.sleep(1)
            logger.bind(tag=TAG).debug("初始化完成，开始请求MCP工具列表")
            await send_mcp_tools_list_request(conn)

            return

        elif msg_id == 2:  # mcpToolsListID
            logger.bind(tag=TAG).debug("收到MCP工具列表响应")
            if isinstance(result, dict) and "tools" in result:
                tools_data = result["tools"]
                if not isinstance(tools_data, list):
                    logger.bind(tag=TAG).error("工具列表格式错误")
                    return

                logger.bind(tag=TAG).info(
                    f"客户端设备支持的工具数量: {len(tools_data)}"
                )

                for i, tool in enumerate(tools_data):
                    if not isinstance(tool, dict):
                        continue

                    name = tool.get("name", "")
                    description = tool.get("description", "")
                    input_schema = {"type": "object", "properties": {}, "required": []}

                    if "inputSchema" in tool and isinstance(tool["inputSchema"], dict):
                        schema = tool["inputSchema"]
                        input_schema["type"] = schema.get("type", "object")
                        input_schema["properties"] = schema.get("properties", {})
                        input_schema["required"] = [
                            s for s in schema.get("required", []) if isinstance(s, str)
                        ]

                    new_tool = {
                        "name": name,
                        "description": description,
                        "inputSchema": input_schema,
                    }
                    await mcp_client.add_tool(new_tool)
                    logger.bind(tag=TAG).debug(f"客户端工具 #{i+1}: {name}")

                # 替换所有工具描述中的工具名称
                for tool_data in mcp_client.tools.values():
                    if "description" in tool_data:
                        description = tool_data["description"]
                        # 遍历所有工具名称进行替换
                        for (
                            sanitized_name,
                            original_name,
                        ) in mcp_client.name_mapping.items():
                            description = description.replace(
                                original_name, sanitized_name
                            )
                        tool_data["description"] = description

                next_cursor = result.get("nextCursor", "")
                if next_cursor:
                    logger.bind(tag=TAG).debug(f"有更多工具，nextCursor: {next_cursor}")
                    await send_mcp_tools_list_continue_request(conn, next_cursor)
                else:
                    await mcp_client.set_ready(True)
                    logger.bind(tag=TAG).debug("所有工具已获取，MCP客户端准备就绪")

                    # 刷新工具缓存，确保MCP工具被包含在函数列表中
                    if hasattr(conn, "func_handler") and conn.func_handler:
                        conn.func_handler.tool_manager.refresh_tools()
                        conn.func_handler.current_support_functions()

                    # Hermes 連線問候：轉頭打招呼（同時驗證舵機物理鏈路）
                    async def _greet():
                        import json as _gj
                        # 🔄 USB-JTAG serial 卡死復原：若有 flag，連線時送 system reboot 給裝置，
                        #    讓它軟重啟、重新初始化 USB serial（開發端才能再截圖/燒錄）。一次性。
                        import os as _os
                        _RBF = "/tmp/hermes_reboot_device"
                        if _os.path.exists(_RBF):
                            try: _os.remove(_RBF)
                            except Exception: pass
                            try:
                                await conn.websocket.send('{"type":"system","command":"reboot"}')
                                logger.bind(tag=TAG).info("🔄 已送 reboot 給裝置(復原 USB serial)")
                            except Exception as _e:
                                logger.bind(tag=TAG).error(f"reboot send fail: {_e}")
                            return
                        name = None
                        for n in ("self.robot.set_head_angles",
                                  "self_robot_set_head_angles"):
                            if mcp_client.has_tool(n):
                                name = n
                                break
                        if not name:
                            return
                        try:
                            # 🔊 音量開到最大（每次連線都設，解決回應太小聲）
                            for vn in ("self.audio_speaker.set_volume", "self_audio_speaker_set_volume"):
                                if mcp_client.has_tool(vn):
                                    await call_mcp_tool(conn, mcp_client, vn,
                                                        _gj.dumps({"volume": 100}), timeout=8)
                                    logger.bind(tag=TAG).info("🔊 音量已設為最大 100")
                                    break
                            # 👀 開啟自動眨眼（Goal 3 靈魂眼神）：每 3-6 秒眨一次，配合呼吸=活著
                            for bn in ("self.display.set_blink", "self_display_set_blink"):
                                if mcp_client.has_tool(bn):
                                    await call_mcp_tool(conn, mcp_client, bn,
                                                        _gj.dumps({"enabled": True}), timeout=8)
                                    logger.bind(tag=TAG).info("👀 自動眨眼已開啟")
                                    break
                            # 🦾 Iron Man 開機動畫（喚醒/連線時）：
                            #   ① 方舟反應爐 LED 環逐顆充能點亮 → ② 眼睛閃亮啟動(surprised)
                            #   → ③ 全環亮一下 → ④ 收斂回待命(idle) + LED 漸暗
                            import asyncio as _aio
                            _set_color = next((n for n in ("self.led.set_color", "self_led_set_color")
                                               if mcp_client.has_tool(n)), None)
                            _set_all = next((n for n in ("self.led.set_all", "self_led_set_all")
                                             if mcp_client.has_tool(n)), None)
                            _set_avatar = next((n for n in ("self.display.set_avatar", "self_display_set_avatar")
                                                if mcp_client.has_tool(n)), None)
                            # ① 環狀充能：逐顆點亮(像反應爐啟動)
                            if _set_all:
                                await call_mcp_tool(conn, mcp_client, _set_all,
                                                    _gj.dumps({"r": 0, "g": 0, "b": 0}), timeout=8)
                            if _set_color:
                                for i in range(12):
                                    await call_mcp_tool(conn, mcp_client, _set_color,
                                                        _gj.dumps({"index": i, "r": 0, "g": 90, "b": 255}), timeout=6)
                                    await _aio.sleep(0.035)
                            # ② 眼睛閃亮啟動
                            if _set_avatar:
                                await call_mcp_tool(conn, mcp_client, _set_avatar,
                                                    _gj.dumps({"face": "surprised"}), timeout=8)
                            # ③ 全環亮一下(充能完成)
                            if _set_all:
                                await call_mcp_tool(conn, mcp_client, _set_all,
                                                    _gj.dumps({"r": 40, "g": 160, "b": 255}), timeout=8)
                            await _aio.sleep(0.45)
                            # ④ 收斂回待命 + LED 漸暗
                            if _set_avatar:
                                await call_mcp_tool(conn, mcp_client, _set_avatar,
                                                    _gj.dumps({"face": "idle"}), timeout=8)
                            if _set_all:
                                for col in ((0, 60, 120), (0, 22, 48), (0, 0, 0)):
                                    await call_mcp_tool(conn, mcp_client, _set_all,
                                                        _gj.dumps({"r": col[0], "g": col[1], "b": col[2]}), timeout=6)
                                    await _aio.sleep(0.13)
                            logger.bind(tag=TAG).info("🦾 Iron Man 開機動畫完成，回到待命")
                            import os as _os
                            # 🎉 派對打招呼（HERMES_PARTY=1）：音量最大+大聲歡迎詞+揮頭+LED+拍照
                            if _os.environ.get("HERMES_PARTY") == "1":
                                async def _avp(f):
                                    for an in ("self.display.set_avatar", "self_display_set_avatar"):
                                        if mcp_client.has_tool(an):
                                            await call_mcp_tool(conn, mcp_client, an, _gj.dumps({"face": f}), timeout=8); return
                                async def _ledp(r, g, b):
                                    for ln in ("self.led.set_all", "self_led_set_all"):
                                        if mcp_client.has_tool(ln):
                                            await call_mcp_tool(conn, mcp_client, ln, _gj.dumps({"r": r, "g": g, "b": b}), timeout=8); return
                                # 音量開到最大
                                for vn in ("self.audio_speaker.set_volume", "self_audio_speaker_set_volume"):
                                    if mcp_client.has_tool(vn):
                                        await call_mcp_tool(conn, mcp_client, vn, _gj.dumps({"volume": 100}), timeout=8); break
                                await _avp("happy"); await _ledp(255, 80, 0)
                                # 大聲說歡迎詞（推 TTS 給裝置喇叭播）
                                try:
                                    import uuid as _uuid
                                    from core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType
                                    from core.handle.sendAudioHandle import send_tts_message
                                    # 先發 tts "start" 讓裝置進入 speaking 狀態（否則 idle 不會播音）
                                    await send_tts_message(conn, "start")
                                    conn.client_is_speaking = True
                                    sid = _uuid.uuid4().hex
                                    conn.sentence_id = sid
                                    greet_text = "大家好！我是 Hermes，Owen 的機器人夥伴！很高興認識大家，歡迎歡迎！"
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sid, sentence_type=SentenceType.FIRST, content_type=ContentType.ACTION))
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sid, sentence_type=SentenceType.MIDDLE, content_type=ContentType.TEXT, content_detail=greet_text))
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sid, sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))
                                    logger.bind(tag=TAG).info("🎉 派對歡迎詞已推送(已先發 start)")
                                except Exception as _e:
                                    logger.bind(tag=TAG).warning(f"歡迎詞推送失敗: {_e}")
                                # 揮頭打招呼 + LED 閃
                                for i, yaw in enumerate((55, -55, 55, -55, 0)):
                                    await call_mcp_tool(conn, mcp_client, name, _gj.dumps({"yaw": yaw, "pitch": 45}), timeout=8)
                                    await _ledp((i * 80) % 256, (255 - i * 60) % 256, (i * 100) % 256)
                                    await asyncio.sleep(0.4)
                                # 拍照看大家
                                for cn in ("self.camera.take_photo", "self_camera_take_photo"):
                                    if mcp_client.has_tool(cn):
                                        r = await call_mcp_tool(conn, mcp_client, cn, _gj.dumps({"question": "你看到誰？用繁體中文打個招呼"}), timeout=30)
                                        logger.bind(tag=TAG).info(f"🎉📷 派對拍照: {str(r)[:200]}")
                                        break
                            # 🎬 DEMO 表演（HERMES_DEMO=1 時）：表情秀+跳舞+LED變色，給使用者看
                            if _os.environ.get("HERMES_DEMO") == "1":
                                async def _av(f):
                                    for an in ("self.display.set_avatar", "self_display_set_avatar"):
                                        if mcp_client.has_tool(an):
                                            await call_mcp_tool(conn, mcp_client, an, _gj.dumps({"face": f}), timeout=8)
                                            return
                                async def _led(r, g, b):
                                    for ln in ("self.led.set_all", "self_led_set_all"):
                                        if mcp_client.has_tool(ln):
                                            await call_mcp_tool(conn, mcp_client, ln, _gj.dumps({"r": r, "g": g, "b": b}), timeout=8)
                                            return
                                for f in ("happy", "surprised", "sad", "thinking", "idle"):
                                    await _av(f)
                                    await asyncio.sleep(1.4)
                                for y, r, g, b in [(75, 255, 0, 0), (-75, 0, 255, 0), (50, 0, 0, 255), (-50, 255, 0, 255), (0, 255, 200, 0)]:
                                    await call_mcp_tool(conn, mcp_client, name, _gj.dumps({"yaw": y, "pitch": 45}), timeout=8)
                                    await _led(r, g, b)
                                    await asyncio.sleep(0.7)
                                await _av("happy")
                                logger.bind(tag=TAG).info("🎬 Hermes DEMO 表演完成")
                            # 相機端到端內部測試：拍一張+視覺描述（只在有 _HERMES_CAM_TEST 旗標時）
                            if _os.environ.get("HERMES_CAM_TEST") == "1":
                                for cn in ("self.camera.take_photo", "self_camera_take_photo"):
                                    if mcp_client.has_tool(cn):
                                        r = await call_mcp_tool(
                                            conn, mcp_client, cn,
                                            _gj.dumps({"question": "你現在看到什麼？用繁體中文一兩句描述"}),
                                            timeout=30)
                                        logger.bind(tag=TAG).info(f"📷 相機視覺結果: {str(r)[:300]}")
                                        break
                        except Exception as _e:
                            logger.bind(tag=TAG).warning(f"問候失敗: {_e}")

                    asyncio.create_task(_greet())

                    # ⏰ 提醒/計時器到點 → 在裝置上唸出來（背景輪詢 Mac 的記憶端點）
                    async def _reminder_poller():
                        import json as _pj
                        import urllib.request as _pu
                        import uuid as _puuid
                        from core.providers.tts.dto.dto import (
                            TTSMessageDTO, SentenceType, ContentType)
                        from core.handle.sendAudioHandle import send_tts_message
                        await asyncio.sleep(15)
                        while True:
                            try:
                                if getattr(conn, "stop_event", None) and conn.stop_event.is_set():
                                    break
                                loop = asyncio.get_event_loop()
                                def _fetch():
                                    return _pj.loads(_pu.urlopen(
                                        "http://host.docker.internal:8809/due_reminders",
                                        timeout=8).read().decode())
                                d = await loop.run_in_executor(None, _fetch)
                                for msg in (d.get("due") or []):
                                    text = f"提醒你，{msg}"
                                    # 只在裝置閒置時唸（避免打斷對話）
                                    sid = _puuid.uuid4().hex
                                    conn.sentence_id = sid
                                    await send_tts_message(conn, "start")
                                    conn.client_is_speaking = True
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(
                                        sentence_id=sid, sentence_type=SentenceType.FIRST,
                                        content_type=ContentType.ACTION))
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(
                                        sentence_id=sid, sentence_type=SentenceType.MIDDLE,
                                        content_type=ContentType.TEXT, content_detail=text))
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(
                                        sentence_id=sid, sentence_type=SentenceType.LAST,
                                        content_type=ContentType.ACTION))
                                    logger.bind(tag=TAG).info(f"⏰ 提醒已唸出: {msg}")
                                # 🕵️ 後台特工任務完成 → 主動匯報（Goal 4 多Agent調度+完成匯報）
                                def _fetch_agent():
                                    return _pj.loads(_pu.urlopen(
                                        "http://host.docker.internal:8809/agent_results",
                                        timeout=8).read().decode())
                                ad = await loop.run_in_executor(None, _fetch_agent)
                                for item in (ad.get("results") or []):
                                    if getattr(conn, "client_is_speaking", False):
                                        break
                                    rep = f"你交代我的「{item.get('task','')}」，我弄好了。{item.get('result','')}"
                                    sid = _puuid.uuid4().hex
                                    conn.sentence_id = sid
                                    await send_tts_message(conn, "start")
                                    conn.client_is_speaking = True
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(
                                        sentence_id=sid, sentence_type=SentenceType.FIRST,
                                        content_type=ContentType.ACTION))
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(
                                        sentence_id=sid, sentence_type=SentenceType.MIDDLE,
                                        content_type=ContentType.TEXT, content_detail=rep))
                                    conn.tts.tts_text_queue.put(TTSMessageDTO(
                                        sentence_id=sid, sentence_type=SentenceType.LAST,
                                        content_type=ContentType.ACTION))
                                    logger.bind(tag=TAG).info(f"🕵️ 後台任務匯報: {item.get('task','')[:40]}")
                            except Exception as _re:
                                logger.bind(tag=TAG).debug(f"提醒輪詢: {_re}")
                            await asyncio.sleep(25)

                    asyncio.create_task(_reminder_poller())

                    # 👁️ 主動式桌面視覺（Goal 3+4）：定時看桌面，看懂就主動關心一句。
                    async def _desk_vision_poller():
                        import json as _vj
                        import uuid as _vuuid
                        import os as _vos
                        from core.providers.tts.dto.dto import (
                            TTSMessageDTO, SentenceType, ContentType)
                        from core.handle.sendAudioHandle import send_tts_message
                        # 預設【關閉】自動桌面視覺——使用者沒要求就不要自己開相機。
                        # 要開的話設環境變數 HERMES_DESK_VISION_SEC=900 之類。
                        interval = int(_vos.environ.get("HERMES_DESK_VISION_SEC", "0"))
                        if interval <= 0:
                            return
                        await asyncio.sleep(45)  # 連線後先靜候
                        cam = None
                        for cn in ("self.camera.take_photo", "self_camera_take_photo"):
                            if mcp_client.has_tool(cn):
                                cam = cn
                                break
                        if not cam:
                            return
                        q = ("看看畫面裡有什麼。如果有值得主動跟主人 Owen 聊或關心的"
                             "（例如他在喝咖啡、在寫程式很久了該休息、桌上有新東西、他看起來累），"
                             "就用繁體中文講一句【簡短、口語、像朋友】的主動關心或評論；"
                             "如果畫面沒什麼特別、太暗或看不清楚，只回兩個字 NONE，不要硬講。")
                        while True:
                            await asyncio.sleep(interval)
                            # 連線關閉就停，避免在死連線上無限輪詢洩漏
                            if getattr(conn, "stop_event", None) and conn.stop_event.is_set():
                                break
                            if getattr(conn, "websocket", None) is None:
                                break
                            try:
                                # 只在裝置閒置時看（避免打斷對話）
                                if getattr(conn, "client_is_speaking", False):
                                    continue
                                r = await call_mcp_tool(conn, mcp_client, cam,
                                                        _vj.dumps({"question": q}), timeout=30)
                                txt = str(r)
                                # 取出視覺回覆內容
                                m = None
                                try:
                                    rd = _vj.loads(txt) if txt.strip().startswith("{") else None
                                    if isinstance(rd, dict):
                                        m = rd.get("response") or rd.get("text")
                                except Exception:
                                    pass
                                comment = (m or txt).strip()
                                if (not comment) or "NONE" in comment.upper() or len(comment) < 4:
                                    continue
                                sid = _vuuid.uuid4().hex
                                conn.sentence_id = sid
                                await send_tts_message(conn, "start")
                                conn.client_is_speaking = True
                                conn.tts.tts_text_queue.put(TTSMessageDTO(
                                    sentence_id=sid, sentence_type=SentenceType.FIRST,
                                    content_type=ContentType.ACTION))
                                conn.tts.tts_text_queue.put(TTSMessageDTO(
                                    sentence_id=sid, sentence_type=SentenceType.MIDDLE,
                                    content_type=ContentType.TEXT, content_detail=comment))
                                conn.tts.tts_text_queue.put(TTSMessageDTO(
                                    sentence_id=sid, sentence_type=SentenceType.LAST,
                                    content_type=ContentType.ACTION))
                                logger.bind(tag=TAG).info(f"👁️ 主動桌面視覺: {comment[:60]}")
                            except Exception as _ve:
                                logger.bind(tag=TAG).debug(f"桌面視覺輪詢: {_ve}")

                    asyncio.create_task(_desk_vision_poller())

                    # Hermes Live 視覺：dashboard 開啟時連續快拍更新 last_camera.jpg
                    async def _live_vision_poller():
                        import json as _lj, os as _los
                        flag = "/opt/xiaozhi-esp32-server/data/live_mode.flag"
                        await asyncio.sleep(18)
                        cam = None
                        for cn in ("self.camera.take_photo", "self_camera_take_photo"):
                            if mcp_client.has_tool(cn):
                                cam = cn
                                break
                        if not cam:
                            return
                        while True:
                            # 連線關閉就停，避免死連線上無限連拍
                            if getattr(conn, "stop_event", None) and conn.stop_event.is_set():
                                break
                            if getattr(conn, "websocket", None) is None:
                                break
                            try:
                                if _los.path.exists(flag):
                                    # 對話中(講話/處理中)就暫停連拍,別跟語音搶裝置+websocket(否則語音會卡)
                                    if (getattr(conn, "client_is_speaking", False)
                                            or getattr(conn, "_hermes_in_turn", False)):
                                        await asyncio.sleep(0.6)
                                    else:
                                        await call_mcp_tool(
                                            conn, mcp_client, cam,
                                            _lj.dumps({"question": "__capture__"}), timeout=12)
                                        await asyncio.sleep(1.5)
                                else:
                                    await asyncio.sleep(2)
                            except Exception:
                                await asyncio.sleep(2)

                    asyncio.create_task(_live_vision_poller())

                    # Hermes 頭部直控：watch data/head_cmd.json，內容一變就 set_head_angles(yaw,pitch)。
                    # 讓外部(Mac/dashboard/未來人臉追蹤)能直接驅動頭，不必經對話。
                    async def _head_cmd_poller():
                        import json as _hj, os as _hos
                        cmd_f = "/opt/xiaozhi-esp32-server/data/head_cmd.json"
                        await asyncio.sleep(12)
                        tool = None
                        for hn in ("self.robot.set_head_angles", "self_robot_set_head_angles"):
                            if mcp_client.has_tool(hn):
                                tool = hn
                                break
                        if not tool:
                            return
                        import time as _ht
                        DOWN, UP, IDLE_S = 15, 45, 15   # 閒置低頭15、喚醒抬頭45、閒置15秒才低頭(財務查股價慢、別答到一半就低頭)

                        async def _set_head(yaw, pitch):
                            await call_mcp_tool(conn, mcp_client, tool,
                                                _hj.dumps({"yaw": int(yaw), "pitch": int(pitch)}), timeout=8)

                        # 開機一次：先低頭休息。【不關 auto-torque】——持續通電 holding 會讓伺服電流
                        # 干擾 CoreS3 麥克風(實測會導致收音變靜音)。韌體預設閒置鬆力、靠靜摩擦hold位置即可。
                        try:
                            await _set_head(0, DOWN)
                            logger.bind(tag=TAG).info("🤖 頭部初始：低頭休息")
                        except Exception:
                            pass
                        last_mtime = 0
                        last_active = _ht.time()
                        is_down = True
                        while True:
                            if getattr(conn, "stop_event", None) and conn.stop_event.is_set():
                                break
                            if getattr(conn, "websocket", None) is None:
                                break
                            try:
                                # ① 手動直控(head_cmd.json)優先
                                if _hos.path.exists(cmd_f):
                                    m = _hos.path.getmtime(cmd_f)
                                    if m != last_mtime:
                                        last_mtime = m
                                        d = _hj.loads(open(cmd_f).read())
                                        await _set_head(d.get("yaw", 0), d.get("pitch", 45))
                                        logger.bind(tag=TAG).info(f"🤖 頭部直控 yaw={d.get('yaw')} pitch={d.get('pitch')}")
                                        is_down = False
                                        last_active = _ht.time()
                                # ② 喚醒→抬頭 / 閒置→低頭。
                                #    _hermes_wake_pending = silero 偵測「開口第一刻」設的持久旗標(喊jarvis一開口就抬)。
                                _wp = getattr(conn, "_hermes_wake_pending", 0)
                                active = ((_wp and _ht.time() - _wp < 6)
                                          or getattr(conn, "_hermes_in_turn", False)
                                          or getattr(conn, "client_is_speaking", False)
                                          or getattr(conn, "_hermes_in_continuous", False)  # 持續對話中→保持抬頭
                                          or getattr(conn, "just_woken_up", False))
                                if active:
                                    last_active = _ht.time()
                                    if is_down:                       # 剛喚醒/活躍 → 抬頭看你
                                        await _set_head(0, UP)
                                        is_down = False
                                        # 抬頭後丟一個「找臉」旗標讓臉追蹤接手(先抬頭再找人在哪)
                                        try:
                                            open("/opt/xiaozhi-esp32-server/data/find_face.flag", "w").write(str(_ht.time()))
                                        except Exception:
                                            pass
                                        logger.bind(tag=TAG).info("🤖 喚醒→抬頭看你(45)")
                                    try:
                                        conn._hermes_wake_pending = 0   # 已處理，清掉
                                    except Exception:
                                        pass
                                elif not is_down and _ht.time() - last_active > IDLE_S:
                                    await _set_head(0, DOWN)
                                    is_down = True
                                    logger.bind(tag=TAG).info("🤖 閒置→低頭休息(15)")
                            except Exception:
                                pass
                            await asyncio.sleep(0.3)

                    asyncio.create_task(_head_cmd_poller())

                    # Hermes 找人拍照：喚醒抬頭後(find_face.flag 存在)，在「沒講話、不在對話turn」的安靜
                    # 空檔連拍幾張 → Mac 端 face_tracker 讀 last_camera.jpg 找臉、寫 head_cmd.json 轉頭對準。
                    # 安靜時才拍，避免跟麥克風搶 websocket(拍照慢、會干擾收音)。
                    async def _face_find_poller():
                        import os as _fos
                        flag = "/opt/xiaozhi-esp32-server/data/find_face.flag"
                        await asyncio.sleep(13)
                        cam = None
                        for cn in ("self.camera.take_photo", "self_camera_take_photo"):
                            if mcp_client.has_tool(cn):
                                cam = cn
                                break
                        if not cam:
                            return
                        while True:
                            if getattr(conn, "stop_event", None) and conn.stop_event.is_set():
                                break
                            if getattr(conn, "websocket", None) is None:
                                break
                            try:
                                if _fos.path.exists(flag):
                                    busy = (getattr(conn, "client_is_speaking", False)
                                            or getattr(conn, "_hermes_in_turn", False))
                                    if not busy:
                                        await call_mcp_tool(conn, mcp_client, cam,
                                                            '{"question": "__capture__"}', timeout=12)
                                        await asyncio.sleep(0.8)
                                    else:
                                        await asyncio.sleep(0.4)
                                else:
                                    await asyncio.sleep(0.5)
                            except Exception:
                                await asyncio.sleep(0.6)

                    asyncio.create_task(_face_find_poller())
            return

    # Handle method calls (requests from the client)
    elif "method" in payload:
        method = payload["method"]
        logger.bind(tag=TAG).info(f"收到MCP客户端请求: {method}")

    elif "error" in payload:
        error_data = payload["error"]
        error_msg = error_data.get("message", "未知错误")
        logger.bind(tag=TAG).error(f"收到MCP错误响应: {error_msg}")

        msg_id = int(payload.get("id", 0))
        if msg_id in mcp_client.call_results:
            await mcp_client.reject_call_result(
                msg_id, Exception(f"MCP错误: {error_msg}")
            )


async def send_mcp_initialize_message(conn: "ConnectionHandler"):
    """发送MCP初始化消息"""

    vision_url = get_vision_url(conn.config)

    # 密钥生成token
    auth = AuthToken(conn.config["server"]["auth_key"])
    token = auth.generate_token(conn.headers.get("device-id"))

    vision = {
        "url": vision_url,
        "token": token,
    }

    payload = {
        "jsonrpc": "2.0",
        "id": 1,  # mcpInitializeID
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
                "vision": vision,
            },
            "clientInfo": {
                "name": "XiaozhiClient",
                "version": "1.0.0",
            },
        },
    }
    logger.bind(tag=TAG).debug("发送MCP初始化消息")
    await send_mcp_message(conn, payload)


async def send_mcp_tools_list_request(conn: "ConnectionHandler"):
    """发送MCP工具列表请求"""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID
        "method": "tools/list",
    }
    logger.bind(tag=TAG).debug("发送MCP工具列表请求")
    await send_mcp_message(conn, payload)


async def send_mcp_tools_list_continue_request(conn: "ConnectionHandler", cursor: str):
    """发送带有cursor的MCP工具列表请求"""
    payload = {
        "jsonrpc": "2.0",
        "id": 2,  # mcpToolsListID (same ID for continuation)
        "method": "tools/list",
        "params": {"cursor": cursor},
    }
    logger.bind(tag=TAG).info(f"发送带cursor的MCP工具列表请求: {cursor}")
    await send_mcp_message(conn, payload)


def _log_device_action(tool_name: str, ok: bool, args: str = ""):
    """把裝置動作結果回報給自我認知（8809）。__capture__ 連拍不記。
    用背景執行緒送，避免阻塞事件迴圈（跳舞 10 步等連續動作會累積成秒級卡頓）。"""
    try:
        if "__capture__" in str(args):
            return
        import urllib.request as _au, json as _aj, threading as _ath
        nmap = {"set_head_angles": "轉頭", "set_avatar": "變表情", "take_photo": "拍照看東西",
                "set_volume": "調音量", "set_blink": "眨眼", "set_message": "顯示文字",
                "led": "燈光", "set_all": "燈光"}
        label = next((v for k, v in nmap.items() if k in tool_name), tool_name.split(".")[-1])

        def _post():
            try:
                _au.urlopen(_au.Request(
                    "http://host.docker.internal:8809/action_log",
                    data=_aj.dumps({"action": label, "ok": ok}).encode(),
                    headers={"Content-Type": "application/json"}), timeout=2)
            except Exception:
                pass
        _ath.Thread(target=_post, daemon=True).start()
    except Exception:
        pass


async def call_mcp_tool(
    conn: "ConnectionHandler",
    mcp_client: MCPClient,
    tool_name: str,
    args: str = "{}",
    timeout: int = 30,
):
    """
    调用指定的工具，并等待响应
    """
    if not await mcp_client.is_ready():
        raise RuntimeError("MCP客户端尚未准备就绪")

    if not mcp_client.has_tool(tool_name):
        raise ValueError(f"工具 {tool_name} 不存在")

    tool_call_id = await mcp_client.get_next_id()
    result_future = asyncio.Future()
    await mcp_client.register_call_result_future(tool_call_id, result_future)

    # 处理参数
    try:
        if isinstance(args, str):
            # 确保字符串是有效的JSON
            if not args.strip():
                arguments = {}
            else:
                try:
                    # 尝试直接解析
                    arguments = json.loads(args)
                except json.JSONDecodeError:
                    # 如果解析失败，尝试合并多个JSON对象
                    try:
                        # 使用正则表达式匹配所有JSON对象
                        json_objects = re.findall(r"\{[^{}]*\}", args)
                        if len(json_objects) > 1:
                            # 合并所有JSON对象
                            merged_dict = {}
                            for json_str in json_objects:
                                try:
                                    obj = json.loads(json_str)
                                    if isinstance(obj, dict):
                                        merged_dict.update(obj)
                                except json.JSONDecodeError:
                                    continue
                            if merged_dict:
                                arguments = merged_dict
                            else:
                                raise ValueError(f"无法解析任何有效的JSON对象: {args}")
                        else:
                            raise ValueError(f"参数JSON解析失败: {args}")
                    except Exception as e:
                        logger.bind(tag=TAG).error(
                            f"参数JSON解析失败: {str(e)}, 原始参数: {args}"
                        )
                        raise ValueError(f"参数JSON解析失败: {str(e)}")
        elif isinstance(args, dict):
            arguments = args
        else:
            raise ValueError(f"参数类型错误，期望字符串或字典，实际类型: {type(args)}")

        # 确保参数是字典类型
        if not isinstance(arguments, dict):
            raise ValueError(f"参数必须是字典类型，实际类型: {type(arguments)}")

    except Exception as e:
        if not isinstance(e, ValueError):
            raise ValueError(f"参数处理失败: {str(e)}")
        raise e

    actual_name = mcp_client.name_mapping.get(tool_name, tool_name)
    payload = {
        "jsonrpc": "2.0",
        "id": tool_call_id,
        "method": "tools/call",
        "params": {"name": actual_name, "arguments": arguments},
    }

    logger.bind(tag=TAG).info(f"发送客户端mcp工具调用请求: {actual_name}，参数: {args}")
    await send_mcp_message(conn, payload)

    try:
        # Wait for response or timeout
        raw_result = await asyncio.wait_for(result_future, timeout=timeout)
        logger.bind(tag=TAG).info(
            f"客户端mcp工具调用 {actual_name} 成功，原始结果: {raw_result}"
        )

        if isinstance(raw_result, dict):
            if raw_result.get("isError") is True:
                error_msg = raw_result.get(
                    "error", "工具调用返回错误，但未提供具体错误信息"
                )
                raise RuntimeError(f"工具调用错误: {error_msg}")

            _log_device_action(tool_name, True, args)
            content = raw_result.get("content")
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    # 直接返回文本内容，不进行JSON解析
                    return content[0]["text"]
        # 如果结果不是预期的格式，将其转换为字符串
        return str(raw_result)
    except asyncio.TimeoutError:
        await mcp_client.cleanup_call_result(tool_call_id)
        _log_device_action(tool_name, False, args)
        raise TimeoutError("工具调用请求超时")
    except Exception as e:
        await mcp_client.cleanup_call_result(tool_call_id)
        _log_device_action(tool_name, False, args)
        raise e
