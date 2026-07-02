import os
import datetime

from modules.productivity.plaud_style.stt_engine import STTEngine
from modules.productivity.plaud_style.summarizer import Summarizer
from modules.productivity.plaud_style.action_extractor import ActionExtractor


class PlaudIntegrator:
    def __init__(self):
        self.stt = STTEngine()
        self.summarizer = Summarizer()
        self.extractor = ActionExtractor()

    def run_pipeline(self, audio_path, output_report_path):
        print(f"🚀 [Plaud-Style] Starting Full Pipeline for: {audio_path}")

        # 1. Transcription + diarization (real Gemini audio understanding)
        transcript = self.stt.transcribe(audio_path)

        # 2. Intelligence extraction (real Gemini text analysis)
        analysis = self.summarizer.summarize(transcript)
        actions = self.extractor.extract_tasks(transcript)

        # 3. Report generation
        report = self._generate_polished_report(os.path.basename(audio_path), transcript, analysis, actions)

        # 4. Save to file
        os.makedirs(os.path.dirname(output_report_path), exist_ok=True)
        with open(output_report_path, "w", encoding="utf-8") as f:
            f.write(report)

        print(f"✅ [Plaud-Style] Report successfully generated at: {output_report_path}")
        return report

    def summarize_transcript(self, transcript, source_label="現場會議", output_report_path=None):
        """給已經有逐字稿（list of {timestamp,speaker,text}）的情境用——
        例如 StackChan 現場分段聆聽累積出來的逐字稿。重用摘要/待辦/報告層。"""
        analysis = self.summarizer.summarize(transcript)
        actions = self.extractor.extract_tasks(transcript)
        report = self._generate_polished_report(source_label, transcript, analysis, actions)
        if output_report_path:
            os.makedirs(os.path.dirname(output_report_path), exist_ok=True)
            with open(output_report_path, "w", encoding="utf-8") as f:
                f.write(report)
        return report, analysis, actions

    def _generate_polished_report(self, source_label, transcript, analysis, actions):
        report = []
        report.append("# 🌌 Plaud-Style Meeting Intelligence Report")
        report.append(f"**Timestamp:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"**Source:** `{source_label}`")
        report.append("\n---\n")

        # Section 1: Executive Summary
        report.append("## 🎯 1. Executive Intelligence Summary")
        report.append(f"> {analysis.get('summary', '（無摘要）')}")
        report.append("\n### 🔍 Key Insights")
        topics = analysis.get("topics") or []
        if not topics:
            report.append("_未識別出明確主題。_")
        for topic in topics:
            report.append(f"- **Topic:** {topic}")
        report.append(f"\n**Meeting Sentiment:** `{analysis.get('sentiment', '未知')}`")
        report.append(f"**Atmosphere:** `{analysis.get('atmosphere', '未知')}`")

        report.append("\n---\n")

        # Section 2: Transcript
        report.append("## 📝 2. Verbatim Transcript (Speaker-Diarized)")
        if not transcript:
            report.append("_無逐字稿內容。_")
        for entry in transcript:
            report.append(
                f"- **[{entry.get('timestamp', '?')}] {entry.get('speaker', 'Unknown')}**: "
                f"{entry.get('text', '')}"
            )

        report.append("\n---\n")

        # Section 3: Actionable Intelligence
        report.append("## ✅ 3. Actionable Intelligence")
        report.append("### 📋 Task Assignments")
        tasks = actions.get("tasks") or []
        if not tasks:
            report.append("_No specific tasks identified._")
        else:
            for task in tasks:
                report.append(
                    f"- [ ] **{task.get('owner', '?')}**: {task.get('task', '')} "
                    f"`(Deadline: {task.get('deadline', 'TBD')})`"
                )

        report.append("\n### ⚖️ Decisions")
        decisions = actions.get("decisions") or []
        if not decisions:
            report.append("_No formal decisions recorded._")
        else:
            for decision in decisions:
                report.append(f"- {decision}")

        report.append("\n---\n")
        report.append("**End of Intelligence Report**")

        return "\n".join(report)
