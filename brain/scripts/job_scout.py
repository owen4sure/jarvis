"""
Jarvis 工作雷達(job scout)
==========================
每天自動爬職缺 → 按 Owen 的履歷輪廓評分 → 只推「新出現的高分職缺」到 Telegram。

履歷輪廓(來源 ~/Desktop/resume/Owen_履歷_2026.html):
  商管背景的 AI Implementation / AI Product Builder——RAG/Agent/Prompt/n8n/Vibe Coding,
  目標 AI 產品/導入類職缺(非純工程師缺),地區偏好由使用者的求職條件決定。

資料源(都是免登入、實測可用):
  · Yourator API(新創/AI 職缺)
  · LinkedIn guest 搜尋(台灣區,免登入 HTML)
  · 104 反爬太硬(CF+SPA),刻意不做——別浪費力氣。

用法:
  python3 job_scout.py            # 乾跑:印結果不推播
  python3 job_scout.py --push     # 抓+評分+推 Telegram(給 launchd 每日排程用)
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN_PATH = os.path.join(BASE, "config", "job_seen.json")
MATCHES_PATH = os.path.join(BASE, "config", "job_matches.json")
DISLIKES_PATH = os.path.join(BASE, "config", "job_dislikes.json")  # dashboard ✕ 掉的
PROFILE_PATH = os.path.join(BASE, "config", "job_profile.json")    # dashboard 自訂求職條件

# LLM 精評用的履歷輪廓(來源 ~/Desktop/resume/Owen_履歷_2026.html,改履歷記得同步)
_DEFAULT_PROFILE = (
    "(範例履歷輪廓——在 dashboard 上傳你自己的履歷後會自動換成你的。)"
    "描述求職者背景、專長、目標職位;硬性條件(地區/薪資/排除的職種)由"
    "上傳 PDF 履歷 + 填『我要的工作條件』後,交給 LLM 生成這段。")


def _resume_profile():
    """履歷輪廓:優先用 dashboard 上傳履歷後 LLM 生成的(job_profile.json 的
    resume_profile),沒有才用內建預設。這樣換人用只要上傳履歷就換一套輪廓。"""
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            rp = (json.load(f).get("resume_profile") or "").strip()
            if rp:
                return rp
    except Exception:
        pass
    return _DEFAULT_PROFILE


# 相容舊呼叫:模組層仍暴露 RESUME_PROFILE(取當下值)
RESUME_PROFILE = _DEFAULT_PROFILE
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

# ---------- 履歷輪廓 → 評分規則 ----------
AI_KW = ("ai", "llm", "gpt", "agent", "rag", "prompt", "生成式", "genai",
         "chatbot", "機器學習應用", "ai應用", "ai 應用", "自動化", "n8n", "導入")
PRODUCT_KW = ("產品經理", "product manager", "pm", "product owner", "產品企劃",
              "implementation", "solution", "導入顧問", "產品營運", "product ops",
              "customer success", "解決方案")
BONUS_KW = ("rag", "agent", "prompt", "n8n", "no-code", "low-code", "金融",
            "fintech", "知識庫", "workflow")
# 純工程/太資深/不相關 → 扣分(Owen 非工程師出身,定位是 AI Product/Implementation)
PENALTY_KW = ("實習", "intern", "工讀", "工程師", "engineer", "principal", "director",
              "嵌入式", "c++", "firmware", "scientist", "資料科學",
              "半導體製程", "電機", "sales ", "業務專員", "門市")


def _score(title: str, company: str, loc: str, tags) -> int:
    t = f"{title} {' '.join(tags or [])}".lower()
    s = 0
    has_ai = any(k in t for k in AI_KW)
    has_prod = any(k in t for k in PRODUCT_KW)
    if has_ai and has_prod:
        s += 10          # AI×產品 交集 = 本命缺
    elif has_ai:
        s += 5
    elif has_prod:
        s += 4
    else:
        return 0         # 兩者皆無 → 不推
    s += sum(2 for k in BONUS_KW if k in t)
    s -= sum(6 for k in PENALTY_KW if k in t)
    # 地點硬條件:只要雙北或遠端(Owen 明確指定)。寫「Taiwan」沒講城市的保留(常是遠端友善)。
    l = (loc or "").lower()
    if "遠端" in l or "remote" in l:
        s += 2
    elif any(c in l for c in ("台北", "臺北", "taipei", "新北", "new taipei")):
        s += 1
    elif any(c in l for c in ("台中", "臺中", "taichung", "新竹", "hsinchu", "高雄",
                              "kaohsiung", "台南", "臺南", "tainan", "桃園", "taoyuan",
                              "彰化", "苗栗", "嘉義", "屏東", "宜蘭", "花蓮", "台東", "基隆")):
        return 0   # 非雙北非遠端 → 直接不推
    return s


# ---------- 資料源 ----------
def _search_terms(default):
    """搜尋關鍵字:優先用上傳履歷後 LLM 生成的 resume_terms,沒有才用預設。"""
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            t = json.load(f).get("resume_terms") or []
            if t:
                return t[:5]
    except Exception:
        pass
    return default


def fetch_104():
    """104 有 Cloudflare + SPA,API 爬不動 → 用 headless playwright 渲染搜尋頁後抽卡。
    慢(整批約 10-20s)但每日排程是背景跑無妨。抓不到就回 [](絕不擋整個 scan)。
    順便抓「外商公司」標籤 → is_foreign,幫投遞選中/英文履歷。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []
    out, seen = [], set()
    _JS = r"""() => {
      const cards = document.querySelectorAll(".info-container, [class*='vue-recycle']");
      const res = [];
      cards.forEach(card => {
        const a = card.querySelector("a[href*='/job/']");
        if (!a) return;
        const title = (a.innerText || "").trim();
        if (!title || title.length > 60) return;
        const comp = card.querySelector("a[href*='/company/']:not([href*='/company/search'])");
        const loc = card.querySelector("a[href*='area=']");
        let sal = "";
        card.querySelectorAll("a[href*='sr='], a[href*='joblist_tag']").forEach(x => {
          const t = (x.innerText || "").trim();
          if (/月薪|年薪|待遇|面議|時薪/.test(t) && !sal) sal = t;
        });
        const foreign = /外商公司/.test(card.innerText || "");
        res.push({title,
                  company: (comp ? comp.innerText.trim() : ""),
                  loc: (loc ? loc.innerText.trim() : ""),
                  salary: sal, url: a.href.split("?")[0], is_foreign: foreign});
      });
      return res;
    }"""
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True,
                                   args=["--disable-blink-features=AutomationControlled"])
            try:
                pg = b.new_page(user_agent=UA)
                for term in _search_terms(["AI 產品", "AI 導入"]):
                    try:
                        q = urllib.parse.quote(term)
                        pg.goto(f"https://www.104.com.tw/jobs/search/?keyword={q}&order=16",
                                timeout=40000)
                        # 等職缺卡渲染(CF 過完就出現),最多 8s——比死睡 6s 快、且不會卡滿
                        try:
                            pg.wait_for_selector("a[href*='/job/']", timeout=8000)
                        except Exception:
                            pass  # 沒等到就照抓當下 DOM(可能被 CF 擋,evaluate 回空)
                        for j in pg.evaluate(_JS):
                            u = j.get("url", "")
                            if u and u not in seen and j.get("title"):
                                seen.add(u)
                                out.append({"source": "104", "title": j["title"],
                                            "company": j.get("company", ""),
                                            "loc": j.get("loc", ""), "salary": j.get("salary", ""),
                                            "tags": [], "url": u,
                                            "is_foreign": bool(j.get("is_foreign"))})
                    except Exception as e:
                        print(f"[104:{term}] {e}", file=sys.stderr)
            finally:
                b.close()   # 例外時也一定關瀏覽器,不留殭屍 Chromium
    except Exception as e:
        print(f"[104] {e}", file=sys.stderr)
    return out


