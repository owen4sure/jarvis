"""
本機 ASR 服務：用 Apple MLX(Metal 加速) 跑 Whisper large-v3-turbo，
暴露成 OpenAI 相容的 /v1/audio/transcriptions 端點，給 xiaozhi 容器的 openai ASR provider 呼叫。

為什麼在本機不在容器：Docker on Mac 只有 CPU，跑大模型慢；本機能用 Apple 晶片 Metal → 又快又準。
繁體：Whisper 中文常吐簡體 → 用 OpenCC s2twp(簡→繁台灣用語) 轉成繁體。
"""
import os
import tempfile

from fastapi import FastAPI, File, Form, UploadFile
import mlx_whisper

MODEL = os.environ.get("MLX_ASR_MODEL", "mlx-community/whisper-large-v3-turbo")
# 繁體轉換（沒裝 opencc 就原樣回，不擋）
try:
    from opencc import OpenCC
    _cc = OpenCC("s2twp")  # 簡體 → 繁體(台灣慣用語)
except Exception:
    _cc = None

import json as _json

_FIN_JSON = os.path.expanduser("~/Hermes_Brain/config/finance.json")
_HINT = {"mtime": 0.0, "prompt": "台灣繁體中文。"}


def _domain_hint():
    """動態把 Owen【真實持股名稱】+ 常用詞餵給 Whisper 的 initial_prompt，
    讓專有名詞(股票名/Jarvis/財務詞)在辨識當下就往對的詞收斂 → 從源頭防錯字，零延遲。
    只放使用者自己的詞、控制長度(避免長 generic prompt 在雜訊上帶歪，這是之前的教訓)。"""
    try:
        mt = os.path.getmtime(_FIN_JSON)
        if mt == _HINT["mtime"]:
            return _HINT["prompt"]
        d = _json.load(open(_FIN_JSON, encoding="utf-8"))
        names = [str(h.get("name", "")).strip() for h in d.get("holdings", []) if h.get("name")]
        terms = [n for n in names if n][:8] + ["Jarvis", "報酬率", "淨資產", "台股", "美股"]
        _HINT.update(mtime=mt, prompt="台灣繁體中文。可能提到：" + "、".join(terms) + "。")
    except Exception:
        pass
    return _HINT["prompt"]


def _wav_rms(path):
    """讀 WAV 算音量(RMS)。靜音 ~<100、語音 ~300+。讀不了回 None(不擋)。"""
    try:
        import wave
        import audioop
        with wave.open(path, "rb") as w:
            frames = w.readframes(w.getnframes())
            if not frames:
                return 0
            return audioop.rms(frames, w.getsampwidth())
    except Exception:
        return None


# Whisper 在非語音/雜訊上常吐這些訓練資料垃圾(YouTube字幕/作曲credit/訂閱語)——絕非使用者真的會講
_HALLUC = (
    "初音", "ミク", "作詞", "作曲", "編曲", "李宗盛", "點贊", "訂閱", "点赞", "订阅",
    "字幕", "謝謝觀看", "谢谢观看", "請不吝", "请不吝", "MBC", "Thank you for watching",
    "下次再見", "本字幕", "中文字幕",
)


def _dehallucinate(text):
    """擋掉 Whisper 幻覺：①高度重複(壓縮率超低) ②已知幻覺字串。判定是幻覺就回空(寧可沒聽到也不亂吐)。"""
    if not text:
        return ""
    try:
        import zlib
        b = text.encode("utf-8")
        if len(b) >= 12:
            ratio = len(zlib.compress(b, 6)) / len(b)
            if ratio < 0.32:   # 壓得太小 = 高度重複(「言言言」「promoting promoting」)
                return ""
    except Exception:
        pass
    for h in _HALLUC:
        if h in text:
            return ""
    return text


app = FastAPI(title="Hermes MLX ASR")


@app.on_event("startup")
def _warmup():
    """開機就把 Whisper 模型載進 Metal(預熱)。
    mlx_whisper 是 lazy 載入,首次轉錄要 ~9s(載模型);先用一段靜音跑一次,
    使用者第一句語音就是熱機的 ~1s,不會吃到冷啟動。失敗不影響功能。"""
    try:
        import numpy as _np
        mlx_whisper.transcribe(
            _np.zeros(16000, dtype=_np.float32),
            path_or_hf_repo=MODEL, language="zh",
            condition_on_previous_text=False,
        )
        print("[mlx-asr] 模型已預熱,首次轉錄就會快(~1s)")
    except Exception as e:
        print("[mlx-asr] 預熱失敗(不影響功能):", e)


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "opencc": _cc is not None}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: str = Form("whisper-1")):
    data = await file.read()
    path = None
    try:
        suffix = os.path.splitext(file.filename or "a.wav")[1] or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            path = f.name
        # 存一份真實音訊供除錯(留最近幾個),這樣能用真實麥克風音訊驗證防幻覺有沒有修好
        try:
            dbg = os.path.join(os.path.dirname(__file__), "..", "memory", "asr_debug")
            os.makedirs(dbg, exist_ok=True)
            import shutil
            shutil.copy(path, os.path.join(dbg, f"last_{int(__import__('time').time())%100000}.wav"))
            for old in sorted(os.listdir(dbg))[:-6]:
                os.remove(os.path.join(dbg, old))
        except Exception:
            pass
        # 【防 Whisper 重複幻覺】真實麥克風音訊常有雜訊/靜音 → Whisper 會卡住狂吐同一字
        # (「言言言言」「promoting promoting」)。下列參數讓它偵測靜音/低信心/重複就放棄,不亂吐：
        #   no_speech_threshold 靜音偵測、compression_ratio_threshold 重複偵測、temperature 失敗就升溫重試。
        # 不再用長 initial_prompt(會在不清楚音訊上把模型帶歪)；繁體靠 OpenCC s2twp 轉。
        # 防線①：靜音能量門檻。RMS 太低(幾乎沒聲音)就根本不轉錄 → Whisper 不會對著靜音幻覺。
        rms = _wav_rms(path)
        if rms is not None and rms < 160:
            return {"text": ""}
        r = mlx_whisper.transcribe(
            path, path_or_hf_repo=MODEL, language="zh",
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.2,
            logprob_threshold=-1.0,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            initial_prompt=_domain_hint(),   # 動態含 Owen 真實持股名 → 專有名詞辨識更準
        )
        text = (r.get("text") or "").strip()
        if _cc and text:
            text = _cc.convert(text)
        text = _dehallucinate(text)
        return {"text": text}
    except Exception as e:
        return {"text": "", "error": str(e)[:200]}
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except Exception:
                pass
