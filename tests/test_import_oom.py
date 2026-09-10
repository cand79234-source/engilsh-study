# -*- coding: utf-8 -*-
"""导入 OOM 修复的回归测试。

跑法：
    cd backend && python3 ../tests/test_import_oom.py

背景：weekimport 原本在每次导入时 `SELECT * FROM dictionary` 读全表并建 dict。
ECDICT 全量词典合入后该表有 76.8 万行，实测耗时 2.5s、峰值内存 492MB；
Render 免费实例只有 512MB，加 Python+FastAPI 基础占用直接 OOM，worker 被杀，
返回非 JSON，前端 r.json() 抛出 "Unexpected token" → 用户只看到「导入请求失败」。

修复：改成按本次导入涉及的词点查（_load_known）。
"""
import os
import resource
import sys
import tempfile

_tmpdb = tempfile.mktemp(suffix=".db")
os.environ["EOS_DB"] = _tmpdb
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

import db  # noqa: E402
import weekimport as wi  # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


def mem_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


TEXT = """第2周｜工作与日常｜3词
第1组｜第一天上班
colleague /ˈkɒliːɡ/ — 同事
company /ˈkʌmpəni/ — 公司
position /pəˈzɪʃn/ — 职位"""

TEXT_IPA_ONLY = """第3周｜日常｜2词
第1组｜第一天
continue /kənˈtɪnjuː/ — 继续
improve /ɪmˈpruːv/ — 改善"""


def main():
    db.init_db()
    conn = db.get_conn()

    print("\n【一】_load_known 只取需要的词，不读全表")
    # 预置 3 个词，另外塞 900 个干扰词（够分 3 批），验证不会被全带出来
    for i in range(900):
        conn.execute(
            "INSERT OR IGNORE INTO dictionary (word, meaning, pos) VALUES (?,?,?)",
            ("noise%d" % i, "干扰词", "名词"))
    conn.execute(
        "INSERT OR IGNORE INTO dictionary (word, meaning, pos) VALUES (?,?,?)",
        ("company", "公司", "名词"))
    conn.execute(
        "INSERT OR IGNORE INTO dictionary (word, meaning, pos) VALUES (?,?,?)",
        ("position", "职位", "名词"))
    conn.commit()

    known = wi._load_known(conn, ["company", "position", "notexist"])
    ck("只返回查到的 2 个词，没把 903 行全带出来", len(known) == 2, len(known))
    ck("company 取到了", "company" in known, sorted(known.keys()))
    ck("position 取到了", "position" in known, sorted(known.keys()))
    ck("查不到的词不进结果", "notexist" not in known)
    ck("词条内容正确（meaning=公司）",
       (known.get("company") or {}).get("meaning") == "公司", known.get("company"))

    print("\n【二】大小写与空白容错")
    known2 = wi._load_known(conn, ["  COMPANY  ", "", None, "Company"])
    ck("大写/空白被归一化，去重后只查一次", len(known2) == 1, list(known2.keys()))
    ck("归一化成小写 key", "company" in known2, list(known2.keys()))

    print("\n【三】空输入不查库")
    ck("空列表返回空 dict", wi._load_known(conn, []) == {})
    ck("全 None 返回空 dict", wi._load_known(conn, [None, "", "   "]) == {})

    print("\n【四】分批：超过单批上限也能全取到")
    many = ["noise%d" % i for i in range(900)]      # 900 > 默认 batch 400
    known3 = wi._load_known(conn, many)
    ck("900 个词分 3 批全部取到", len(known3) == 900, len(known3))
    conn.close()

    print("\n【五】完整导入链路：两个分支都要能出结果")
    for branch in ["import_rich_week", "import_rich_week_merge"]:
        fn = getattr(wi, branch)
        r = fn(TEXT, forced_stage=None, forced_week=2)
        ck(f"{branch} 返回 ok", r.get("ok") is True, r.get("error"))
        ck(f"{branch} 导入了 3 个词", r.get("total") == 3, r.get("total"))
        ck(f"{branch} 词清单正确",
           sorted(w.lower() for w in r.get("words") or []) == ["colleague", "company", "position"],
           r.get("words"))

    print("\n【六】带音标的格式照样能导（上一轮修的 IPA 不能回退）")
    r = wi.import_rich_week(TEXT_IPA_ONLY, forced_stage=None, forced_week=3)
    ck("带 IPA 音标导入成功", r.get("ok") is True, r.get("error"))
    ck("导入了 2 个词", r.get("total") == 2, r.get("total"))
    ck("词是 continue / improve",
       sorted(w.lower() for w in r.get("words") or []) == ["continue", "improve"],
       r.get("words"))

    print("\n【七】内存：导入不该把整个词典装进内存")
    before = mem_mb()
    for _ in range(5):
        wi.import_rich_week(TEXT, forced_stage=None, forced_week=2)
    after = mem_mb()
    grow = after - before
    # 修复前单次导入就会 +480MB；现在跑 5 次增量应该可以忽略
    ck("连续导入 5 次，内存增长 < 50MB", grow < 50, "增长 %.1f MB" % grow)

    print("\n【八】异常兜底：导入内部报错也要返回 JSON，不能裸 500")
    import main as _m
    orig = wi.import_rich_week
    wi.import_rich_week = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟炸了"))
    try:
        out = _m.words_import({"text": "第2周｜测试｜1词\n第1组\nfoo — 吧", "merge": False})
        ck("异常被捕获，返回 JSON", isinstance(out, dict) and out.get("ok") is False, out)
        ck("错误信息里带上异常类型", "RuntimeError" in (out.get("error") or ""), out.get("error"))
        ck("错误信息里带上异常内容", "模拟炸了" in (out.get("error") or ""), out.get("error"))
    finally:
        wi.import_rich_week = orig

    ck("兜底恢复后导入仍正常", _m.words_import({"text": TEXT, "week": 2}).get("ok") is True)

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