def fetch_yourator():
    out = []
    for term in _search_terms(["AI", "產品經理"]):
        try:
            q = urllib.parse.quote(term)
            for page in (1, 2):
                url = f"https://www.yourator.co/api/v4/jobs?term%5B%5D={q}&page={page}"
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                d = json.loads(urllib.request.urlopen(req, timeout=15).read())
                for j in d.get("payload", {}).get("jobs", []):
                    out.append({
                        "source": "Yourator",
                        "title": (j.get("name") or "").strip(),
                        "company": (j.get("company", {}) or {}).get("brand")
                                   or j.get("companyName") or "",
                        "loc": j.get("location") or "",
                        "salary": j.get("salary") or "",
                        "tags": j.get("tags") or [],
                        "url": "https://www.yourator.co" + (j.get("path") or ""),
                    })
                time.sleep(1)
        except Exception as e:
            print(f"[yourator:{term}] {e}", file=sys.stderr)
    return out


def fetch_linkedin():
    out = []
    for kw in _search_terms(["AI Product Manager", "AI 產品經理", "AI Implementation"]):
        try:
            q = urllib.parse.quote(kw)
            url = ("https://www.linkedin.com/jobs-guest/jobs/api/"
                   f"seeMoreJobPostings/search?keywords={q}&location=Taiwan&start=0")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            titles = re.findall(r'base-search-card__title["\s>]+([^<]+)', raw)
            comps = re.findall(r'base-search-card__subtitle[^>]*>\s*<a[^>]*>\s*([^<]+)', raw)
            locs = re.findall(r'job-search-card__location[^>]*>\s*([^<]+)', raw)
            urls = re.findall(r'base-card__full-link"[^>]*href="([^"]+)"', raw) \
                or re.findall(r'href="(https://[a-z]+\.linkedin\.com/jobs/view/[^"]+)"', raw)
            # 每張卡的 jobPosting id(順序跟 title 對齊)→ 之後可抓完整 JD 來翻譯
            jids = re.findall(r'jobPosting:(\d+)', raw)
            for i, title in enumerate(titles):
                u = (urls[i].split("?")[0] if i < len(urls) else
                     "https://www.linkedin.com/jobs/search/?keywords=" + q)
                out.append({
                    "source": "LinkedIn",
                    "title": title.strip().replace("&amp;", "&"),
                    "company": (comps[i].strip() if i < len(comps) else ""),
                    "loc": (locs[i].strip() if i < len(locs) else ""),
                    "salary": "", "tags": [], "url": u,
                    "jd_id": (jids[i] if i < len(jids) else ""),
                })
            time.sleep(1)
        except Exception as e:
            print(f"[linkedin:{kw}] {e}", file=sys.stderr)
    return out


