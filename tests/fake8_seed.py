#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 8 周（56 天）模拟学习数据，用来压测周报 / 月报 / 薄弱项。

用法：
    python3 fake8_seed.py                      # 生成到 /tmp/fake8.db（默认）
    python3 fake8_seed.py --db /path/to.db     # 指定库
    EOS_DB=/path/to.db python3 fake8_seed.py   # 环境变量也认
    python3 fake8_seed.py --scale 5            # 数据量 ×5（压测用）
    python3 fake8_seed.py --scale 20           # 数据量 ×20（极限压测）
    python3 fake8_seed.py --clean              # 只清掉假数据，不生成

安全设计：
  - 默认写到 /tmp/fake8.db 这个**独立文件**，绝不会碰到你的真实数据库
  - 所有假数据都带可识别标记，--clean 能精确删干净
  - 生成前会检查目标库是否已有真实数据（行数 > 阈值时拒绝执行）

数据设计（覆盖周报 / 月报 / 薄弱项要用到的每一张表）：
  weeks / sentences / reviews / quizzes / listening_progress /
  errors / word_output / day_items / history /
  training_projects / training_sessions / training_rounds / training_attempts /
  weak_snapshots

刻意做出「趋势」而不是一堆相同的数字：
  - 造句均分从第 1 周的 62 分逐步爬到第 8 周的 84 分
  - 错误集中在 5 个类型上，其中「非谓语」最顽固（前期多、后期才降）
  - 听力正确率、周测分数、五星输出都随周次改善
  这样周报 / 月报才有东西可看，薄弱项才算得出高低。
"""
import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

# ---------------- 假数据的可识别标记 ----------------
# 所有假数据都带上这个前缀，--clean 时按它精确删除，不会误伤真实数据。
FAKE_TAG = "FAKE8"
FAKE_WORD_PREFIX = "fk"          # 假单词统一 fk 开头，一眼能认出
DEFAULT_DB = "/tmp/fake8.db"

# 第 1 天是多久以前：默认 8 周前
DAYS = 56
WEEKS = 8

# 中等强度：每天造句 5 条、复习 8 个、每周 1 次周测 + 2 次听力
PER_DAY_SENTENCES = 5
PER_WEEK_QUIZZES = 1
PER_WEEK_LISTENING = 2
PER_DAY_ERRORS = 3
PER_DAY_VOCAB = 6

# 五类错误，「非谓语」最顽固
ERROR_TYPES = [
    ("非谓语", 0.30, 0.15),   # (类型, 第1周占比, 第8周占比)
    ("时态",   0.24, 0.18),
    ("三单",   0.20, 0.16),
    ("冠词",   0.14, 0.25),
    ("介词",   0.12, 0.26),
]

THEMES = ["工作与日常", "旅行出行", "饮食健康", "学习教育",
          "科技数码", "家庭关系", "购物消费", "天气环境"]

GRAMMAR_POINTS = ["一般现在时", "一般过去时", "现在完成时", "被动语态",
                  "非谓语动词", "定语从句", "情态动词", "比较级"]


def ts(dt):
    """统一时间格式：'YYYY-MM-DDTHH:MM:SS'（report.py 用 created_at LIKE 匹配）"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def lerp(a, b, t):
    """线性插值：t=0 返回 a，t=1 返回 b"""
    return a + (b - a) * t


def pick_error_type(week_idx, rnd):
    """按周次加权挑一个错误类型 —— 早期「非谓语」多，后期慢慢降"""
    t = week_idx / max(1, WEEKS - 1)
    weights = [lerp(w0, w1, t) for (_, w0, w1) in ERROR_TYPES]
    names = [e[0] for e in ERROR_TYPES]
    return rnd.choices(names, weights=weights, k=1)[0]


