#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 8 周假数据跑出来的周报 / 月报 / 薄弱项，以人能看懂的形式打印出来。

用法：
    python3 fake8_show.py                      # 看 /tmp/fake8.db
    python3 fake8_show.py --db /tmp/fake8_big.db
    python3 fake8_show.py --weeks 8            # 指定周报覆盖几周

只读取、不写入，可以直接对任何库跑。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8056
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "faketest123"


def get(path, timeout=120):
    url = BASE + urllib.parse.quote(path, safe="/?=&")
    req = urllib.request.Request(url)
    req.add_header("X-Auth-Token", TOKEN)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def bar(val, mx, width=24, ch="█"):
    """画个简易柱状图"""
    if not mx:
        return ""
    n = max(0, min(width, int(round(val / mx * width))))
    return ch * n + "·" * (width - n)


def line(t=""):
    print(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/fake8.db")
    ap.add_argument("--weeks", type=int, default=8)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"❌ 找不到 {args.db}，先跑: python3 fake8_seed.py --db {args.db}")
        return 1

    backend = None
    for cand in [HERE, os.path.join(HERE, "backend"),
                 os.path.join(HERE, "..", "..", "..", "tmp", "pushtest", "backend")]:
        if os.path.exists(os.path.join(cand, "db.py")):
            backend = cand
            break
    if not backend:
        print("❌ 找不到 backend/db.py")
        return 1

    env = dict(os.environ)
    env["EOS_DB"] = args.db
    env["ACCESS_TOKEN"] = TOKEN
    env.pop("DATABASE_URL", None)
    log = open("/tmp/fake8_show_srv.log", "w")
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=backend, env=env, stdout=log, stderr=subprocess.STDOUT)

    try:
        t0 = time.time()
        while time.time() - t0 < 60:
            try:
                urllib.request.urlopen(
                    urllib.request.Request(BASE + "/api/health",
                                           headers={"X-Auth-Token": TOKEN}),
                    timeout=3)
                break
            except Exception:
                time.sleep(0.4)
        else:
            print("❌ 服务起不来：")
            print(open("/tmp/fake8_show_srv.log").read()[-1500:])
            return 1

        # ---------------- 月报 ----------------
        line("=" * 68)
        line("  月报（/api/report）")
        line("=" * 68)
        try:
            r = get("/api/report")
            if not r.get("ok"):
                line(f"  ⚠️  {r}")
            else:
                pr = r.get("progress") or {}
                # 这个接口返回的 progress 只带 stage / week，没有 day，按有无输出
                day = pr.get("day")
                day_s = f" / 第 {day} 天" if day is not None else ""
                line(f"  当前进度: 阶段 S{pr.get('stage')} / 第 {pr.get('week')} 周"
                     f"{day_s}")
                w = r.get("week") or {}
                line(f"  本周主题: {w.get('title')}   语法: {w.get('grammar')}")
                s = w.get("sentences") or {}
                line(f"  本周造句: 共 {s.get('total')} 条，正确 {s.get('good')} 条，"
                     f"均分 {s.get('avg')}")
                line(f"  本周复习: {w.get('reviews')} 条    周测次数: {w.get('quiz_count')}")
                m = r.get("month") or r.get("monthly") or {}
                if m:
                    line("")
                    line("  近 30 天汇总：")
                    for k, v in m.items():
                        line(f"      {k}: {v}")
                # 其余顶层字段（不同版本字段名可能不同，兜个底全列出来）
                known = {"ok", "progress", "week", "month", "monthly"}
                rest = {k: v for k, v in r.items() if k not in known}
                if rest:
                    line("")
                    line("  其它字段：")
                    for k, v in rest.items():
                        s = json.dumps(v, ensure_ascii=False)
                        line(f"      {k}: {s[:160]}")
        except Exception as e:
            line(f"  ❌ 月报读取失败: {type(e).__name__}: {e}")

        # ---------------- 周报 ----------------
        line("")
        line("=" * 68)
        line(f"  周报（/api/activity?weeks={args.weeks}）")
        line("=" * 68)
        try:
            a = get(f"/api/activity?weeks={args.weeks}")
            recent = a.get("recent") or []
            weeks = a.get("weeks") or []
            line(f"  最近活动 {len(recent)} 条：")
            for it in recent[:8]:
                line(f"      {it.get('date')}  {it.get('type'):<4} "
                     f"{str(it.get('detail'))[:32]:<34} "
                     f"分数 {it.get('score')}")
            if len(recent) > 8:
                line(f"      … 还有 {len(recent) - 8} 条")
            line("")
            line(f"  近 {len(weeks)} 周趋势：")
            line(f"      {'周':<6}{'学习天数':>8}{'听力正确率':>12}{'测评均分':>10}")
            for wk in weeks:
                la = wk.get("listening_acc")
                qa = wk.get("quiz_avg")
                line(f"      {wk.get('label'):<6}"
                     f"{wk.get('learning_days', 0):>8}"
                     f"{(str(la) + '%') if la is not None else '—':>12}"
                     f"{qa if qa is not None else '—':>10}")
            # 画个学习天数的柱状图
            if weeks:
                mx = max((w.get("learning_days") or 0) for w in weeks) or 1
                line("")
                line("  学习天数分布：")
                for wk in weeks:
                    d = wk.get("learning_days") or 0
                    line(f"      {wk.get('label'):<4} {bar(d, mx)} {d} 天")
        except Exception as e:
            line(f"  ❌ 周报读取失败: {type(e).__name__}: {e}")

        # ---------------- 薄弱项 ----------------
        line("")
        line("=" * 68)
        line("  薄弱项（/api/weakness）")
        line("=" * 68)
        try:
            wk = get("/api/weakness")
            ets = wk.get("error_types") or []
            if ets:
                line("  错误类型排行：")
                for e in ets[:8]:
                    line(f"      {e.get('level','')} {e.get('type'):<10} "
                         f"近30天 {e.get('count_30d'):>3} 次   "
                         f"累计 {e.get('total'):>3} 次")
                mx = max((e.get("count_30d") or 0) for e in ets) or 1
                line("")
                for e in ets[:8]:
                    c = e.get("count_30d") or 0
                    line(f"      {e.get('type'):<10} {bar(c, mx)} {c}")
            lsw = wk.get("low_star_words") or []
            if lsw:
                line("")
                line(f"  低星词（输出不达标）{len(lsw)} 个，前 8：")
                for w in lsw[:8]:
                    if isinstance(w, dict):
                        line(f"      {w.get('word'):<12} {w.get('stars')} 星  "
                             f"尝试 {w.get('total_attempts')} 次")
                    else:
                        line(f"      {w}")
            srcs = wk.get("sources")
            if srcs:
                line("")
                line("  分板块来源明细：")
                line("      " + json.dumps(srcs, ensure_ascii=False)[:400])
            # 兜底：把没展示的字段也列出来
            known = {"error_types", "low_star_words", "sources"}
            rest = {k: v for k, v in wk.items() if k not in known}
            if rest:
                line("")
                line("  其它字段：")
                for k, v in rest.items():
                    line(f"      {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
        except Exception as e:
            line(f"  ❌ 薄弱项读取失败: {type(e).__name__}: {e}")

        # ---------------- 错误趋势 ----------------
        line("")
        line("=" * 68)
        line("  错误趋势（/api/errors/trend）")
        line("=" * 68)
        try:
            tr = get("/api/errors/trend")
            line("      " + json.dumps(tr, ensure_ascii=False)[:500])
        except Exception as e:
            line(f"  ❌ {type(e).__name__}: {e}")

        # ---------------- 专项训练汇总 ----------------
        line("")
        line("=" * 68)
        line("  专项训练汇总（/api/training/summary）")
        line("=" * 68)
        try:
            ts = get("/api/training/summary")
            line("      " + json.dumps(ts, ensure_ascii=False)[:500])
        except Exception as e:
            line(f"  ❌ {type(e).__name__}: {e}")

        line("")
        line("=" * 68)
        line(f"  数据库: {args.db}  ({os.path.getsize(args.db)/1024/1024:.1f} MB)")
        line("  看完了想删：")
        line(f"      python3 {os.path.join(HERE,'fake8_seed.py')} --db {args.db} --clean")
        line(f"      rm -f {args.db}")
        line("=" * 68)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except Exception:
            srv.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
