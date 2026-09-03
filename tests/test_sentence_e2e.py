"""造句功能改造 —— 真实浏览器端到端验收（Playwright）。

对应需求里必须实测的 7 条：
  ① I like you.            → 判正确，不进错题本
  ② I very like you.       → 判错、讲清原因、进错题本
  ③ He go to school every day. → 讲清第三人称单数错误
  ④ 正确但可优化的句子     → 不能被判错
  ⑤ 第一次错 + 第二次对    → 两条记录都保留
  ⑥ 单题复制              → 内容完整
  ⑦ 复制全部              → 当天内容完整

前置：需要本地已启动服务
    cd backend && EOS_DB=/tmp/eos_e2e.db python3.11 -m uvicorn main:app --port 8000

运行：python3.11 tests/test_sentence_e2e.py
"""
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {extra}")


def first_task_key(page):
    """拿到第①段基础造句的第一个 task_key。"""
    return page.evaluate("() => { const k = planKeys(); return k.length ? k[0] : ''; }")


def write_and_check(page, tk, sentence):
    """在指定题里写句子并点批改，返回该题最新一次结果对象。"""
    page.fill(f'[id="in_{tk}"]', sentence)
    page.click(f'[data-act="check"][data-val="{tk}"]')
    # 等这道题的历史条数增加
    page.wait_for_function(
        "([tk, n]) => histOf(tk).length >= n",
        arg=[tk, len(page.evaluate("tk => histOf(tk)", tk)) + 1],
        timeout=15000,
    )
    return page.evaluate("tk => { const l = histOf(tk); return l[l.length-1]; }", tk)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = ctx.new_page()
        js_errors = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        page.goto(BASE)
        page.wait_for_selector("#sentences .sitem", timeout=20000)
        print("=== 0. 学习页加载 ===")
        check("造句区已渲染", page.locator("#sentences .sitem").count() > 0)
        check("有「📋 复制全部」按钮", page.locator("#copyAllBtn").count() == 1)
        check("页面无 AI 入口", "找AI" not in page.content() and "找 AI" not in page.content())
        check("无 JS 错误", len(js_errors) == 0, js_errors)

        keys = page.evaluate("() => planKeys()")
        check("题目数量 > 3", len(keys) > 3, f"实际 {len(keys)}")
        tk1, tk2, tk3, tk4 = keys[0], keys[1], keys[2], keys[3]

        # ---------------- 用例 ① 正确句 ----------------
        print("=== ① I like you. → 正确、不进错题本 ===")
        a = write_and_check(page, tk1, "I like you.")
        check("判定为正确", a["ok"] is True, a)
        check("分数 >= 85", a["score"] >= 85, a["score"])
        check("无错误项", len(a["errors"]) == 0, a["errors"])
        res1 = page.locator(f'[id="res_{tk1}"]').inner_text()
        check("界面显示 ✅ 正确", "✅ 正确" in res1, res1[:120])
        check("界面显示「基本掌握」", "基本掌握" in res1)
        check("界面显示「没有明显错误」", "没有明显错误" in res1)
        bank = page.evaluate("async () => (await api('/error-bank')).items || []")
        check("错题本不含该正确句", all("I like you." not in (b.get("sentence_text") or "")
                                        for b in bank), bank)

        # ---------------- 用例 ④ 正确但可优化 ----------------
        print("=== ④ 正确但可优化 → 不判错、优化不扣分 ===")
        check("有「可以优化」建议", len(a["optimizations"]) > 0, a["optimizations"])
        check("优化区标注「原句没错」", "原句没错" in res1 or "不扣分" in res1, res1[:400])
        check("有优化仍判正确", a["ok"] is True and a["score"] >= 85)
        opt_txt = " ".join((o.get("suggestion") or "") + (o.get("reason") or "")
                           for o in a["optimizations"])
        check("优化说明含「不代表原句有错」类措辞",
              "不代表" in opt_txt or "并不是错" in opt_txt or "原句完全正确" in opt_txt,
              opt_txt[:200])

        # ---------------- 用例 ② 错句 ----------------
        print("=== ② I very like you. → 判错 + 讲原因 + 进错题本 ===")
        b = write_and_check(page, tk2, "I very like you.")
        check("判定为错误", b["ok"] is False, b)
        check("分数 < 85", b["score"] < 85, b["score"])
        check("定位到 very like", any("very like" in (e["where"] or "") for e in b["errors"]),
              b["errors"])
        check("正确表达是 really like",
              any("really like" in (e["correct"] or "") for e in b["errors"]), b["errors"])
        why = " ".join(e["explanation"] or "" for e in b["errors"])
        check("解释了为什么错（提到 very 不能修饰动词）",
              "very" in why and ("动词" in why or "修饰" in why), why[:200])
        check("整句改写为 I really like you.", b["corrected"] == "I really like you.",
              b["corrected"])
        res2 = page.locator(f'[id="res_{tk2}"]').inner_text()
        check("界面显示 ❌ 有错误", "❌ 有错误" in res2, res2[:120])
        check("界面显示「哪里错了」", "哪里错了" in res2)
        check("界面显示「为什么错」", "为什么错" in res2)
        check("界面显示「正确表达」", "正确表达" in res2)
        check("界面显示「需要改进」", "需要改进" in res2)
        bank = page.evaluate("async () => (await api('/error-bank')).items || []")
        hit = [x for x in bank if "I very like you." in (x.get("sentence_text") or "")]
        check("错题本已记录该错句", len(hit) == 1, bank)
        if hit:
            e = hit[0]
            check("错题本含错误位置", bool(e.get("error_text")), e)
            check("错题本含错误原因", bool(e.get("explanation")), e)
            check("错题本含正确表达", bool(e.get("corrected")), e)
            check("错题本含首次出错时间", bool(e.get("first_at")), e)
            check("错题本含出现次数", int(e.get("times") or 0) >= 1, e)
            check("错题本含是否已改正字段", "fixed" in e, e)

        # ---------------- 用例 ③ 第三人称单数 ----------------
        print("=== ③ He go to school every day. → 讲清三单错误 ===")
        c = write_and_check(page, tk3, "He go to school every day.")
        check("判定为错误", c["ok"] is False, c)
        why3 = " ".join(e["explanation"] or "" for e in c["errors"])
        check("解释提到第三人称单数",
              "三" in why3 or "第三人称" in why3 or "单数" in why3, why3[:200])
        check("正确表达含 goes", any("goes" in (e["correct"] or "") for e in c["errors"]),
              c["errors"])
        check("整句改写为 He goes to school every day.",
              c["corrected"] == "He goes to school every day.", c["corrected"])

        # ---------------- 用例 ⑤ 先错后对，两条都留 ----------------
        print("=== ⑤ 第一次错 + 第二次对 → 两条记录都保留 ===")
        w1 = write_and_check(page, tk4, "I very like you.")
        check("第 1 次记为 attempt=1", w1["attempt"] == 1, w1["attempt"])
        check("第 1 次判错", w1["ok"] is False)
        # 点「重新作答」
        check("出现「✏️ 重新作答」按钮", page.locator(f'[id="redo_{tk4}"]').is_visible())
        page.click(f'[id="redo_{tk4}"]')
        w2 = write_and_check(page, tk4, "I really like you.")
        check("第 2 次记为 attempt=2", w2["attempt"] == 2, w2["attempt"])
        check("第 2 次判对", w2["ok"] is True and w2["score"] >= 85, w2)
        hist = page.evaluate("tk => histOf(tk)", tk4)
        check("前端保留 2 条历史", len(hist) == 2, len(hist))
        check("两条 sentence_id 不同", hist[0]["id"] != hist[1]["id"], hist)
        res4 = page.locator(f'[id="res_{tk4}"]').inner_text()
        check("界面同时显示第1次和第2次", "第 1 次作答" in res4 and "第 2 次作答" in res4,
              res4[:200])
        check("界面上第1次仍显示 ❌，第2次显示 ✅",
              "❌ 有错误" in res4 and "✅ 正确" in res4, res4[:300])
        # 后端也必须两条都在
        srv = page.evaluate(
            "async tk => (await api('/sentence/attempts/' + tk)).attempts || []", tk4)
        check("后端保留 2 条作答", len(srv) == 2, srv)
        check("后端第1条是错句", any(s["sentence"] == "I very like you." for s in srv), srv)
        check("后端第2条是对句", any(s["sentence"] == "I really like you." for s in srv), srv)
        # 错题本第一条错误记录不能被删，只应被标记已改正
        bank = page.evaluate("async () => (await api('/error-bank')).items || []")
        hit = [x for x in bank if "I very like you." in (x.get("sentence_text") or "")]
        check("第一次的错题记录未被删除", len(hit) >= 1, bank)
        check("第二次答对后标记为已改正", any(int(x.get("fixed") or 0) == 1 for x in hit), hit)
        check("答对的句子没有被写进错题本",
              all("I really like you." not in (x.get("sentence_text") or "") for x in bank), bank)

        # ---------------- 用例 ⑥ 单题复制 ----------------
        print("=== ⑥ 单题复制 → 内容完整 ===")
        page.click(f'[id="cp_{tk4}"]')
        time.sleep(0.4)
        txt = page.evaluate("() => navigator.clipboard.readText()")
        for kw in ("【造句批改】", "单词：", "我的句子：", "得分：", "判定："):
            check(f"复制文本含「{kw}」", kw in txt, txt[:200])
        check("复制文本含分数数字", re.search(r"得分：\d+/100", txt) is not None, txt[:200])
        # 换成复制第 1 次（错句），校验错误四要素齐全
        page.click(f'[data-act="copyOne"][data-val="{tk4}"][data-val2="1"]')
        time.sleep(0.4)
        txt1 = page.evaluate("() => navigator.clipboard.readText()")
        for kw in ("哪里错了：", "类型：", "为什么错：", "正确表达：", "整句正确写法："):
            check(f"错句复制含「{kw}」", kw in txt1, txt1[:300])
        check("错句复制含原句", "I very like you." in txt1, txt1[:200])

        # ---------------- 用例 ⑦ 复制全部 ----------------
        print("=== ⑦ 复制全部 → 当天内容完整 ===")
        page.click("#copyAllBtn")
        time.sleep(0.5)
        allt = page.evaluate("() => navigator.clipboard.readText()")
        check("含总标题", "【今日造句批改记录】" in allt, allt[:120])
        check("含日期", "日期：" in allt)
        check("含进度", "进度：阶段" in allt)
        check("含题数统计", re.search(r"共 \d+ 题", allt) is not None, allt[:200])
        for s in ("I like you.", "I very like you.", "He go to school every day.",
                  "I really like you."):
            check(f"含当天句子「{s}」", s in allt, allt[:200])
        check("同一题的两次作答都在", allt.count("I very like you.") >= 2, allt.count("I very like you."))
        check("复制全部无 AI 请求措辞外泄（只是纯记录）", "openai" not in allt.lower())

        # ---------------- 收尾 ----------------
        print("=== 8. 错题本页面 ===")
        page.click('.bottomnav a[data-page="errors"]')
        page.wait_for_timeout(1500)
        body = page.content()
        check("薄弱项页有「错题本」区", "错题本" in body, body[:200])
        check("错题本列出 very like", "very like" in body, "")
        check("全程无 JS 错误", len(js_errors) == 0, js_errors)

        browser.close()

    print(f"\n========== 结果: {passed} 通过, {failed} 失败 ==========")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
