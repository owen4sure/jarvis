"""
meeting — 把 StackChan 放會議室，全程聆聽，散會後自動產出會議報告
============================================================================
firmware 的 listen 單次上限 30 秒，所以用「分段連續聆聽」：每段最多 30 秒、
即時轉文字、累積整場逐字稿，直到你喊散會（或手動停），再用既有的 Plaud 智能層
產出摘要 + 待辦 + 決策報告，推到 Telegram，並用一句話口頭報告重點。

限制（硬體本質）：
- 段與段之間有極小間隙（轉檔/握手），可能漏掉一兩個字 → 近乎完整、非逐字法庭級。
- 現場分段用 faster-whisper，無跨整場語者分離（檔案上傳模式才有）。摘要/待辦不受影響。
- StackChan 麥克風小，最好放在離說話者幾公尺內；大會議室遠端輕聲可能收不到。
"""
import os
import threading
import time

CHUNK_MS = 30000
MAX_HOURS = 3
_LOCK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "memory", "meeting.lock")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           "memory", "reports", "meetings")

# 散會 / 結束的口語線索（現場聆聽到這些就自動收工）
END_CUES = ["散會", "會議結束", "開完了", "到此結束", "今天就到這", "就到這裡", "結束會議", "先這樣"]


class MeetingRecorder:
    _instance = None

    def __init__(self, robot=None, gemini=None, on_done=None):
        from .stackchan_mcp_client import StackChanClient
        from .gemini_client import GeminiClient
        self.robot = robot or StackChanClient()
        self.gemini = gemini or GeminiClient()
        self.on_done = on_done            # 完成時的 callback(report, analysis)
        self.segments = []
        self.active = False
        self._thread = None
        self._t0 = None

    # ── 控制 ───────────────────────────────────────────────
    def start(self, label="現場會議"):
        if self.active:
            return "我已經在記錄這場會議了。"
        self.label = label
        self.segments = []
        self.active = True
        self._t0 = time.time()
        try:
            open(_LOCK, "w").write(str(os.getpid()))
        except Exception:
            pass
        # 紅燈 + 思考臉 = 錄音中
        try:
            self.robot.set_all_leds(200, 0, 0)
            self.robot.set_avatar("thinking")
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        MeetingRecorder._instance = self
        return f"好，我開始記錄「{label}」了，會全程聽著。結束時跟我說一聲『散會』就好。"

    def stop(self):
        if not self.active:
            return None
        self.active = False
        if self._thread:
            self._thread.join(timeout=CHUNK_MS / 1000 + 5)
        try:
            os.remove(_LOCK)
        except Exception:
            pass
        try:
            self.robot.clear_leds()
            self.robot.set_avatar("idle")
        except Exception:
            pass
        return self._produce_report()

    # ── 聆聽迴圈 ────────────────────────────────────────────
    def _loop(self):
        while self.active:
            if time.time() - self._t0 > MAX_HOURS * 3600:
                self.active = False
                break
            try:
                r = self.robot.call_tool("listen", {
                    "duration_ms": CHUNK_MS, "engine": "faster-whisper",
                    "language": "zh", "motion": "none"})
                text = self._extract_text(r)
                if text:
                    mmss = time.strftime("%M:%S", time.gmtime(time.time() - self._t0))
                    self.segments.append({"timestamp": mmss, "speaker": "會議", "text": text})
                    # 現場聽到「散會」之類 → 自動收工
                    if any(c in text for c in END_CUES):
                        self.active = False
                        break
            except Exception as e:
                print(f"⚠️ [Meeting] chunk 失敗，繼續: {e}")
                time.sleep(1)

    @staticmethod
    def _extract_text(r):
        import json
        res = r.get("result", r) if isinstance(r, dict) else {}
        content = res.get("content") if isinstance(res, dict) else None
        if content and isinstance(content, list):
            for c in content:
                t = c.get("text", "")
                try:
                    j = json.loads(t)
                    if isinstance(j, dict) and j.get("text"):
                        return j["text"].strip()
                except Exception:
                    if t.strip():
                        return t.strip()
        return ""

    def _produce_report(self):
        from modules.productivity.plaud_style.integrator import PlaudIntegrator
        if not self.segments:
            return {"report": None, "summary": "這場我幾乎沒聽到內容（可能太遠或太安靜），沒辦法總結。"}
        os.makedirs(REPORTS_DIR, exist_ok=True)
        path = os.path.join(REPORTS_DIR, time.strftime("%Y%m%d_%H%M%S_meeting.md"))
        report, analysis, actions = PlaudIntegrator().summarize_transcript(
            self.segments, source_label=self.label, output_report_path=path)
        result = {"report": report, "path": path,
                  "summary": analysis.get("summary", "已產出會議報告。"),
                  "tasks": actions.get("tasks", []), "decisions": actions.get("decisions", [])}
        if self.on_done:
            try:
                self.on_done(result)
            except Exception:
                pass
        return result


def active_recorder():
    inst = MeetingRecorder._instance
    return inst if (inst and inst.active) else None