def build(conn, scale, seed):
    rnd = random.Random(seed)
    cur = conn.cursor()

    # 起始日期：DAYS 天前，对齐到周一
    end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=DAYS - 1)

    stage = 1
    n = {"weeks": 0, "sentences": 0, "reviews": 0, "quizzes": 0,
         "listening": 0, "errors": 0, "word_output": 0, "day_items": 0,
         "history": 0, "projects": 0, "sessions": 0, "rounds": 0,
         "attempts": 0, "snapshots": 0}

    # ---------- weeks：8 周课程表 ----------
    # 注意：init_db() 已经预置了课程周，且 (stage, week_no) 有唯一约束，
    # 所以这里必须 upsert（已存在就改标题，不存在才插）。
    for w in range(1, WEEKS + 1):
        cur.execute(
            "INSERT INTO weeks (stage, week_no, title, grammar, vocab_json, topics) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(stage, week_no) DO UPDATE SET "
            "  title=excluded.title, grammar=excluded.grammar, "
            "  vocab_json=excluded.vocab_json, topics=excluded.topics",
            (stage, w, f"{FAKE_TAG}-{THEMES[(w - 1) % len(THEMES)]}-W{w}",
             GRAMMAR_POINTS[(w - 1) % len(GRAMMAR_POINTS)],
             json.dumps([f"{FAKE_WORD_PREFIX}{w}{i}" for i in range(10)],
                        ensure_ascii=False),
             json.dumps([f"话题{w}-{i}" for i in range(3)], ensure_ascii=False)))
        n["weeks"] += 1

    # ---------- 逐天生成 ----------
    for day_offset in range(DAYS):
        day_dt = start + timedelta(days=day_offset)
        week_idx = day_offset // 7          # 0..7
        week_no = week_idx + 1
        day_no = (day_offset % 7) + 1       # 1..7
        t = week_idx / max(1, WEEKS - 1)    # 0→1 的时间进度

        # 偶尔缺勤：真实用户不会天天打卡。不放进波动的话，周报里「每周学习
        # 天数」清一色是 7、错误趋势每周固定 21 次，一眼假，也测不出趋势图。
        rest_today = rnd.random() < 0.12
        if rest_today:
            continue

        # 造句：均分 62 → 84，正确率 45% → 80%
        for k in range(PER_DAY_SENTENCES * scale):
            word = f"{FAKE_WORD_PREFIX}{week_no}{(k % 10)}"
            avg = lerp(62, 84, t)
            score = max(0, min(100, int(rnd.gauss(avg, 12))))
            good = 1 if rnd.random() < lerp(0.45, 0.80, t) else 0
            etype = pick_error_type(week_idx, rnd) if not good else ""
            cur.execute(
                "INSERT INTO sentences (stage, week, day, word, task_key, attempt, "
                "original, corrected, error_type, explanation, ai_source, good, "
                "score, verdict, errors_json, opts_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (stage, week_no, day_no, word, f"basic:{k}", 1,
                 f"I am go to school about {word}.",
                 f"I go to school about {word}.",
                 etype, f"{FAKE_TAG} 模拟讲解", "fake", good, score,
                 "正确" if good else "有错误",
                 json.dumps([{"type": etype}] if etype else [], ensure_ascii=False),
                 "{}", ts(day_dt + timedelta(hours=9, minutes=k * 3))))
            n["sentences"] += 1

        # 复习：SRS 卡片，每个词一张（表上有 UNIQUE(kind, ref_key, prompt)）。
        # 重复复习同一个词时累加对错次数、推进间隔，而不是插新行。
        for k in range(max(1, PER_DAY_VOCAB * scale // 2)):
            word = f"{FAKE_WORD_PREFIX}{week_no}{(k % 10)}"
            tc = rnd.randint(0, 2)
            tw = max(0, int(rnd.gauss(lerp(1.2, 0.4, t), 0.9)))
            cur.execute(
                "INSERT INTO reviews (kind, ref_key, prompt, answer, stage, week, day, "
                "ease, interval, reps, next_due, last_score, total_correct, "
                "total_wrong, last_reviewed, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kind, ref_key, prompt) DO UPDATE SET "
                "  reps=reviews.reps+1, "
                "  total_correct=reviews.total_correct+excluded.total_correct, "
                "  total_wrong=reviews.total_wrong+excluded.total_wrong, "
                "  last_score=excluded.last_score, "
                "  last_reviewed=excluded.last_reviewed, "
                "  interval=MIN(reviews.interval+1, 21)",
                ("vocab", word, f"{word} 的中文", "模拟释义",
                 stage, week_no, day_no, 2.5, 1, 1,
                 (day_dt + timedelta(days=1)).strftime("%Y-%m-%d"),
                 int(lerp(60, 88, t)), tc, tw, ts(day_dt), ts(day_dt)))
            n["reviews"] += 1

        # 错误本：每天条数有波动（1~PER_DAY_ERRORS+2），一部分已改正
        for k in range(rnd.randint(1, PER_DAY_ERRORS + 2) * scale):
            etype = pick_error_type(week_idx, rnd)
            fixed = 1 if rnd.random() < lerp(0.20, 0.55, t) else 0
            cur.execute(
                "INSERT INTO errors (error_type, original, corrected, explanation, "
                "source, created_at, word, task_key, error_text, sentence_text, "
                "times, first_at, last_at, fixed, fixed_at, stage, week, day) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (etype, f"He go to school about {etype}.",
                 f"He goes to school about {etype}.",
                 f"{FAKE_TAG} 讲解", "造句", ts(day_dt),
                 f"{FAKE_WORD_PREFIX}{week_no}{(k % 10)}", f"basic:{k}",
                 f"{etype} 错误", f"He go to school about {etype}.",
                 rnd.randint(1, 4), ts(day_dt), ts(day_dt), fixed,
                 ts(day_dt) if fixed else None, stage, week_no, day_no))
            n["errors"] += 1

        # 背词历史（/api/activity 会读）
        cur.execute(
            "INSERT INTO history (date, stage, week, day, action, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (day_dt.strftime("%Y-%m-%d"), stage, week_no, day_no, "learn_vocab",
             f"{FAKE_TAG} 学习 {PER_DAY_VOCAB * scale} 个词", ts(day_dt)))
        n["history"] += 1

        # 每日词条（day_items）
        for k in range(3 * scale):
            cur.execute(
                "INSERT INTO day_items (stage, week, day, kind, ref_key, "
                "payload_json, mastered, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (stage, week_no, day_no, "vocab",
                 f"{FAKE_WORD_PREFIX}{week_no}{k}",
                 json.dumps({"word": f"{FAKE_WORD_PREFIX}{week_no}{k}",
                             "meaning": "模拟释义"}, ensure_ascii=False),
                 1 if rnd.random() < lerp(0.3, 0.7, t) else 0, ts(day_dt)))
            n["day_items"] += 1

        # 薄弱项每日快照：56 天 × 5 类，趋势是「逐步下降」
        # 这样 /api/weakness 的趋势图、本周 vs 上周、连续周才算得出东西
        for etype, w0, w1 in ERROR_TYPES:
            base = lerp(9, 2, t)
            val = max(0, int(rnd.gauss(base, 1.8)))
            cur.execute(
                "INSERT INTO weak_snapshots (d, item_key, val, updated_at) "
                "VALUES (?,?,?,?)",
                (day_dt.strftime("%Y-%m-%d"), f"@{etype}", val, ts(day_dt)))
            n["snapshots"] += 1

    # ---------- 每周：周测 + 听力 ----------
    for week_idx in range(WEEKS):
        week_no = week_idx + 1
        t = week_idx / max(1, WEEKS - 1)
        wdt = start + timedelta(days=week_idx * 7 + 4)

        for _ in range(PER_WEEK_QUIZZES * scale):
            qs = lerp(55, 88, t)
            score = max(0, min(100, int(rnd.gauss(qs, 10))))
            detail = []
            for qn in range(10):
                ok = rnd.random() < lerp(0.5, 0.85, t)
                et = pick_error_type(week_idx, rnd)
                detail.append({"no": qn + 1, "ok": ok, "type": et,
                               # 带上 FAKE_TAG：清理时靠它认出假数据
                               # （quizzes 没有 word 之类的业务字段可挂标记）
                               "q": f"{FAKE_TAG} Q{qn+1} about {et}",
                               "a": "wrong" if not ok else "right",
                               "right": "right"})
            cur.execute(
                "INSERT INTO quizzes (kind, stage, week, score, passed, "
                "detail_json, created_at) VALUES (?,?,?,?,?,?,?)",
                ("weekly", stage, week_no, score, 1 if score >= 60 else 0,
                 json.dumps(detail, ensure_ascii=False), ts(wdt)))
            n["quizzes"] += 1

        # 表上有 UNIQUE(stage, week, day)：一天最多一条进度记录。
        # scale 放大后一周可能排到十几次，超出 7 天就会撞约束 ——
        # 所以按天取模并 upsert，同一天多次练习累加题数（这本来就是真实语义）。
        for li in range(PER_WEEK_LISTENING * scale):
            tot = 10
            acc = lerp(0.45, 0.82, t)
            done = max(0, min(tot, int(rnd.gauss(acc * tot, 1.6))))
            cur.execute(
                "INSERT INTO listening_progress (stage, week, day, listening_done, "
                "listening_total, parts_json, created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(stage, week, day) DO UPDATE SET "
                "  listening_done=listening_progress.listening_done+excluded.listening_done, "
                "  listening_total=listening_progress.listening_total+excluded.listening_total",
                (stage, week_no, (li % 7) + 1, done, tot,
                 # parts_json 塞个标记：这张表没有别的字段能挂识别信息
                 json.dumps({"tag": FAKE_TAG}, ensure_ascii=False),
                 ts(wdt + timedelta(days=li % 7))))
            n["listening"] += 1

    # ---------- 五星输出：有的 5 星有的 1 星（低星会进薄弱项）----------
    for week_no in range(1, WEEKS + 1):
        t = (week_no - 1) / max(1, WEEKS - 1)
        for k in range(10 * scale):
            word = f"{FAKE_WORD_PREFIX}{week_no}{k}"
            # 前面几周星低，后面变高；故意留几个 1-2 星的词当薄弱点
            star = max(1, min(5, int(rnd.gauss(lerp(2.2, 4.3, t), 1.1))))
            if k < 2:                     # 每周固定留 2 个顽固低星词
                star = rnd.choice([1, 2])
            cur.execute(
                "INSERT INTO word_output (word, stars, total_attempts, last_result, "
                "last_score, first_at, last_at, updated_at, stage, week) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                # first_at 取「该周最后一天」而不是第一天：月报按自然月统计
                # word_output.first_at，取第一天会让最后一周掉到上个月，
                # 导致「本月新学词汇」恒为 0。学完一周再输出也更符合真实节奏。
                (word, star, rnd.randint(3, 12),
                 "good" if star >= 3 else "weak",
                 lerp(50, 85, t), ts(start + timedelta(days=(week_no - 1) * 7 + 6)),
                 ts(start + timedelta(days=(week_no - 1) * 7 + 6)),
                 ts(start + timedelta(days=(week_no - 1) * 7 + 6)),
                 stage, week_no))
            n["word_output"] += 1

    # ---------- 专项训练四层 ----------
    for pi in range(3 * scale):
        pkey = f"{FAKE_TAG}-P{pi}"
        ability = GRAMMAR_POINTS[pi % len(GRAMMAR_POINTS)]
        items = []
        for qi in range(3):
            items.append({
                "id": f"q{qi}", "type": "CHOICE",
                "prompt": f"Choose the right form about {ability} #{qi}",
                "options": [{"key": "A", "text": "went"}, {"key": "B", "text": "go"}],
                "answer": "A", "ability": ability})
        cur.execute(
            "INSERT INTO training_projects (ability, problem, prompt_md, created_at, "
            "project_key, priority, intervention_level, training_goal, "
            "training_boundary, forbidden_json, exit_standard, exit_rule_json, "
            "status, items_json, updated_at, stage, week) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ability, f"{FAKE_TAG} {ability} 掌握不牢",
             f"{FAKE_TAG} 训练题：{ability}", ts(start), pkey, "P1", "SUGGESTED",
             f"掌握 {ability}", "只练单句", "[]", "连续 3 次独立正确", "{}",
             "IN_PROGRESS", json.dumps(items, ensure_ascii=False),
             ts(end), stage, 1))
        n["projects"] += 1
        pid = cur.lastrowid

        for si in range(8 * scale // max(1, scale)):
            sdt = start + timedelta(days=si * 7 + 2)
            sid = f"{pkey}-S{si}"
            t = si / max(1, 7)
            cur.execute(
                "INSERT INTO training_sessions (session_id, project_key, started_at, "
                "ended_at, round_count, valid_attempts, correct_count, "
                "incorrect_count, hint_count, independent_correct_count, "
                "consecutive_independent_correct, final_status, next_step, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, pkey, ts(sdt), ts(sdt + timedelta(minutes=10)),
                 2, 6, int(lerp(2, 5, t)), int(lerp(4, 1, t)),
                 int(lerp(3, 1, t)), int(lerp(1, 4, t)), int(lerp(0, 3, t)),
                 # 取值必须跟前端一致：PASS / NEEDS_REVIEW / NOT_YET
                 # （见 db.py 建表注释与 index.html 里的判断）。
                 # 写过 'PASSED' 会导致 /api/training/summary 的 passed 恒为 0。
                 ("PASS" if t > 0.6 else
                  ("NEEDS_REVIEW" if t > 0.3 else "NOT_YET")),
                 "继续下一轮", ts(sdt)))
            n["sessions"] += 1

            for ri in range(2):
                rid = f"{sid}-R{ri}"
                cur.execute(
                    "INSERT INTO training_rounds (round_id, session_id, project_key, "
                    "idx, started_at, ended_at) VALUES (?,?,?,?,?,?)",
                    (rid, sid, pkey, ri, ts(sdt),
                     ts(sdt + timedelta(minutes=5))))
                n["rounds"] += 1

                for qi in range(3):
                    ok = 1 if rnd.random() < lerp(0.4, 0.85, t) else 0
                    cur.execute(
                        "INSERT INTO training_attempts (project_id, round, "
                        "user_sentence, score, ok, errors_json, created_at, "
                        "attempt_id, session_id, round_id, question_id, project_key, "
                        "user_answer, is_correct, manual, used_hint, hint_level, "
                        "is_independent, word) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, ri, "", 100 if ok else 40, ok, "[]",
                         ts(sdt), f"{rid}-A{qi}", sid, rid, f"q{qi}", pkey,
                         "A" if ok else "B", ok, 0, 1 - ok, 1 - ok,
                         ok, f"{FAKE_WORD_PREFIX}1{qi}"))
                    n["attempts"] += 1

    # ---------- 把进度指到最后一周 ----------
    # 假数据写在 stage=1，而 init_db() 默认进度是 stage=0 week=3。
    # 不指过去的话，月报里的「本周造句/复习/周测」会全部是 0
    # （因为按 (stage, week) 查不到任何东西），看起来像算错了。
    try:
        cur.execute(
            "UPDATE progress SET stage=?, week=?, day=?, last_activity='vocab', "
            "updated_at=? WHERE id=(SELECT MIN(id) FROM progress)",
            (stage, WEEKS, 7, ts(end)))
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO progress (stage, week, day, last_activity, updated_at) "
                "VALUES (?,?,?,?,?)", (stage, WEEKS, 7, "vocab", ts(end)))
    except Exception as e:
        print(f"   （进度表更新跳过: {e}）")

    conn.commit()
    return n


