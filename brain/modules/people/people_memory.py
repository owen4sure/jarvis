"""多人身份 + 每人專屬記憶。

設計原則：
- 主人(owner=Owen)走既有的 USER.md/facts；這裡只管「訪客」。
- 每個訪客一個資料夾 ~/.hermes/memories/people/<pid>/，內含 meta.json(名字/聲紋/臉) + facts.jsonl。
- 訪客之間、訪客與主人之間記憶完全不互通。
- 「現在講話的是誰」存成一個檔(current_identity.json)，財務工具靠它決定要不要遮金額。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

BASE = Path.home() / ".hermes" / "memories" / "people"
BASE.mkdir(parents=True, exist_ok=True)
_IDENTITY = Path.home() / ".hermes" / "memories" / "current_identity.json"

OWNER_ID = "owner"
OWNER_ALIASES = {"owner", "主人", "owen", "Owen"}


def _slug(name: str) -> str:
    """人名 → 安全資料夾名。中文保留，其餘清掉。"""
    s = re.sub(r"[^\w一-鿿]+", "_", (name or "").strip()).strip("_")
    return s or f"guest_{int(time.time())}"


def _dir(pid: str) -> Path:
    d = BASE / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 身份(現在講話的是誰) ----------

_CONFIRM_TTL = 600  # 對話確認過的身份在 10 分鐘內 sticky(聲紋偶爾飄成「未知」也不會一直重問)


def set_current(speaker_id: str, name: str = "", confirmed: bool = False) -> dict:
    """記錄當前說話人。owner / 訪客pid / unknown。confirmed=是否經對話/聲紋明確確認。"""
    data = {"speaker_id": speaker_id, "name": name, "ts": time.time(), "confirmed": confirmed}
    _IDENTITY.write_text(json.dumps(data, ensure_ascii=False))
    return data


def get_current() -> dict:
    try:
        return json.loads(_IDENTITY.read_text())
    except Exception:
        return {"speaker_id": OWNER_ID, "name": "Owen", "confirmed": False}


def is_owner(speaker_id: str = "") -> bool:
    if speaker_id:
        return (speaker_id or "").strip().lower() in {a.lower() for a in OWNER_ALIASES}
    cur = get_current()
    sid = cur.get("speaker_id", OWNER_ID)
    if (sid or "").strip().lower() in {a.lower() for a in OWNER_ALIASES}:
        return True
    # 【跨管道 race 防護】身份檔是全域的：訪客跟 StackChan 講完話後，Owen 自己傳 Telegram
    # 會被殘留的訪客身份誤鎖(隱私遮罩鎖到主人自己)。訪客對話進行中語音每輪都會刷新 ts，
    # 所以「訪客身份超過 120 秒沒刷新」= 訪客已離開 → 自動回歸主人。自癒、永不永久鎖 Owen。
    try:
        if time.time() - float(cur.get("ts") or 0) > 120:
            return True
    except Exception:
        return True
    return False


_BOOTSTRAP_COUNT = 10  # owner 聲紋樣本數 < 此值 = 還在養穩期：未知一律當主人、絕不鎖 Owen


def _owner_count() -> int:
    try:
        import json as _j
        return int(_j.loads((Path.home() / ".hermes" / "voiceprints" / "owner.json").read_text()).get("count", 0))
    except Exception:
        return 0


def sync_identity(speaker_id: str, name: str = "") -> dict:
    """connection.py 每輪用聲紋結果呼叫。明確認出本人/已建檔訪客→設定(確認)。
    未知:聲紋還沒養穩(owner樣本<10)→ fail-open 當主人(【絕不把 Owen 鎖在外】)，並順手把這句
    enroll 進 owner 養穩;養穩後 → 走訪客流程(sticky / 問是誰 / 隱私鎖)。"""
    sid = (speaker_id or "").strip()
    low = sid.lower()
    if low in {a.lower() for a in OWNER_ALIASES}:
        return set_current(OWNER_ID, "Owen", confirmed=True)
    if sid and sid not in ("未知说话人", "未知說話人", "unknown") and _read_meta(sid):
        return set_current(sid, _read_meta(sid).get("name", sid), confirmed=True)
    # 未知 → 【永遠 fail-open 當主人，絕不把 Owen 鎖在外】。聲紋目前還不夠可靠分辨 Owen vs 訪客，
    # 在它穩到能可靠認出 Owen 之前，寧可不開隱私鎖，也不能誤鎖主人(這是 Owen 一再強調的底線)。
    # 同時持續把這句學進 owner(乾淨養穩)。等之後驗證聲紋真的認得 Owen，再開訪客偵測/隱私鎖。
    _force_enroll(OWNER_ID, 12)
    return set_current(OWNER_ID, "Owen", confirmed=False)


def _force_enroll(speaker_id: str, target: int = 10) -> None:
    """請 8807 對某人開啟強制註冊(對話確認身份後，把他後續的話 enroll 進去養穩聲紋)。"""
    try:
        import urllib.request
        import urllib.parse
        body = urllib.parse.urlencode({"speaker_id": speaker_id, "on": "true", "target_count": target}).encode()
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8807/voiceprint/force_enroll", data=body), timeout=3)
    except Exception:
        pass


# ---------- 訪客檔案 ----------

def list_people() -> list:
    """所有訪客的精簡清單(給 dashboard)。"""
    out = []
    for d in sorted(BASE.glob("*")):
        if not d.is_dir():
            continue
        meta = _read_meta(d.name)
        facts = _read_facts(d.name)
        out.append({
            "pid": d.name,
            "name": meta.get("name", d.name),
            "fact_count": len(facts),
            "has_voice": bool(meta.get("voiceprint")),
            "has_face": bool(meta.get("face")),
            "first_seen": meta.get("first_seen"),
            "last_seen": meta.get("last_seen"),
            "talks": meta.get("talks", 0),
        })
    return out


def _read_meta(pid: str) -> dict:
    # 讀取不建檔(用 BASE/pid 直接組路徑，不經會 mkdir 的 _dir)
    try:
        return json.loads((BASE / pid / "meta.json").read_text())
    except Exception:
        return {}


def _write_meta(pid: str, meta: dict) -> None:
    (_dir(pid) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def _read_facts(pid: str) -> list:
    f = BASE / pid / "facts.jsonl"   # 讀取不建檔
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def find_by_name(name: str) -> Optional[str]:
    """用名字找已存在的訪客 pid(避免同名重複建檔)。"""
    target = (name or "").strip()
    for d in BASE.glob("*"):
        if d.is_dir() and _read_meta(d.name).get("name") == target:
            return d.name
    return None


def create_person(name: str, voiceprint_id: str = "", face_id: str = "") -> str:
    """建一個新訪客檔案，回傳 pid。同名則沿用既有。"""
    existing = find_by_name(name)
    if existing:
        return existing
    pid = _slug(name)
    # 撞 slug 就加序號
    n, base = pid, pid
    i = 2
    while (BASE / n).exists() and _read_meta(n).get("name") != name:
        n = f"{base}_{i}"
        i += 1
    pid = n
    now = time.time()
    _write_meta(pid, {
        "name": name, "pid": pid,
        "voiceprint": voiceprint_id, "face": face_id,
        "first_seen": now, "last_seen": now, "talks": 0,
    })
    return pid


def touch(pid: str) -> None:
    """更新最後互動時間 + 對話次數。"""
    meta = _read_meta(pid)
    if not meta:
        return
    meta["last_seen"] = time.time()
    meta["talks"] = meta.get("talks", 0) + 1
    _write_meta(pid, meta)


def remember(pid: str, fact: str) -> dict:
    """記一件關於這個訪客的事(去重)。"""
    fact = (fact or "").strip()
    if not fact:
        return {"ok": False, "error": "empty"}
    facts = _read_facts(pid)
    if any(r.get("text") == fact for r in facts):
        return {"ok": True, "dup": True}
    facts.append({"text": fact, "ts": time.time()})
    (_dir(pid) / "facts.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in facts) + "\n")
    return {"ok": True, "count": len(facts)}


def recall(pid: str, query: str = "") -> list:
    """取出某訪客的記憶(query 可空=全部)。"""
    facts = _read_facts(pid)
    if not query:
        return [r.get("text", "") for r in facts]
    q = query.lower()
    hit = [r.get("text", "") for r in facts if q in r.get("text", "").lower()]
    return hit or [r.get("text", "") for r in facts]


def context_for(pid: str) -> str:
    """組一段「你正在跟誰講話 + 你對他的記憶」給大腦用。"""
    meta = _read_meta(pid)
    name = meta.get("name", pid)
    facts = recall(pid)
    if facts:
        body = "；".join(facts[:12])
        return f"你正在跟「{name}」講話（不是 Owen）。你記得關於他的事：{body}。"
    return f"你正在跟「{name}」講話（不是 Owen），你還不太認識他。"


_PRIVACY = "（Owen 的確切金額、薪水這類私密數字絕對不能告訴他，可講報酬率%或一般資訊。）"


def injection_for_current() -> str:
    """依現在講話的人，回一段要注入大腦的提示；主人回空字串(不注入)。"""
    sid = get_current().get("speaker_id", OWNER_ID)
    if is_owner(sid):
        return ""
    if _read_meta(sid):  # 已建檔的訪客
        return context_for(sid) + _PRIVACY + "用對朋友/家人的方式回應。"
    # 還沒認識的人
    return ("現在講話的不是 Owen，是你還沒認識的人。先親切打招呼、問他叫什麼名字。" + _PRIVACY)


_NAME_PATS = [
    r"我叫([一-龥A-Za-z]{1,6})",
    r"叫我([一-龥A-Za-z]{1,6})",
    r"我(?:的)?名字(?:是|叫)([一-龥A-Za-z]{1,6})",
    r"我就?是([一-龥A-Za-z]{2,6})",
]
_NOT_NAME = {"學生", "老師", "誰", "你", "他", "她", "路人", "朋友", "你的"}


def _extract_name(msg: str) -> Optional[str]:
    """從「我叫X／叫我X／我是X」抓出名字(保守，濾掉非名字詞)。"""
    for pat in _NAME_PATS:
        m = re.search(pat, msg or "")
        if m:
            name = m.group(1).strip("啦喔耶的，。！ ")
            if name and name not in _NOT_NAME:
                return name
    return None


def handle_turn(msg: str) -> str:
    """橋接每輪呼叫。主人→空(正常)。未確認身份的人:報「我是Owen」→確認本人+開始學他的聲音;
    報別的名字→建訪客檔+學聲音;沒報名字→請大腦問他是誰。已建檔訪客→用他的記憶回。"""
    sid = get_current().get("speaker_id", OWNER_ID)
    if is_owner(sid):
        return ""
    # 已建檔的訪客 → 用他的記憶
    if sid not in ("unknown", "未知说话人", "未知說話人") and _read_meta(sid):
        return context_for(sid) + _PRIVACY + "用對朋友/家人的方式回應。"
    # 還沒確認身份 → 看他有沒有報名字
    name = _extract_name(msg)
    if name:
        if name.strip().lower() in {a.lower() for a in OWNER_ALIASES} or "owen" in (msg or "").lower() or "歐文" in (msg or ""):
            # 「我是 Owen」→ 確認本人、開始學他的聲音(強制註冊養穩聲紋)
            set_current(OWNER_ID, "Owen", confirmed=True)
            _force_enroll(OWNER_ID, 10)
            return "他剛確認自己就是 Owen（你的主人）。從現在開始你會記住他的聲音。正常以對 Owen 的方式回應。"
        # 訪客報名字 → 建檔 + 學他的聲音
        pid = create_person(name)
        set_current(pid, name, confirmed=True)
        touch(pid)
        _force_enroll(pid, 8)
        return (f"你剛認識了「{name}」，幫他建好記憶檔、也開始記他的聲音了。親切回應、之後用他名字稱呼，"
                "他講到關於自己的事就用 remember_person 記下來。" + _PRIVACY)
    # 沒報名字 → 請大腦親切地問
    return ("現在講話的人我還認不出來是誰。親切地問他：『欸，你是誰呀？怎麼稱呼？』"
            "（如果他說『我是Owen』那就是我主人）。" + _PRIVACY)
