# -*- coding: utf-8 -*-
"""浏览器实测：走真实 UI 导入，以及导入失败时用户到底看得到什么。

背景：后端一 500，前端 api() 里 `if (r.status !== 401) return r.json()`
会抛出 "Unexpected token"，用户只看到一句「导入请求失败」，真实原因全被吞掉。
这是「导入坏了但说不清哪里坏」的直接原因。

本测试跑在 76.8 万行词典的真实规模库上（8000 端口），全部走真实 UI 路径。
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


TEXT = """第2周｜工作与日常｜3词
第1组｜第一天上班
ztestcolleague /ˈkɒliːɡ/ — 同事
ztestcompany /ˈkʌmpəni/ — 公司
ztestposition /pəˈzɪʃn/ — 职位"""


def open_import_and_run(pg, text):
    """打开导入弹窗 → 填文本 → 点「解析并导入」→ 返回结果区文案。

    每次先清掉残留弹窗：导入成功后弹窗不会自动关闭（按钮变成「✅ 完成，点击
    关闭」），重复 showImport 会叠加多层 overlay，后层的 sheet 会挡住按钮。
    """
    # showImport 依赖 state.learn.d.progress；上一轮导入成功后 runImport 会重渲染
    # learn 页，state 可能尚未就绪，先渲染一次确保数据到位
    pg.evaluate("render('learn');")
    time.sleep(1.2)
    pg.evaluate("document.querySelectorAll('.overlay').forEach(o=>o.remove());")
    pg.evaluate("showImport();")
    time.sleep(0.8)
    pg.fill("#imp_rich", text)
    pg.click("#imp_run_btn")
    # 轮询读取：导入成功后 runImport 会在 400ms 后自动关掉弹窗，
    # 固定 sleep 太长会读到一个已经被移除的空节点。
    for _ in range(40):
        el = pg.query_selector("#imp_rich_result")
        if el:
            t = (el.inner_text() or "").strip()
            if t:
                return t
        time.sleep(0.15)
    el = pg.query_selector("#imp_rich_result")
    return (el.inner_text() if el else "").strip()


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.evaluate("localStorage.setItem('eos_token','test123')")
        pg.reload(wait_until="networkidle")
        errs.clear()
        time.sleep(1.5)

        print("\n【一】真实 UI 导入（76.8 万行词典）")
        pg.evaluate("render('learn');")
        time.sleep(1.5)
        out = open_import_and_run(pg, TEXT)
        ck("显示成功提示", "已合并到" in out, out[:80])
        ck("显示了本次导入的词数", "D1:3词" in out, out[:120])
        # 用导入结果返回的 stage/week 去查（当前进度是 stage=1，写死 0 会查空）
        got = pg.evaluate("""async(t)=>{
            const j = await api('/words/import',{method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({text:t, merge:true, week:2})});
            const w = await api('/week/'+j.stage+'/'+j.week);
            return JSON.stringify({stage:j.stage, week:j.week, total:j.total,
                words:(w&&w.vocab?w.vocab.map(v=>String(v.word||'').toLowerCase()):[])});
        }""", TEXT)
        import json as _json
        g = _json.loads(got or "{}")
        words = g.get("words") or []
        ck("导入结果带回了 stage/week", g.get("stage") is not None and g.get("week") == 2,
           f"stage={g.get('stage')} week={g.get('week')}")
        ck("3 个新词都进去了",
           all(w in words for w in ["ztestcolleague", "ztestcompany", "ztestposition"]),
           f"共 {len(words)} 词，含目标词: "
           + str([w for w in ["ztestcolleague", "ztestcompany", "ztestposition"] if w in words]))

        print("\n【二】后端 500 时，错误要看得见（不再是一句 Unexpected token）")
        # 下面两项是故意 mock 出 500 / ok:false 的，浏览器必然会记 console error。
        # 先把计数清零，免得把"预期内的报错"算成回归失败。
        errs.clear()
        pg.route("**/api/words/import", lambda route: route.fulfill(
            status=500, content_type="text/plain", body="Internal Server Error"))
        out2 = open_import_and_run(pg, TEXT)
        ck("结果区有内容", bool(out2), repr(out2[:60]))
        ck("带上了 HTTP 状态码", "500" in out2, out2[:120])
        ck("带上了服务器原文", "Internal Server Error" in out2, out2[:120])
        ck("不再显示没头没尾的 Unexpected token", "Unexpected token" not in out2, out2[:120])
        pg.unroute("**/api/words/import")

        print("\n【三】后端返回 ok:false 时，显示具体原因")
        pg.route("**/api/words/import", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"ok":false,"error":"没有识别到任何单词"}'))
        out3 = open_import_and_run(pg, TEXT)
        ck("显示了后端给的具体原因", "没有识别到任何单词" in out3, out3[:120])
        pg.unroute("**/api/words/import")
        errs.clear()          # mock 窗口结束，后续再出现的异常才是真问题

        print("\n【四】导入恢复后仍能正常用")
        out4 = open_import_and_run(pg, TEXT)
        ck("解除拦截后导入恢复正常", "已合并到" in out4, out4[:80])

        print("\n【五】全站回归")
        for mode in ["learn", "review", "test", "listen", "sum", "train", "errors"]:
            pg.evaluate(f"render({mode!r});")
            time.sleep(0.8)
            txt = pg.inner_html("#content")
            ck(f"{mode} 页正常渲染", len(txt.strip()) > 50, f"{len(txt.strip())} 字符")
        ck("全程零 JS 异常", not errs, errs[:3])
        b.close()

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("   -", f)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
