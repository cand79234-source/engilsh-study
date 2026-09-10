# -*- coding: utf-8 -*-
"""浏览器实测：薄弱项每日快照从 localStorage 迁到服务端。

核心验证：清空浏览器 localStorage 后，趋势图依然能算出「改善 / 恶化」——
那只能说明数据来自服务端，而不是本机。
"""
import os
import sqlite3
import sys
import time

import requests
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8012"
TEST_DB = "/tmp/snap_test.db"
H = {"X-Auth-Token": "test123", "Content-Type": "application/json"}
PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


def api(p, method="GET", body=None):
    if method == "GET":
        return requests.get(BASE + "/api" + p, headers=H, timeout=10).json()
    return requests.post(BASE + "/api" + p, headers=H, json=body or {}, timeout=10).json()


def wipe():
    """直接清表：测试库跨轮复用，上一轮塞的日期会污染「最近非今天」的基线选取。"""
    c = sqlite3.connect(TEST_DB)
    c.execute("DELETE FROM weak_snapshots")
    c.commit()
    c.close()


def main():
    wipe()   # 清空，保证基线选取不受历史测试数据干扰

    print("\n【一】接口层：写入 / 读回 / 覆盖")
    r = api("/weak/snapshots")
    ck("GET 接口可用", r.get("ok") is True, r)
    ck("snapshots 是数组", isinstance(r.get("snapshots"), list))

    r2 = api("/weak/snapshots", "POST", {"d": "2026-09-01", "map": {"@grammar": 8, "grammar|tense": 4}})
    ck("POST 返回写入数量 2", r2.get("saved") == 2, r2)

    snaps = api("/weak/snapshots")["snapshots"]
    d1 = [s for s in snaps if s["d"] == "2026-09-01"]
    ck("读回 09-01", bool(d1), [s["d"] for s in snaps])
    ck("@grammar = 8", d1 and d1[0]["map"].get("@grammar") == 8, d1[0]["map"] if d1 else None)

    api("/weak/snapshots", "POST", {"d": "2026-09-01", "map": {"@grammar": 2, "grammar|tense": 4}})
    d1 = [s for s in api("/weak/snapshots")["snapshots"] if s["d"] == "2026-09-01"]
    ck("同一天重复提交 → 覆盖为 2（不堆积）", d1[0]["map"].get("@grammar") == 2, d1[0]["map"])

    print("\n【二】脏数据被挡在接口外")
    for bad in ["2026/09/01", "abc", "", None]:
        r3 = api("/weak/snapshots", "POST", {"d": bad, "map": {"x": 1}})
        ck(f"拒绝日期 {bad!r}", r3.get("saved") == 0, r3)
    r4 = api("/weak/snapshots", "POST", {"d": "2026-09-07", "map": [1, 2]})
    ck("拒绝非 dict 的 map", r4.get("saved") == 0, r4)

    print("\n【三】浏览器：清空本机后，趋势依然算得出来（证明数据在服务端）")
    # 再清一次表：趋势的基线取「最近的非今天那份」，上面【一】写的 09-01 会把它抢走。
    wipe()
    # 基线必须落在「上一周」：ISO 周按周一划分，同一周内的两天在 weakWeekStat 里
    # 会被聚合成一个点，算不出「本周 vs 上周」。往前推 7 天必定跨周。
    base_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))
    print(f"      基线日期 = {base_day}（上周）")
    api("/weak/snapshots", "POST", {"d": base_day, "map": {"@grammar": 20, "grammar|tense": 20}})

    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.evaluate("localStorage.setItem('eos_token','test123')")
        # 关键：抹掉本机快照，只留服务端
        pg.evaluate("localStorage.removeItem('eos_weak_snap_v1')")
        pg.reload(wait_until="networkidle")
        errs.clear()
        time.sleep(1.5)

        # 进薄弱项页（会触发 wsnapEnsure + weakSnapshot）
        pg.evaluate("render('errors');")
        time.sleep(2.5)

        wsnap_local = pg.evaluate("localStorage.getItem('eos_weak_snap_v1')")
        ck("本机被重写了一份（降级兜底仍在）", bool(wsnap_local))

        cache = pg.evaluate("_wsnapCache")
        ck("内存缓存已从服务端拉到", isinstance(cache, list) and len(cache) >= 2,
           (len(cache) if isinstance(cache, list) else cache))
        days = [s["d"] for s in (cache or [])]
        ck(f"缓存里含服务端的 {base_day}（本机从未有过）", base_day in days, days)

        # 趋势计算：基线 20 → 当前 3，应为「改善」
        trend = pg.evaluate("weakTrend('grammar', null, 3)")
        ck("趋势判定为「改善」（基线 20 → 当前 3）", trend.get("code") == "improve", trend)

        wk = pg.evaluate("weakWeekStat('grammar', null, 3)")
        ck("上周读到了服务端的 20（不是「首次记录」）", wk.get("lastWeek") == 20, wk)
        ck("本周数字来自今天的快照", wk.get("thisWeek") is not None, wk.get("thisWeek"))
        ck("趋势百分比为负（下降 = 改善）", (wk.get("pct") or 1) < 0, wk.get("pct"))
        ck("状态判定为「改善中」", wk.get("status", {}).get("code") == "improve",
           wk.get("status"))
        ck("本周为 0 → 连续周数归 0（符合「已消失」语义）", wk.get("consec") == 0,
           wk.get("consec"))
        ck("趋势序列含两周数据", len(wk.get("series") or []) == 2, wk.get("series"))

        # 本周有命中时，连续周数应该 ≥ 1 —— 注入一个非零值后强制刷新缓存验证
        today0 = pg.evaluate("_todayStr()")
        api("/weak/snapshots", "POST",
            {"d": today0, "map": {"@zzztest": 4, "zzztest|x": 4}})
        pg.evaluate("async()=>{ _wsnapCache=null; await wsnapEnsure(); }")
        wk2 = pg.evaluate("weakWeekStat('zzztest', null, 4)")
        ck("本周有命中 → 连续周数 ≥ 1", (wk2.get("consec") or 0) >= 1, wk2)

        print("\n【四】今天打开后，快照回写到服务端")
        today = pg.evaluate("_todayStr()")
        snaps = api("/weak/snapshots")["snapshots"]
        td = [s for s in snaps if s["d"] == today]
        ck(f"服务端出现今天的快照 {today}", bool(td), [s["d"] for s in snaps])
        ck("今天的快照有内容（不止一两个键）", td and len(td[0]["map"]) > 5,
           len(td[0]["map"]) if td else 0)

        print("\n【五】全站回归（确认没弄坏别的页面）")
        for mode in ["learn", "review", "test", "listen", "sum", "train", "errors"]:
            pg.evaluate(f"render({mode!r});")
            time.sleep(0.9)
            txt = pg.inner_html("#content")
            ck(f"{mode} 页正常渲染", len(txt.strip()) > 50, f"{len(txt.strip())} 字符")

        ck("全程零 JS 异常", not errs, errs[:3])
        b.close()

    print("\n【六】后端不可达时的降级（不能白屏、不能报错）")
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.evaluate("localStorage.setItem('eos_token','test123')")
        pg.reload(wait_until="networkidle")
        # 掐断快照接口，模拟后端不可用
        pg.route("**/api/weak/snapshots", lambda route: route.abort())
        errs.clear()
        pg.evaluate("render('errors');")
        time.sleep(2.5)
        txt = pg.inner_html("#content")
        ck("后端挂掉后薄弱项页照常渲染", len(txt.strip()) > 50, f"{len(txt.strip())} 字符")
        loc = pg.evaluate("localStorage.getItem('eos_weak_snap_v1')")
        ck("降级后本机快照仍在写", bool(loc))
        ck("降级路径无 JS 异常", not errs, errs[:3])
        b.close()

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("   -", f)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
