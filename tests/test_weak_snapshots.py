# -*- coding: utf-8 -*-
"""薄弱项每日快照落库 的测试。

跑法：
    cd backend && python3 ../tests/test_weak_snapshots.py

为什么必须有这张表：`/api/weakness` 给的是「近 30 天累计次数」——一个会随时间
滚动的当前值。历史某天的数值今天重算不出来，而趋势图 / 本周 vs 上周 / 连续周
要的恰恰是历史序列。所以快照必须存，前端 localStorage 只是离线兜底。

这里用真实 SQLite 文件库，验证真实的 SQL 与 ON CONFLICT 行为。
"""
import os
import sys
import tempfile

_tmpdb = tempfile.mktemp(suffix=".db")
os.environ["EOS_DB"] = _tmpdb
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

import db  # noqa: E402
import link  # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


def main():
    db.init_db()

    print("\n【1】迁移与建表")
    conn = db.get_conn()
    tbl = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='weak_snapshots'"
    ).fetchone()
    ck("weak_snapshots 表已创建", tbl is not None)
    if tbl is None:
        print("\n结果：❌ 表没建出来，后续测试无意义"); sys.exit(1)
    conn.close()

    print("\n【2】写入与读回")
    n = link.save_snapshots("2026-09-01", {"@grammar": 3, "grammar|tense": 5})
    ck("写入 2 个键", n == 2, n)
    snaps = link.load_snapshots()
    ck("读回 1 天", len(snaps) == 1, len(snaps))
    ck("日期正确", snaps[0]["d"] == "2026-09-01", snaps[0]["d"])
    ck("值正确 @grammar=3", snaps[0]["map"].get("@grammar") == 3, snaps[0]["map"])
    ck("值正确 grammar|tense=5", snaps[0]["map"].get("grammar|tense") == 5, snaps[0]["map"])
    ck("返回按日期升序（趋势算法依赖）", [s["d"] for s in snaps] == sorted(s["d"] for s in snaps))

    print("\n【3】同一天重复提交 → 覆盖而不是堆积")
    link.save_snapshots("2026-09-01", {"@grammar": 7, "grammar|tense": 5})
    snaps = link.load_snapshots()
    ck("仍只有 1 天", len(snaps) == 1, len(snaps))
    ck("@grammar 被覆盖为 7", snaps[0]["map"].get("@grammar") == 7, snaps[0]["map"])
    ck("grammar|tense 未受影响", snaps[0]["map"].get("grammar|tense") == 5)

    print("\n【4】多天并存、按日期升序（趋势基线靠这个）")
    link.save_snapshots("2026-09-03", {"@grammar": 1})
    link.save_snapshots("2026-09-02", {"@grammar": 9})
    snaps = link.load_snapshots()
    days = [s["d"] for s in snaps]
    ck("3 天都在", len(days) == 3, days)
    ck("顺序是 09-01 → 09-02 → 09-03", days == ["2026-09-01", "2026-09-02", "2026-09-03"], days)
    ck("09-02 的值是 9（验证排序没把值带错）",
       [s for s in snaps if s["d"] == "2026-09-02"][0]["map"].get("@grammar") == 9)

    print("\n【5】脏数据必须被挡住（日期是主键的一部分，脏数据会毁掉 ISO 周聚合）")
    ck("拒绝 '2026/09/01'", link.save_snapshots("2026/09/01", {"a": 1}) == 0)
    ck("拒绝 'abc'", link.save_snapshots("abc", {"a": 1}) == 0)
    ck("拒绝空字符串", link.save_snapshots("", {"a": 1}) == 0)
    ck("拒绝 None", link.save_snapshots(None, {"a": 1}) == 0)
    ck("拒绝非 dict 的 map", link.save_snapshots("2026-09-10", [1, 2, 3]) == 0)
    ck("拒绝空 map", link.save_snapshots("2026-09-10", {}) == 0)
    snaps = link.load_snapshots()
    ck("脏数据没进表", len(snaps) == 3, [s["d"] for s in snaps])

    print("\n【6】非数字值归 0，不让它污染聚合")
    link.save_snapshots("2026-09-04", {"@grammar": "oops", "@structure": None, "@reading": 4})
    m = [s for s in link.load_snapshots() if s["d"] == "2026-09-04"][0]["map"]
    ck("字符串 'oops' → 0", m.get("@grammar") == 0, m)
    ck("None → 0", m.get("@structure") == 0, m)
    ck("正常数字不受影响", m.get("@reading") == 4, m)

    print("\n【7】空 key 被跳过")
    n = link.save_snapshots("2026-09-05", {"": 1, "   ": 2, "@ok": 3})
    ck("只写入 1 个", n == 1, n)
    m = [s for s in link.load_snapshots() if s["d"] == "2026-09-05"][0]["map"]
    ck("@ok 写入成功，空 key 不在", list(m.keys()) == ["@ok"], list(m.keys()))

    print("\n【8】单日键数量上限（防止异常数据灌爆表）")
    big = {("k%d" % i): i for i in range(link.SNAP_MAX_KEYS + 200)}
    n = link.save_snapshots("2026-09-06", big)
    ck("被截断到 SNAP_MAX_KEYS", n == link.SNAP_MAX_KEYS, n)

    print("\n【9】老快照清理：只保留最近 60 天")
    conn = db.get_conn()
    conn.execute("DELETE FROM weak_snapshots")
    conn.commit()
    conn.close()
    for i in range(1, 66):                       # 2026-01-01 .. 2026-03-06
        link.save_snapshots("2026-%02d-%02d" % (1 + (i - 1) // 31, ((i - 1) % 31) + 1), {"@x": i})
    snaps = link.load_snapshots()
    ck("清理后剩 60 天", len(snaps) == link.SNAP_KEEP_DAYS, len(snaps))
    ck("留下的是最近的 60 天（最旧的是第 6 天）",
       snaps[0]["map"].get("@x") == 6, snaps[0])
    ck("最新的一天还在", snaps[-1]["map"].get("@x") == 65, snaps[-1])

    print("\n【10】init_db 幂等（连续跑三次不炸、不丢数据）")
    link.save_snapshots("2026-09-09", {"@persist": 42})
    for _ in range(3):
        db.init_db()
    snaps = link.load_snapshots()
    hit = [s for s in snaps if s["d"] == "2026-09-09"]
    ck("重跑 init_db 后数据仍在", bool(hit), len(snaps))
    ck("值没被覆盖", hit and hit[0]["map"].get("@persist") == 42, hit[0]["map"] if hit else None)

    print("\n" + "=" * 56)
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("❌ 失败项：")
        for f in FAIL:
            print("   - " + f)
        sys.exit(1)
    print("✅ 全部通过")
    try:
        os.unlink(_tmpdb)
    except OSError:
        pass


if __name__ == "__main__":
    main()
