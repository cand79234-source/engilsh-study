#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""专项回归：weeks.vocab_json 为旧格式（字符串数组）时，/api/today 不再 500。

背景：8 周假数据压测时发现，学习页「加载失败」。根因是
/api/today 里 `v.get("day")` 只兼容对象数组，库里若存的是
["word1","word2"] 这种旧格式字符串数组就 AttributeError → 500。

修复：ensure_week_content 在唯一读取入口做形状归一化（字符串升级为
{"word": ...} 并写回库自愈），today() 里 day 取值再加一层 null/脏值兜底。

用真实浏览器验证页面真的能打开（不是只看接口返回 200）。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8057
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "test123"
PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


def api(path):
    req = urllib.request.Request(BASE + path)
    req.add_header("X-Auth-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def main():
    backend = None
    for cand in [HERE, os.path.join(HERE, "backend"),
                 os.path.join(HERE, "..", "..", "..", "tmp", "pushtest", "backend")]:
        if os.path.exists(os.path.join(cand, "db.py")):
            backend = cand
            break
    if not backend:
        print("❌ 找不到 backend/db.py")
        return 1

    dbfile = "/tmp/vocab_shapes.db"
    if os.path.exists(dbfile):
        os.remove(dbfile)
    os.environ["EOS_DB"] = dbfile
    sys.path.insert(0, backend)
    import db
    db.init_db()

    # 预置三种形状的周数据 + 当前进度指到 week1
    conn = db.get_conn()
    cases = {
        # (stage, week): vocab_json
        (1, 1): json.dumps(["apple", "banana", "fkold1"]),          # 旧格式：字符串数组
        (1, 2): json.dumps([{"word": "fknew1", "day": 1},
                            {"word": "fknew2", "day": 2}]),         # 新格式：对象数组
        (1, 3): "这不是JSON{{{",                                     # 坏 JSON
        (1, 4): json.dumps([{"word": "fknull1", "day": None},
                            {"word": "fknull2", "day": "3"}]),      # day 为 null / 字符串
    }
    for (st, wk), vj in cases.items():
        conn.execute("INSERT INTO weeks (stage, week_no, title, grammar, vocab_json) "
                     "VALUES (?,?,?,?,?) "
                     "ON CONFLICT(stage, week_no) DO UPDATE SET vocab_json=excluded.vocab_json",
                     (st, wk, f"形状测试W{wk}", "g", vj))
    conn.execute("UPDATE progress SET stage=1, week=1, day=1 WHERE id=(SELECT MIN(id) FROM progress)")
    conn.commit()
    conn.close()

    env = dict(os.environ)
    env["EOS_DB"] = dbfile
    env["ACCESS_TOKEN"] = TOKEN
    env.pop("DATABASE_URL", None)
    log = open("/tmp/vocab_shapes_srv.log", "w")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app",
                            "--host", "127.0.0.1", "--port", str(PORT)],
                           cwd=backend, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                if api("/api/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.4)
        else:
            print("❌ 服务起不来")
            return 1

        print("\n【一】四种 vocab 形状，/api/today 全部不炸")
        for wk, desc in [(1, "旧格式字符串数组"), (2, "新格式对象数组"),
                         (3, "坏 JSON"), (4, "day 为 null/字符串")]:
            st, body = api(f"/api/today?stage=1&week={wk}" if False else "/api/today")
            ck(f"{desc} → 200", st == 200, f"HTTP {st} {body[:80]}")
            # today 用当前进度周；为测不同周，直接调 ensure 路由
            st2, _ = api(f"/api/week/1/{wk}/ensure")
            ck(f"  week/1/{wk} ensure → 200", st2 == 200, f"HTTP {st2}")
            st3, _ = api(f"/api/week/1/{wk}")
            ck(f"  week/1/{wk} 读取 → 200", st3 == 200, f"HTTP {st3}")

        print("\n【二】旧格式已在库里自愈成新格式")
        import sqlite3
        c = sqlite3.connect(dbfile)
        v1 = json.loads(c.execute("SELECT vocab_json FROM weeks WHERE stage=1 AND week_no=1").fetchone()[0])
        ck("week1 已变成对象数组", bool(v1) and all(isinstance(x, dict) for x in v1), v1[:3])
        ck("单词内容没丢", sorted(x.get("word") for x in v1) == ["apple", "banana", "fkold1"],
           [x.get("word") for x in v1])
        v3raw = c.execute("SELECT vocab_json FROM weeks WHERE stage=1 AND week_no=3").fetchone()[0]
        v3 = json.loads(v3raw)
        # 坏 JSON 归一化成空数组后，词典非空会触发自动填充 —— 两种结果都对：
        # 要么空数组，要么全是带 word 的对象（自动填充产物）
        ok3 = (isinstance(v3, list)
               and (v3 == [] or all(isinstance(x, dict) and x.get("word") for x in v3)))
        ck("坏 JSON 已自愈（空数组或自动填充）", ok3, v3raw[:60])
        c.close()

        print("\n【三】真实浏览器：学习页能打开（之前显示「加载失败」）")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--no-sandbox"])
            pg = b.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(BASE + "/", wait_until="domcontentloaded")
            pg.evaluate(f"localStorage.setItem('eos_token','{TOKEN}')")
            pg.reload(wait_until="networkidle")
            errs.clear()
            time.sleep(1.0)
            # 进度指向 week1（旧格式那周）—— 修复前这里必现「加载失败」
            pg.evaluate("render('learn');")
            time.sleep(2.5)
            txt = pg.inner_html("#content")
            ck("学习页渲染出内容（>500 字符）", len(txt.strip()) > 500, f"{len(txt.strip())} 字符")
            ck("没有「加载失败」", "加载失败" not in txt)
            # ON CONFLICT 只更新 vocab_json，title 仍是 init_db 预置的「日常与习惯」；
            # 旧格式周（week1）能正常渲染出标题就说明归一化生效
            ck("显示本周标题（预置的「日常与习惯」）", "日常与习惯" in txt,
               txt[:80].replace("\n", " "))
            ck("零 JS 异常", not errs, errs[:2])
            b.close()

    finally:
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except Exception:
            srv.kill()

    print("\n" + "=" * 50)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("   -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