def clean(conn):
    """删掉所有假数据。按标记精确删除，不碰真实数据。

    删除顺序必须遵守外键依赖：training_attempts / rounds / sessions 都引用
    training_projects，先删父表会被外键约束拦下，而异常一旦被 except 吞掉
    就成了「看起来删了其实一行没动」。所以子表必须先删干净。
    """
    cur = conn.cursor()
    removed = {}

    def _del(key, sql, args=()):
        try:
            cur.execute(sql, args)
            if cur.rowcount:
                removed[key] = cur.rowcount
        except Exception as e:
            removed[key] = f"失败: {type(e).__name__}: {e}"

    # 1) 训练四层：子表 → 父表（顺序不能反）
    _del("training_attempts",
         f"DELETE FROM training_attempts WHERE project_key LIKE '{FAKE_TAG}%'")
    _del("training_rounds",
         f"DELETE FROM training_rounds WHERE project_key LIKE '{FAKE_TAG}%'")
    _del("training_sessions",
         f"DELETE FROM training_sessions WHERE project_key LIKE '{FAKE_TAG}%'")
    _del("training_projects",
         f"DELETE FROM training_projects WHERE project_key LIKE '{FAKE_TAG}%'")

    # 2) 按单词前缀识别的（fk 开头）
    for t, col in [("sentences", "word"), ("errors", "word"),
                   ("word_output", "word"), ("reviews", "ref_key"),
                   ("day_items", "ref_key")]:
        _del(f"{t}({col}~)",
             f"DELETE FROM {t} WHERE {col} LIKE '{FAKE_WORD_PREFIX}%'")

    # 3) 按文本标记识别的
    for t, col in [("history", "detail"), ("weeks", "title"),
                   ("quizzes", "detail_json"),
                   ("listening_progress", "parts_json")]:
        _del(f"{t}({col})",
             f"DELETE FROM {t} WHERE {col} LIKE '%{FAKE_TAG}%'")

    # 4) 薄弱项快照
    _del("weak_snapshots", "DELETE FROM weak_snapshots WHERE item_key LIKE '@%' "
                           "AND d >= date('now','-70 days')")
    conn.commit()
    return {k: v for k, v in removed.items() if v}


