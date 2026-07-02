"""Quiz/question generator + grader for the Stack-chan "Claude 出題口頭評分"
wish-list item.

`/quiz [主題]` asks Gemini for a short question (optionally about a topic)
and stores the expected answer in `config/quiz_state.json`. `/answer <文字>`
grades the user's reply against that stored question with Gemini and clears
the state.

This covers the text-based half of the wish (question generation + grading
via Gemini). The "口頭" (spoken) half - the question being read aloud by
StackChan and the answer captured by its microphone - depends on the
StackChan voice loop (modules/embodied/audio_bridge.py), which already
round-trips through the same GeminiClient; once hardware arrives, the
voice loop can call `new_question()`/`grade_answer()` the same way the
Telegram commands below do.
"""

import json
import os

from modules.embodied.gemini_client import GeminiClient

STATE_PATH = "/Users/chenyouwei/Hermes_Brain/config/quiz_state.json"


def _load():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _clear():
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)


def new_question(topic=""):
    topic_hint = f"主題：{topic}。" if topic else "主題不限，可以是知識、語言、邏輯或常識。"
    prompt = (
        f"請出一個簡短的問答題給我練習。{topic_hint}"
        '輸出 JSON：{"question": "題目", "answer": "標準答案"}。'
        "題目要適合口頭回答（不要選擇題、不要太長），只輸出 JSON。"
    )
    fallback = {"question": "今天天氣如何？", "answer": "依實際天氣回答即可"}
    data = GeminiClient().generate_json(prompt, fallback)
    _save({"question": data["question"], "answer": data["answer"]})
    return data["question"]


def grade_answer(user_answer):
    state = _load()
    if state is None:
        return "目前沒有等待回答的題目。用 /quiz 出一題吧。"

    prompt = (
        f"題目：{state['question']}\n"
        f"標準答案：{state['answer']}\n"
        f"使用者的回答：{user_answer}\n\n"
        "請評分使用者的回答是否正確（可以有不同講法，只要意思對即可），"
        "用 1-2 句話給予簡短回饋與正確答案，繁體中文，口語化，不要用 markdown。"
    )
    feedback = GeminiClient().chat(prompt)
    _clear()
    return feedback
