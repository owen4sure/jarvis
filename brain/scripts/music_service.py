"""
Hermes 音樂服務 — 在 Mac 的 Chrome 上播 YouTube（看得到畫面）
==============================================================
xiaozhi-server(Docker)透過 host.docker.internal:8810 呼叫。

使用者選擇要「看得到 YouTube 畫面」，所以用 Chrome 開分頁播放（不是背景 mpv）。

【可靠性關鍵】每次控制 Chrome 前先 _kill_stray_chrome():
殘留的 headless Chrome 殭屍(--user-data-dir=/tmp/chr-*,截圖/DOM工具留下的)會搶走
AppleScript 目標 → osascript 控制到空的 Chrome(看到0視窗)→ 暫停/關閉全失效、兩首疊播。
先清掉殭屍,控制才永遠打得到真正在播的 Chrome。

注意:語音「暫停」用 JS 注入,需要在 Chrome 開「檢視→開發人員→允許 Apple 事件的 JavaScript」。
播放/切歌/關閉不需要這個設定(關分頁不需 JS)。
"""
import subprocess
import urllib.parse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

YTDLP = "/Users/USERNAME/Hermes_Brain/.venv/bin/yt-dlp"
app = FastAPI(title="Hermes Music Service (Chrome)")


class PlayReq(BaseModel):
    query: str


def _search_video(query: str) -> str:
    """用 yt-dlp 找 YouTube 第一支影片 id；失敗回空字串。"""
    try:
        out = subprocess.run(
            [YTDLP, f"ytsearch1:{query}", "--get-id", "--no-warnings",
             "--no-playlist", "--socket-timeout", "15"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if len(line) == 11 and all(c.isalnum() or c in "-_" for c in line):
                return line
    except Exception:
        pass
    return ""


def _kill_stray_chrome():
    """殺掉殘留的 headless Chrome(--user-data-dir=/tmp/chr-*)——它們會搶走 AppleScript 目標,
    害控制打到空的 Chrome(看到0視窗)。每次控制 Chrome 前先清,音樂控制才永遠可靠。"""
    try:
        subprocess.run(["/usr/bin/pkill", "-f", "user-data-dir=/tmp/chr"],
                       timeout=4, capture_output=True)
    except Exception:
        pass


def _close_youtube_tabs():
    """關掉 Chrome 裡所有含 youtube 的分頁。
    用「單一 whose 指令」一次關完(比逐一迴圈快很多、也比較不會卡住 AppleEvent)。"""
    _kill_stray_chrome()
    script = ('tell application "Google Chrome" to '
              'close (every tab of every window whose URL contains "youtube")')
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       timeout=8, capture_output=True)
    except Exception:
        pass


@app.post("/play")
def play(req: PlayReq):
    vid = _search_video(req.query)
    if vid:
        url = f"https://www.youtube.com/watch?v={vid}"
    else:
        url = ("https://www.youtube.com/results?search_query="
               + urllib.parse.quote(req.query))
    # 放新歌前先關掉前一首的 YouTube 分頁 → 不會兩首同時播。
    _close_youtube_tabs()
    # 開在 Chrome(看得到畫面)。Chrome 沒開會自動啟動。
    subprocess.run(["/usr/bin/open", "-a", "Google Chrome", url])
    return JSONResponse({"ok": True, "query": req.query, "url": url,
                         "matched": bool(vid)})


NOWPLAYING = "/opt/homebrew/bin/nowplaying-cli"


@app.post("/pause")
def pause():
    # 用 nowplaying-cli 切換系統「正在播放」的暫停/繼續。YouTube 用 Media Session API 註冊進 macOS
    # 的 Now Playing,所以這能直接暫停/繼續 Chrome 裡的 YouTube,【完全不需要】開 Chrome 的
    # 「允許 Apple 事件的 JavaScript」設定（比舊的 osascript JS 注入可靠又零設定）。
    try:
        subprocess.run([NOWPLAYING, "togglePlayPause"],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    return JSONResponse({"ok": True, "action": "toggle_pause"})


@app.post("/stop")
def stop():
    # 先用 nowplaying 立刻靜音（萬一關分頁的 osascript 卡住，聲音也馬上停，不會繼續吵），再關分頁。
    try:
        subprocess.run([NOWPLAYING, "pause"], timeout=4, capture_output=True)
    except Exception:
        pass
    _close_youtube_tabs()  # 關掉所有 YouTube 分頁
    return JSONResponse({"ok": True, "action": "stop"})


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8810, log_level="warning")
