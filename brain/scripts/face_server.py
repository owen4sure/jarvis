"""本機人臉辨識服務（仿聲紋 voiceprint_server）。

StackChan 拍照 → 這裡用 face_recognition(dlib 128 維編碼)認出是誰。
和聲紋/每人記憶共用同一個 pid，所以「聲音認到誰」和「臉認到誰」指的是同一個人。

端點：
  POST /face/enroll    (person_id, file)  -> {"ok": true}
  POST /face/identify  (file)             -> {"person": "<best或空>", "score": float}
  GET  /health

設計同聲紋：running mean 強化、高信心自動學習、陌生人不會誤判成已知。
跑在 8812。
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import FastAPI, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse

try:
    import face_recognition  # dlib
except Exception:  # 安裝完成前先別爆
    face_recognition = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("face")

STORE = Path.home() / ".hermes" / "faces"
STORE.mkdir(parents=True, exist_ok=True)
# face_recognition 標準門檻：距離<0.6 算同一人。轉成 score=1-dist 方便和聲紋一致(越大越像)。
MATCH = 0.58
app = FastAPI(title="Hermes Face")


def _path(pid: str) -> Path:
    safe = "".join(c for c in pid if c.isalnum() or c in ("-", "_", ".")) or "default"
    return STORE / f"{safe}.json"


def encode(raw: bytes) -> Optional[np.ndarray]:
    """一張圖 → 最大那張臉的 128 維編碼(沒臉回 None)。"""
    if face_recognition is None:
        return None
    try:
        img = face_recognition.load_image_file(io.BytesIO(raw))
        locs = face_recognition.face_locations(img)
        if not locs:
            return None
        # 取面積最大的臉(最靠近/最主要的人)
        locs.sort(key=lambda b: (b[2] - b[0]) * (b[1] - b[3]), reverse=True)
        encs = face_recognition.face_encodings(img, [locs[0]])
        return np.asarray(encs[0], dtype=np.float32) if encs else None
    except Exception as e:
        log.warning("encode failed: %s", e)
        return None


def load(pid: str) -> Optional[np.ndarray]:
    p = _path(pid)
    if not p.exists():
        return None
    try:
        return np.asarray(json.loads(p.read_text())["encoding"], dtype=np.float32)
    except Exception:
        return None


def save(pid: str, enc: np.ndarray) -> None:
    """running mean，越多張越穩。"""
    p = _path(pid)
    count, acc = 0, np.zeros_like(enc)
    if p.exists():
        try:
            o = json.loads(p.read_text())
            count = int(o.get("count", 1))
            acc = np.asarray(o["encoding"], dtype=np.float32) * count
        except Exception:
            count, acc = 0, np.zeros_like(enc)
    count += 1
    mean = (acc + enc) / count
    p.write_text(json.dumps({"encoding": mean.tolist(), "count": count}))


def enrolled() -> List[str]:
    return sorted(p.stem for p in STORE.glob("*.json"))


@app.get("/health")
def health():
    return {"ok": True, "ready": face_recognition is not None, "enrolled": enrolled()}


@app.post("/face/enroll")
async def enroll(person_id: str = Form(...), file: UploadFile = File(...),
                 authorization: Optional[str] = Header(None)):
    enc = encode(await file.read())
    if enc is None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "no face / not ready"})
    save(person_id, enc)
    # 標記這個人有臉了
    try:
        import sys
        sys.path.insert(0, str(Path.home() / "Hermes_Brain"))
        from modules.people import people_memory as pm
        meta = pm._read_meta(person_id)
        if meta:
            meta["face"] = True
            pm._write_meta(person_id, meta)
    except Exception:
        pass
    log.info("enrolled face %s", person_id)
    return {"ok": True, "person_id": person_id}


@app.post("/face/identify")
async def identify(file: UploadFile = File(...), authorization: Optional[str] = Header(None)):
    enc = encode(await file.read())
    if enc is None:
        return {"person": "", "score": 0.0, "reason": "no_face"}
    best, best_d = "", 9.9
    for pid in enrolled():
        ref = load(pid)
        if ref is None:
            continue
        d = float(np.linalg.norm(enc - ref))
        if d < best_d:
            best_d, best = d, pid
    if best_d > MATCH:
        return {"person": "", "score": round(max(0.0, 1 - best_d), 4)}
    # 自動學習：認得很準就把這張加進去強化(門檻比辨識更嚴，避免誤學)
    if best_d < MATCH * 0.8:
        try:
            save(best, enc)
        except Exception:
            pass
    return {"person": best, "score": round(1 - best_d, 4)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8812, log_level="warning")