# ---------- 去重 / 儲存 ----------
def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _key(j):
    return f"{j['company']}|{j['title']}".lower()


def llm_rerank(cands, disliked_titles):
    """用 LLM(走 8808 免費輪換)按履歷輪廓精評每個職缺 0-100 + 一句理由。
    英文職缺順便給中文翻譯;dashboard 自訂條件是最高優先標準。
    失敗回 None → 保持關鍵字排序照樣能用(graceful degradation)。"""
    if not cands:
        return None
    items = "\n".join(
        f"{i}. {j['title']}｜{j['company']}｜{j['loc']}｜{j.get('salary','')}"
        f"｜tags:{','.join(j.get('tags') or [])}" for i, j in enumerate(cands))
    taste = ("他之前看過但按了「不喜歡」的職缺(類似的請給低分):\n- "
             + "\n- ".join(disliked_titles[-15:])) if disliked_titles else ""
    prof = _load(PROFILE_PATH, {})
    custom = (prof.get("analyzed") or prof.get("custom") or "").strip()
    custom_block = (f"【Owen 自己輸入的求職條件——這是最高優先標準,與履歷輪廓衝突時以這個為準】:\n"
                    f"{custom}\n\n") if custom else ""
    prompt = (
        f"你是求職者的職涯配對引擎。他的履歷輪廓:\n{_resume_profile()}\n\n"
        f"{custom_block}{taste}\n\n"
        f"以下職缺逐一評「適配分」0-100(80+=非常適合該主動投遞;60-79=值得看;"
        f"<60=不適合),並給一句 20 字內的中文理由(講為什麼適合/不適合他,要具體)。"
        f"職缺名稱若是英文,額外給 title_zh 中文翻譯(中文職缺就省略這欄)。\n"
        f"{items}\n\n"
        f"只回 JSON array,格式 [{{\"i\":0,\"fit\":85,\"reason\":\"...\",\"title_zh\":\"...\"}}],不要其他文字。")
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8808/v1beta/openai/chat/completions",
            data=json.dumps({"model": "gemini-3.1-flash-lite",
                             "messages": [{"role": "user", "content": prompt}],
                             "temperature": 0.2}).encode(),
            headers={"Content-Type": "application/json"})
        raw = json.loads(urllib.request.urlopen(req, timeout=60).read())
        text = raw["choices"][0]["message"]["content"]
        m = re.search(r"\[[\s\S]*\]", text)
        arr = json.loads(m.group(0)) if m else []
        out = {}
        for it in arr:
            i = int(it.get("i", -1))
            if 0 <= i < len(cands):
                out[i] = {"fit": max(0, min(100, int(it.get("fit", 0)))),
                          "reason": str(it.get("reason", ""))[:60],
                          "title_zh": str(it.get("title_zh", "") or "")[:60]}
        return out if out else None
    except Exception as e:
        print(f"[llm_rerank] {e} → 退回關鍵字排序", file=sys.stderr)
        return None


