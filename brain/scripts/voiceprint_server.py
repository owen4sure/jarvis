#!/usr/bin/env python3
"""Local speaker-verification (voiceprint) HTTP service.

Recognizes the OWNER's voice and rejects background voices / TV / noise.
Uses Resemblyzer (small pretrained speaker encoder, torch CPU) producing
256-d L2-normalized embeddings; cosine similarity for matching.

Endpoints:
  POST /voiceprint/identify  -> {"speaker_id": "<best or empty>", "score": float}
  POST /voiceprint/enroll    -> {"ok": true, "speaker_id": ...}
  GET  /health               -> {"ok": true, "enrolled": [...]}

Runs on port 8807.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Form, UploadFile, File, Header
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = 8807
STORE_DIR = Path("/Users/USERNAME/.hermes/voiceprints")
STORE_DIR.mkdir(parents=True, exist_ok=True)
TARGET_SR = 16000
MIN_SAMPLES = TARGET_SR // 2  # require >= 0.5s of audio
_FORCE_ENROLL: dict = {}  # speaker_id -> target_count（對話式強制註冊：開啟後 identify 收到的音訊都 enroll 進去直到達標）

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voiceprint")

# ---------------------------------------------------------------------------
# Model (lazy-loaded so the process starts fast and import errors are visible)
# ---------------------------------------------------------------------------
_encoder = None


def get_encoder():
    global _encoder
    if _encoder is None:
        from resemblyzer import VoiceEncoder

        _encoder = VoiceEncoder(device="cpu", verbose=False)
        log.info("VoiceEncoder loaded on cpu")
    return _encoder


# ---------------------------------------------------------------------------
# Audio handling
# ---------------------------------------------------------------------------
def _resample(wav: np.ndarray, sr: int, target: int = TARGET_SR) -> np.ndarray:
    if sr == target or wav.size == 0:
        return wav
    # Linear interpolation resample (no scipy/librosa dependency required here).
    duration = wav.shape[0] / float(sr)
    n_out = int(round(duration * target))
    if n_out <= 1:
        return wav
    x_old = np.linspace(0.0, duration, wav.shape[0], endpoint=False)
    x_new = np.linspace(0.0, duration, n_out, endpoint=False)
    return np.interp(x_new, x_old, wav).astype(np.float32)


def decode_wav(raw: bytes) -> Optional[np.ndarray]:
    """Decode WAV bytes -> mono float32 @ 16k. Returns None if undecodable/too short."""
    if not raw:
        return None
    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("WAV decode failed: %s", e)
        return None
    if data is None or data.size == 0:
        return None
    # Stereo -> mono
    if data.ndim == 2:
        data = data.mean(axis=1)
    data = np.asarray(data, dtype=np.float32).flatten()
    data = _resample(data, sr, TARGET_SR)
    if data.shape[0] < MIN_SAMPLES:
        return None
    # Reject (near) silence: too quiet to verify a speaker.
    if float(np.sqrt(np.mean(data ** 2))) < 1e-4:
        return None
    return data


def embed(raw: bytes) -> Optional[np.ndarray]:
    """Compute an L2-normalized speaker embedding from WAV bytes, or None."""
    wav = decode_wav(raw)
    if wav is None:
        return None
    try:
        from resemblyzer import preprocess_wav

        # preprocess_wav expects float wav already at the source rate; we pass 16k.
        proc = preprocess_wav(wav, source_sr=TARGET_SR)
        if proc is None or proc.size < MIN_SAMPLES:
            return None
        emb = get_encoder().embed_utterance(proc)
    except Exception as e:
        log.warning("Embedding failed: %s", e)
        return None
    emb = np.asarray(emb, dtype=np.float32)
    n = float(np.linalg.norm(emb))
    if n < 1e-8:
        return None
    return emb / n


# ---------------------------------------------------------------------------
# Enrollment store
# ---------------------------------------------------------------------------
def _path(speaker_id: str) -> Path:
    safe = "".join(c for c in speaker_id if c.isalnum() or c in ("-", "_", "."))
    safe = safe or "default"
    return STORE_DIR / f"{safe}.json"


def load_embedding(speaker_id: str) -> Optional[np.ndarray]:
    p = _path(speaker_id)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text())
        emb = np.asarray(obj["embedding"], dtype=np.float32)
        n = float(np.linalg.norm(emb))
        return emb / n if n > 1e-8 else None
    except Exception as e:
        log.warning("Failed to load %s: %s", speaker_id, e)
        return None


def save_embedding(speaker_id: str, new_emb: np.ndarray) -> None:
    """Average with any existing enrollment (running mean), then L2-normalize."""
    p = _path(speaker_id)
    count = 0
    acc = np.zeros_like(new_emb)
    if p.exists():
        try:
            obj = json.loads(p.read_text())
            prev = np.asarray(obj["embedding"], dtype=np.float32)
            count = int(obj.get("count", 1))
            acc = prev * count
        except Exception:
            count = 0
            acc = np.zeros_like(new_emb)
    # 【防稀釋】running-mean 的 count 無上限的話,樣本一多、新樣本權重趨近 0,聲紋永遠修不動;
    # 上限 40 → 新樣本至少保有 ~2.5% 權重,聲音隨時間變化(感冒/設備換)也能慢慢跟上。
    if count > 40:
        acc = acc * (40.0 / count)
        count = 40
    acc = acc + new_emb
    count += 1
    mean = acc / count
    n = float(np.linalg.norm(mean))
    if n > 1e-8:
        mean = mean / n
    p.write_text(json.dumps({"embedding": mean.tolist(), "count": count}))


def list_enrolled() -> List[str]:
    return sorted(p.stem for p in STORE_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
app = FastAPI(title="Hermes Voiceprint", version="1.0")


@app.get("/health")
def health():
    return {"ok": True, "enrolled": list_enrolled()}


@app.get("/voiceprint/health")
def voiceprint_health():
    # xiaozhi 的 VoiceprintProvider 健康檢查打這個路徑、且要看到 status=healthy 才會啟用聲紋。
    return {"status": "healthy", "enrolled": list_enrolled()}


@app.post("/voiceprint/enroll")
async def enroll(
    speaker_id: str = Form(...),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    raw = await file.read()
    emb = embed(raw)
    if emb is None:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "speaker_id": speaker_id, "error": "audio empty/too short/undecodable"},
        )
    save_embedding(speaker_id, emb)
    log.info("Enrolled speaker_id=%s", speaker_id)
    return {"ok": True, "speaker_id": speaker_id}


@app.post("/voiceprint/identify")
async def identify(
    speaker_ids: str = Form(""),
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
):
    ids = [s.strip() for s in speaker_ids.split(",") if s.strip()]
    raw = await file.read()
    emb = embed(raw)
    if emb is None or not ids:
        return {"speaker_id": "", "score": 0.0}

    best_id = ""
    best_score = -1.0
    for sid in ids:
        ref = load_embedding(sid)
        if ref is None:
            continue
        # cosine similarity of unit vectors; clamp to [0,1].
        # Same-speaker ~0.75-0.95, different-speaker/TV/noise ~0.0-0.55.
        cos = float(np.dot(emb, ref))
        score = max(0.0, min(1.0, cos))
        if score > best_score:
            best_score = score
            best_id = sid

    # 【強制註冊·對話式】剛確認身份(「我是Owen」)後開啟，把這個人的話直接 enroll 進去，
    # 直到 count 達標。★必須在下面 best_score<0 提前返回「之前」跑★——否則 owner.json 空的時候
    # best_score 永遠 <0 就 return 了，強制註冊永遠沒機會寫第一筆(雞生蛋 bug)。emb 非空=有聲音才學。
    for sid, target in list(_FORCE_ENROLL.items()):
        try:
            c2 = json.loads(_path(sid).read_text()).get("count", 0)
        except Exception:
            c2 = 0
        if c2 >= target:
            _FORCE_ENROLL.pop(sid, None)
            continue
        # 【防污染】聲紋已成形(≥8樣本)後,跟本人相似度過低(<0.30)的音訊八成是別人/電視,
        # 不能學進去(會把主人聲紋越拉越歪)。養成期(<8)不設限,否則第一筆永遠進不去。
        if c2 >= 8 and sid == best_id and best_score < 0.30:
            log.info("Force-enroll skip(疑似他人聲音) %s score=%.2f", sid, best_score)
            continue
        try:
            save_embedding(sid, emb)
            log.info("Force-enroll %s -> %d/%d", sid, c2 + 1, target)
        except Exception:
            pass

    if best_score < 0:
        return {"speaker_id": "", "score": 0.0}

    # 【自動學習】認出某人就把這句加進聲紋(running mean 強化)。樣本少→門檻低、學快;
    # 樣本多→門檻高、學嚴。兩者都遠高於陌生人(~0.0-0.55)，不會誤學。
    if best_id:
        try:
            cnt = json.loads(_path(best_id).read_text()).get("count", 0)
        except Exception:
            cnt = 0
        if best_score >= (0.70 if cnt < 8 else 0.80):
            try:
                save_embedding(best_id, emb)
            except Exception:
                pass

    return {"speaker_id": best_id, "score": round(best_score, 4)}


@app.post("/voiceprint/force_enroll")
async def force_enroll(speaker_id: str = Form(...), on: str = Form("true"),
                       target_count: int = Form(10), authorization: Optional[str] = Header(None)):
    """開/關某人的強制註冊模式(對話式註冊用)。on=true 後，之後 identify 收到的音訊
    都會 enroll 進 speaker_id，直到 count 達 target_count 自動停。"""
    if str(on).lower() in ("true", "1", "yes", "on"):
        _FORCE_ENROLL[speaker_id] = int(target_count)
    else:
        _FORCE_ENROLL.pop(speaker_id, None)
    return {"ok": True, "force_enroll": _FORCE_ENROLL}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
