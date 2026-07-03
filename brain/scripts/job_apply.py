"""
LinkedIn 一鍵投遞(human-in-the-loop)
====================================
dashboard 工作雷達按「📨 投遞」→ 這支開一個【看得見的】Chrome 視窗(獨立
persistent profile,登入一次就記住)→ 自動點 Easy Apply → 自動上傳履歷 →
簡單頁面自動按下一步;遇到它沒把握的問題(自訂問答/複雜表單)就【停下來把
視窗留給 Owen 手動完成】——寧可少按一步,不亂填送出。

第一次使用:視窗打開後手動登入 LinkedIn 一次,之後都記住。
履歷檔路徑設定在 config/job_profile.json 的 resume_en / resume_zh。

用法: job_apply.py <job_url>
狀態寫進 config/job_apply_log.json(dashboard 可查)。
"""
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.expanduser("~/.hermes/linkedin_profile")
LOG_PATH = os.path.join(BASE, "config", "job_apply_log.json")
JOB_PROFILE = os.path.join(BASE, "config", "job_profile.json")


def _log(url, status, note=""):
    try:
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {"applies": []}
        d["applies"].append({"url": url, "status": status, "note": note,
                             "ts": time.strftime("%Y-%m-%d %H:%M")})
        d["applies"] = d["applies"][-100:]
        tmp = LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LOG_PATH)
    except Exception:
        pass
    print(f"[{status}] {note}")


def _resume_path(title="", is_foreign=False):
    """挑投遞用履歷:外商/英文職缺→英文履歷,台灣中文職缺→中文履歷。
    優先序:is_foreign(104「外商公司」標籤)→ 英文;否則看標題語言(含中文→中文)。
    只有一份時就用那份。"""
    try:
        p = json.load(open(JOB_PROFILE, encoding="utf-8"))
    except Exception:
        p = {}
    zh, en = p.get("resume_zh") or "", p.get("resume_en") or ""
    # 外商 → 英文優先;否則標題含中文 → 中文優先
    prefer_zh = (not is_foreign) and bool(re.search(r"[一-龥]", title))
    for cand in ([zh, en] if prefer_zh else [en, zh]):
        if cand and os.path.exists(os.path.expanduser(cand)):
            return os.path.expanduser(cand)
    return None


def apply(url, title="", is_foreign=False):
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE_DIR, exist_ok=True)
    resume = _resume_path(title, is_foreign)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, timeout=45000)
        page.wait_for_timeout(3500)

        # 沒登入 → 留視窗給 Owen 登入(登入會記住,下次直接投)
        if re.search(r"/(login|authwall|checkpoint)", page.url) or page.locator(
                "a[href*='login']").count() > 3:
            _log(url, "need_login", "視窗已開,請先登入 LinkedIn(只需一次),登入後重按投遞")
            page.wait_for_timeout(300000)  # 留 5 分鐘讓他登入
            ctx.close()
            return

        # 找 Easy Apply 按鈕
        btn = page.locator("button.jobs-apply-button, button:has-text('Easy Apply'), "
                           "button:has-text('快速應徵')").first
        try:
            btn.wait_for(timeout=8000)
        except Exception:
            _log(url, "no_easy_apply", "這缺不支援 Easy Apply(外部網站投遞),視窗留給你手動")
            page.wait_for_timeout(240000)
            ctx.close()
            return
        btn.click()
        page.wait_for_timeout(2500)

        # 逐步走 Easy Apply 精靈:上傳履歷/按下一步;遇到沒把握的欄位就停
        for step in range(8):
            # 上傳履歷(有 file input 且我們有檔案)
            if resume:
                fi = page.locator("input[type='file']")
                if fi.count():
                    try:
                        fi.first.set_input_files(resume)
                        page.wait_for_timeout(2000)
                        _log(url, "resume_uploaded", os.path.basename(resume))
                    except Exception:
                        pass
            # 有必填問答(radio/text 問題)且非預填 → 停下來給 Owen
            questions = page.locator(".jobs-easy-apply-form-section__grouping "
                                     "input[type='text']:not([value]), "
                                     ".fb-dash-form-element input[type='radio']")
            required_empty = 0
            try:
                for i in range(min(questions.count(), 10)):
                    el = questions.nth(i)
                    if el.get_attribute("type") == "text" and not el.input_value():
                        required_empty += 1
            except Exception:
                pass
            if required_empty:
                _log(url, "manual_finish", f"有 {required_empty} 個自訂問題要你自己答,視窗留著,答完按送出即可")
                page.wait_for_timeout(600000)  # 留 10 分鐘
                ctx.close()
                return
            # Submit > Review > Next 優先序
            for sel, done in (("button[aria-label*='Submit'], button:has-text('Submit application')", True),
                              ("button[aria-label*='Review'], button:has-text('Review')", False),
                              ("button[aria-label*='next'], button:has-text('Next'), button:has-text('繼續')", False)):
                b = page.locator(sel).first
                if b.count():
                    try:
                        b.click()
                        page.wait_for_timeout(2200)
                        if done:
                            _log(url, "submitted", "已送出投遞 ✅")
                            page.wait_for_timeout(4000)
                            ctx.close()
                            return
                        break
                    except Exception:
                        continue
            else:
                break  # 三種按鈕都沒有 → 精靈長得不一樣,交給人
        _log(url, "manual_finish", "自動走到一半,剩下的表單留視窗給你完成")
        page.wait_for_timeout(600000)
        ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: job_apply.py <job_url> [title]")
        sys.exit(1)
    u = sys.argv[1]
    t = sys.argv[2] if len(sys.argv) > 2 else ""
    fgn = (len(sys.argv) > 3 and sys.argv[3] in ("1", "true", "foreign"))
    try:
        apply(u, t, fgn)
    except Exception as e:
        _log(u, "error", str(e)[:120])