def run(push: bool):
    jobs = fetch_yourator() + fetch_linkedin() + fetch_104()
    # 同缺去重(兩源都有時保留先出現的)
    uniq = {}
    for j in jobs:
        uniq.setdefault(_key(j), j)
    dislikes = _load(DISLIKES_PATH, {})   # {key: {"title":..,"ts":..}} dashboard ✕ 掉的
    scored = []
    for j in uniq.values():
        if _key(j) in dislikes:
            continue                       # 按過不喜歡的永不再出現
        s = _score(j["title"], j["company"], j["loc"], j["tags"])
        if s >= 5:
            scored.append({**j, "score": s, "key": _key(j)})
    scored.sort(key=lambda x: -x["score"])

    # LLM 按履歷精評前 20 名(關鍵字只是粗篩;適配分+理由才是真排序)
    pool = scored[:20]
    disliked_titles = [v.get("title", k) for k, v in dislikes.items()]
    fits = llm_rerank(pool, disliked_titles)
    if fits:
        for i, j in enumerate(pool):
            j["fit"] = fits.get(i, {}).get("fit", 50)
            j["reason"] = fits.get(i, {}).get("reason", "")
            tz = fits.get(i, {}).get("title_zh", "")
            if tz and tz != j["title"]:
                j["title_zh"] = tz
        scored = [j for j in pool if j.get("fit", 0) >= 55] + scored[20:]
        scored.sort(key=lambda x: (-(x.get("fit", 0)), -x["score"]))

    seen = _load(SEEN_PATH, {})
    fresh = [j for j in scored if _key(j) not in seen]
    top = fresh[:8]

    # matches 存最新一輪完整榜(給 dashboard/語音查);seen 只在【真的推播】後才記,
    # 乾跑不記——否則測試一次就把職缺全標成看過,正式推播變空的。
    now = time.strftime("%Y-%m-%d %H:%M")
    _save(MATCHES_PATH, {"updated": now, "top": scored[:20], "new_today": top})

    print(f"抓到 {len(jobs)} 筆 → 去重 {len(uniq)} → 達標 {len(scored)} → 新的 {len(fresh)}"
          f"{'(含LLM精評)' if fits else '(LLM不可用,關鍵字排序)'}")
    for j in top:
        sal = f"｜{j['salary']}" if j.get("salary") else ""
        fit = f"fit{j['fit']}" if j.get("fit") is not None else f"kw{j['score']}"
        print(f"  [{fit:>5}] {j['title']}｜{j['company']}｜{j['loc']}{sal}｜{j.get('reason','')}")

    if not top:
        print("今天沒有新的高分職缺,不推播。")
        return
    if not push:
        print("---- DRY RUN(不推播) ----")
        return

    lines = ["🎯 Jarvis 工作雷達｜今日新出現的適配職缺\n"]
    for i, j in enumerate(top, 1):
        sal = f"\n   💰 {j['salary']}" if j.get("salary") else ""
        why = f"\n   💡 {j['reason']}" if j.get("reason") else ""
        fit = f"（適配 {j['fit']}%）" if j.get("fit") is not None else ""
        zh = f"\n   🈶 {j['title_zh']}" if j.get("title_zh") else ""
        lines.append(f"{i}. {j['title']}{fit}{zh}\n   🏢 {j['company']}｜📍 {j['loc']}{sal}{why}\n   {j['url']}")
    lines.append("\n(LLM 按你的履歷逐缺評分,只推雙北/遠端。不喜歡的去 dashboard 💼 按✕,以後類似的不再推。)")
    text = "\n".join(lines)

    sys.path.insert(0, BASE)
    from modules.remote.telegram_handler import TelegramHandler
    cfg = _load(os.path.join(BASE, "config", "telegram.json"), {})
    handler = TelegramHandler()
    delivered = False
    for uid in cfg.get("allowed_user_ids", []):
        try:
            handler.send_message(uid, text)
            delivered = True
        except Exception as e:
            print(f"⚠️ 推播給 {uid} 失敗: {e}", file=sys.stderr)
    if delivered:  # 推成功才標 seen,失敗明天重推
        for j in top:
            seen[_key(j)] = now
        if len(seen) > 800:   # seen 檔別無限長大,砍最舊的
            for k in list(seen)[:-800]:
                seen.pop(k, None)
        _save(SEEN_PATH, seen)
    print("已推播 Telegram。" if delivered else "推播全部失敗,seen 不記、明天重試。")


if __name__ == "__main__":
    run(push="--push" in sys.argv)
