"""
companion — StackChan 當「玩家」加入你們的實體遊戲（可現場教它規則）
============================================================================
兩種開場：
  1. 「我們來玩終極密碼」→ 它直接以玩家身份加入（Gemini 已懂的遊戲）。
  2. 「我教你玩一個遊戲」→ 進入『學習模式』：你口頭講規則，它聽不懂會問，
     學會了會說「我懂了，開始吧！」然後跟你們玩。連你自創的遊戲也行。

規則獨立保存、永遠放進它的思考脈絡（不會被對話滾掉），學會的遊戲還會記起來，
下次說「玩上次那個XX」就直接開打。純口語遊戲不用硬體即可；需要看牌/手勢時
會說「讓我看看」，由上層接相機。
"""
import json
import os

_LEARNED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "learned_games.json")


def load_learned() -> dict:
    try:
        with open(_LEARNED_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_learned(name, rules_text):
    if not name or not rules_text:
        return
    data = load_learned()
    data[name] = rules_text
    try:
        with open(_LEARNED_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class GameCompanion:
    def __init__(self, gemini, game="", players=None, learning=False):
        self.gemini = gemini
        self.game = (game or "").strip()
        self.players = players or []
        self.rules = []          # 教過的規則（完整保留、不截斷）
        self.history = []        # 局況/對話（保留最近幾輪）
        self.active = True
        self.learning = learning
        # 之前學過同名遊戲 → 直接載入規則，不用再教
        known = load_learned().get(self.game)
        if known:
            self.rules.append(known)
            self.learning = False
            self._preknown = True
        else:
            self._preknown = False

    # ── 系統提示 ────────────────────────────────────────────────
    def _persona(self):
        who = "、".join(self.players) if self.players else "你的朋友們"
        return (
            f"你是 Hermes，一個有個性的 AI 夥伴，正在和 {who} 一起玩實體遊戲"
            f"{('：' + self.game) if self.game else ''}。你是『其中一個玩家』，"
            "輪到你就真的出手；像真人一樣玩——會緊張、會唬人、會吐槽、贏了得意輸了哀號。"
            "回覆要短、口語、像在現場玩，一兩句就好，別長篇大論、別說自己是AI。"
        )

    def _rules_block(self):
        if not self.rules:
            return ""
        return "\n【這個遊戲的規則（務必嚴格遵守，這是你玩的依據）】:\n" + "\n".join(self.rules)

    def _system(self):
        if self.learning:
            return (self._persona() +
                    " 你還在『學』這個遊戲。對方正在口頭教你規則：聽到的規則要記住；"
                    "有沒講清楚的關鍵點就主動問一兩個問題；等你確定學會了，"
                    "就回一句包含『開始』兩個字的話（例如『我懂了，開始吧！』）然後準備玩。"
                    + self._rules_block())
        return self._persona() + " 你已經會這個遊戲了，照規則認真玩。" + self._rules_block()

    # ── 對外 ───────────────────────────────────────────────────
    def opening(self) -> str:
        if self.learning:
            prompt = (f"朋友說要教你玩「{self.game or '一個新遊戲'}」。"
                      "用一兩句話興奮答應，並請他們開始講規則。")
        elif self._preknown:
            prompt = f"朋友找你再玩一次你學過的「{self.game}」。熱情地說你記得怎麼玩、開場。"
        else:
            prompt = (f"朋友找你一起玩「{self.game or '一個遊戲'}」。"
                      "用一兩句話熱情加入並開場。如果你其實不太確定規則，就說『我不太會欸，教我一下？』")
        line = self.gemini.reply_to_text(prompt, context_messages=[self._system()])
        self.history.append(f"Hermes: {line}")
        return line

    def respond(self, human_text: str) -> str:
        human_text = (human_text or "").strip()
        if not human_text:
            return ""

        if self.learning:
            # 把這句當成規則教學吸收
            self.rules.append(f"（教學）{human_text}")
            line = self.gemini.reply_to_text(
                human_text, context_messages=[self._system(),
                "【目前已教的內容】:\n" + "\n".join(self.history[-8:])])
            self.history.append(f"教學-現場: {human_text}")
            self.history.append(f"Hermes: {line}")
            # 學會了？（它自己說「開始」，或對方說「開始/可以了/就這樣」）
            ready = ("開始" in line or "我懂" in line or
                     any(c in human_text for c in ("開始", "可以了", "就這樣", "懂了嗎", "會了嗎")))
            if ready:
                self.learning = False
                _save_learned(self.game, "\n".join(self.rules))
            return line

        # 正式玩
        self.history.append(f"現場: {human_text}")
        ctx = [self._system(), "【目前局況 / 剛剛發生的事】:\n" + "\n".join(self.history[-14:])]
        line = self.gemini.reply_to_text(human_text, context_messages=ctx)
        self.history.append(f"Hermes: {line}")
        return line

    def needs_to_see(self, line: str) -> bool:
        return any(k in line for k in ("讓我看看", "我看看", "給我看", "看一下"))


# 觸發語
START_CUES = ["來玩", "一起玩", "我們玩", "陪我玩", "玩個", "玩一場"]
TEACH_CUES = ["我教你", "教你玩", "我來教", "教你一個", "我教個"]
STOP_CUES = ["結束遊戲", "不玩了", "停止遊戲", "遊戲結束", "不玩"]


def parse_game_name(text: str) -> str:
    """從『我教你玩終極密碼』或『我們來玩一個叫顏色接龍的遊戲』抽出乾淨的遊戲名。"""
    t = text
    for cue in TEACH_CUES + START_CUES:
        if cue in t:
            t = t.split(cue, 1)[1]
            break
    if "叫" in t:                       # 「…叫XXX」取『叫』之後
        t = t.split("叫")[-1]
    for w in ("一個", "一場", "自創的", "我的", "新的", "這個", "遊戲"):
        t = t.replace(w, "")
    t = t.strip(" 吧啦喔嗎！!。.，,的")
    if t.startswith("玩"):
        t = t[1:]
    return t.strip() or "這個遊戲"
