#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「演示模式」用的固定数据包 backend/demo8.json。

思路和直接灌假数据完全不同：
  1. 往一个**临时库**里灌 8 周假数据
  2. 调**真实的**统计代码（report.build_report / link.build_weakness / ...）
  3. 把它们算出来的结果原样存成 JSON

好处：
  - 演示数据的结构和真实接口 100% 一致，不可能出现「演示能显示、真实就崩」
  - 演示模式运行时**不查库、不写库**，只返回这份 JSON，零污染
  - 数字是真实统计代码算出来的，不是手工编的，可复现

用法：
    python3 tests/demo8_gen.py                 # 生成 backend/demo8.json
    python3 tests/demo8_gen.py --scale 5       # 数据量 ×5
    python3 tests/demo8_gen.py --seed 7        # 换随机种子
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, HERE)

import fake8_seed  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(ROOT, "backend", "demo8.json"))
    ap.add_argument("--scale", type=int, default=1, help="数据量倍数")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，可复现）")
    args = ap.parse_args()

    tmpdb = tempfile.mktemp(suffix=".db")
    os.environ["EOS_DB"] = tmpdb
    try:
        import db as D
        D.init_db()

        conn = sqlite3.connect(tmpdb)
        conn.row_factory = sqlite3.Row
        n = fake8_seed.build(conn, args.scale, args.seed)
        conn.commit()

        # 把一批复习卡调度成「今天到期」。
        # fake8_seed 生成的是过去 8 周的学习流水，卡片的 next_due 散落在
        # 这 8 周里 —— 到期日属于 SRS 调度状态，不是统计结果，
        # 这里把它们推到今天，演示时复习页才真的有卡可翻。
        # 统计数字仍然全部由下面的真实接口计算，不受影响。
        today = __import__("datetime").date.today().isoformat()
        cur = conn.cursor()
        cur.execute(
            "UPDATE reviews SET next_due=? WHERE kind='vocab' AND id IN ("
            "  SELECT id FROM reviews WHERE kind='vocab' ORDER BY id LIMIT 12)",
            (today,))
        scheduled = cur.rowcount
        conn.commit()
        conn.close()
        print(f"[1/3] 已往临时库灌入假数据: {n}（另把 {scheduled} 张卡调度到今天到期）")

        # —— 用真实统计代码算出演示数据 ——
        import report as _report
        import link
        import services as svc

        print("[2/3] 调用真实统计接口计算…")
        data = {}
        try:
            data["report"] = _report.build_report()
            print("      /api/report      ✓")
        except Exception as e:
            print(f"      /api/report      ✗ {e}")
            raise
        try:
            data["weakness"] = link.build_weakness()
            print("      /api/weakness    ✓")
        except Exception as e:
            print(f"      /api/weakness    ✗ {e}")
            raise
        # 直接调 main 里的路由函数本体（不是 HTTP），拿到的就是接口真实返回值
        import main as M

        try:
            data["errors"] = M.errors_breakdown()
            print(f"      /api/errors      ✓ {len(data['errors']) if isinstance(data['errors'], list) else '?'} 条")
        except Exception as e:
            print(f"      /api/errors      ✗ {e}（降级为空）")
            data["errors"] = []
        try:
            data["error_bank"] = M.error_bank()
            print(f"      /api/error-bank  ✓ {len(data['error_bank']) if hasattr(data['error_bank'], '__len__') else '?'} 条")
        except Exception as e:
            print(f"      /api/error-bank  ✗ {e}（降级为空）")
            data["error_bank"] = {}
        try:
            data["errors_trend"] = M.errors_trend()
            print("      /api/errors/trend ✓")
        except Exception as e:
            print(f"      /api/errors/trend ✗ {e}（降级为空）")
            data["errors_trend"] = []
        # 复习闪卡 / 今日学习：这两个页面也要能在演示模式下翻，
        # 否则一进复习页就 404，看着像坏了。
        try:
            data["flashcards"] = M.review_flashcards()
            n_fc = len(data["flashcards"].get("items", []))
            print(f"      /api/review/flashcards ✓ {n_fc} 张")
        except Exception as e:
            print(f"      /api/review/flashcards ✗ {e}（降级为空）")
            data["flashcards"] = {"items": []}
        try:
            data["today"] = M.today()
            print("      /api/today        ✓")
        except Exception as e:
            print(f"      /api/today        ✗ {e}（降级为空）")
            data["today"] = {}

        # 其余页面（学习 / 复习 / 总结 / 薄弱项）要能在演示模式下正常打开，
        # 少一个接口页面就会缺一块甚至报错，所以这里一并快照。
        p = D.get_conn().execute(
            "SELECT stage, week, day FROM progress WHERE id=1").fetchone()
        stage = p["stage"] if p else 0
        week = p["week"] if p else 1
        D.get_conn().close()

        for key, label, fn in [
            ("progress", "/api/progress", lambda: M.progress()),
            ("home", "/api/home", lambda: M.home()),
            ("activity", "/api/activity", lambda: M.activity()),
            ("history", "/api/history", lambda: M.history()),
            ("vocab_all", "/api/vocab/all", lambda: M.vocab_all()),
            ("weak_snapshots", "/api/weak/snapshots", lambda: M.weak_snapshots_get()),
            ("sentence_history", "/api/sentence/history", lambda: M.sentence_history()),
            ("training_summary", "/api/training/summary", lambda: M.training_summary()),
            ("review_due", "/api/review/due", lambda: M.review_due()),
            ("week", "/api/week/{stage}/{week}", lambda: M.get_week(stage, week)),
            ("quiz", "/api/quiz/{stage}/{week}", lambda: M.quiz_get(stage, week)),
        ]:
            try:
                data[key] = fn()
                print(f"      {label:<28} ✓")
            except Exception as e:
                print(f"      {label:<28} ✗ {e}（降级为空）")
                data[key] = {} if key in ("home", "week") else []

        payload = {
            "_meta": {
                "generated_by": "tests/demo8_gen.py",
                "scale": args.scale,
                "seed": args.seed,
                "note": "演示模式专用。数据由真实统计代码算出后快照，运行时不查库、不写库。",
            },
            "data": data,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        size = os.path.getsize(args.out)
        print(f"[3/3] 已写出 {args.out} ({size:,} 字节)")
        return 0
    finally:
        try:
            os.unlink(tmpdb)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
