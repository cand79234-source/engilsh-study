"""欧陆式点词查义 —— 真实浏览器端到端验收（Playwright，本地 SQLite，零密钥）。

验证：
  A. hlColloc 渲染：例句里每个英文词都被包成可点 span(data-act=wlookup)，搭配词组保持加粗
  B. 点词弹层：调用 wordLookup 后弹出卡片，显示 音标/词性/释义 + 🔊，且词典收录词能查到
  C. 未收录词：弹层显示「本地词典未收录该词」

前置：本脚本自起本地 uvicorn（EOS_DB 指向临时 SQLite，不设 EOS_TOKEN → 完全开放）。
运行：python3.11 tests/test_wordlookup_e2e.py
"""
import os
import subprocess
import sys
import time
import tempfile

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DB = tempfile.mktemp(suffix=".db")
PORT = 8123
BASE = f"http://localhost:{PORT}"

# ---- 1) 预置一个唯一测试词进本地词典（避免与后台播种冲突）----
os.environ["EOS_DB"] = DB
sys.path.insert(0, os.path.join(ROOT, "backend"))
import db
db.init_db()
_c = db.get_conn()
_c.execute(
    "INSERT OR IGNORE INTO dictionary(word, phonetic, pos, meaning) VALUES(?,?,?,?)",
    ("eoslookupword", "/ˈtɛst/", "名词", "测试词释义；仅用于端到端"),
)
_c.commit()
_c.close()

passed = failed = 0
def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} {extra}")

def main():
    # ---- 2) 起本地服务 ----
    env = dict(os.environ, EOS_DB=DB)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT)],
        cwd=os.path.join(ROOT, "backend"), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import urllib.request
        for _ in range(40):
            try:
                urllib.request.urlopen(f"{BASE}/api/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("服务启动失败"); sys.exit(1)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto(BASE + "/")
            pg.wait_for_timeout(800)

            # ---- A. 渲染：例句单词包成可点 span，搭配加粗 ----
            html = pg.evaluate("() => hlColloc('I eat an apple and a banana.', ['eat an apple'])")
            check("例句单词包成可点 span(data-act=wlookup)", 'data-act="wlookup"' in html and 'class="tk"' in html, html[:120])
            check("搭配词组保持加粗(<b class=hl>)", '<b class="hl">' in html, html[:120])

            # ---- B. 点词弹层：收录词查到释义+音标 ----
            pg.evaluate("() => document.querySelectorAll('.overlay').forEach(o=>o.remove())")
            pg.evaluate("async () => { await wordLookup('eoslookupword'); }")
            pg.wait_for_selector("#wlookup_body", timeout=8000)
            pg.wait_for_timeout(300)
            body = pg.evaluate("() => { const ov=[...document.querySelectorAll('.overlay')].pop(); return ov?ov.querySelector('#wlookup_body').innerText:''; }")
            check("弹层显示释义(测试词释义)", "测试词释义" in body, body[:80])
            check("弹层显示音标(/ˈtɛst/)", "/ˈtɛst/" in body, body[:80])
            check("弹层带 🔊 朗读按钮", pg.evaluate("() => !!document.querySelector('.sheet [data-act=speak]')"))
            # 关闭
            pg.evaluate("() => document.querySelectorAll('.overlay').forEach(o=>o.remove())")

            # ---- C. 未收录词 ----
            pg.evaluate("async () => { await wordLookup('zzzqqqnotaword'); }")
            pg.wait_for_selector("#wlookup_body", timeout=8000)
            pg.wait_for_timeout(300)
            body2 = pg.evaluate("() => { const ov=[...document.querySelectorAll('.overlay')].pop(); return ov?ov.querySelector('#wlookup_body').innerText:''; }")
            check("未收录词弹层提示", "未收录" in body2, body2[:80])
            b.close()
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()

    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