def main():
    ap = argparse.ArgumentParser(description="生成 8 周模拟学习数据（压测周报/月报/薄弱项）")
    ap.add_argument("--db", default=os.environ.get("EOS_DB") or DEFAULT_DB,
                    help=f"目标数据库文件（默认 {DEFAULT_DB}）")
    ap.add_argument("--scale", type=int, default=1, help="数据量倍数，默认 1")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，默认 42（可复现）")
    ap.add_argument("--clean", action="store_true", help="只删除假数据，不生成")
    ap.add_argument("--force", action="store_true", help="目标库非空时也继续（危险）")
    args = ap.parse_args()

    is_pg = str(args.db).startswith("postgres")
    if is_pg:
        print("❌ 拒绝写入 PostgreSQL。")
        print("   本脚本只操作本地 SQLite 文件，避免污染线上数据。")
        print("   想测线上请导出一份副本到本地再跑。")
        return 1

    fresh = not os.path.exists(args.db)
    print(f"{'新建' if fresh else '打开'} SQLite: {args.db}")

    # backend/ 里的 db.py 是建表入口，必须复用它才能保证表结构与线上一致
    _here = os.path.dirname(os.path.abspath(__file__))
    for cand in [_here, os.path.join(_here, "backend"),
                 os.path.join(_here, "..", "..", "..", "tmp", "pushtest", "backend")]:
        if os.path.exists(os.path.join(cand, "db.py")):
            sys.path.insert(0, cand)
            break
    else:
        print(f"❌ 找不到 backend/db.py。请把本脚本放到 backend/ 下，"
              f"或用 --db 指定库后手动建表。")
        return 1

    os.environ["EOS_DB"] = args.db
    import db as _db
    _db.init_db()
    conn = _db.get_conn()

    if args.clean:
        r = clean(conn)
        print("\n已删除假数据：")
        for k, v in r.items():
            print(f"  {k}: {v} 行")
        if not r:
            print("  （没找到假数据，库可能已经是干净的）")
        conn.close()
        return 0

    # 安全检查：库里已有不少真实数据时，默认拒绝
    if not fresh and not args.force:
        try:
            s = conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0]
            e = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
            if s + e > 0:
                fake_s = conn.execute(
                    "SELECT COUNT(*) FROM sentences WHERE word LIKE 'fk%'").fetchone()[0]
                real = (s - fake_s) + e
                if real > 50:
                    print(f"\n⚠️  这个库里已有 {real} 条看起来像真实数据的记录。")
                    print("   为了不污染，默认拒绝写入。")
                    print("   确认是测试库就加 --force；换个库就加 --db /path/to.db")
                    conn.close()
                    return 1
        except Exception:
            pass

    t0 = datetime.now()
    n = build(conn, args.scale, args.seed)
    el = (datetime.now() - t0).total_seconds()
    size = os.path.getsize(args.db) / 1024 / 1024

    print(f"\n✅ 已生成 8 周（{DAYS} 天）模拟数据，规模 ×{args.scale}，耗时 {el:.1f}s")
    print(f"   数据库大小: {size:.1f} MB")
    print("\n各表行数：")
    for k in sorted(n):
        print(f"   {k:14} {n[k]:>7,}")

    print("\n数据趋势（用来验证周报/月报算得对不对）：")
    for w in range(1, WEEKS + 1):
        row = conn.execute(
            "SELECT COUNT(*) c, AVG(score) a, SUM(good) g FROM sentences "
            "WHERE week=? AND word LIKE 'fk%'", (w,)).fetchone()
        c = row[0] or 0
        a = row[1] or 0
        g = row[2] or 0
        acc = (g * 100 // c) if c else 0
        print(f"   第{w}周  造句 {c:>4} 条   均分 {a:5.1f}   正确率 {acc:>3}%")

    print("\n清理命令：")
    print(f"   python3 {os.path.basename(__file__)} --db {args.db} --clean")
    print(f"   # 或者直接删文件： rm -f {args.db}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
