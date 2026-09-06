# -*- coding: utf-8 -*-
"""总结页「周报 / 月报」真实统计。

背景：前端 pages.sum 一直请求 /api/report，但这个路由从未实现（线上 404），
导致周报/月报的核心指标（造句、复习、周测、听力、错误）全部显示 —。
本模块补上这个接口，全部数据来自现有表，零编造。

口径约定：
  * 本周  = 当前学习进度所在的「课程周」（progress.stage + progress.week）。
            sentences / reviews / quizzes / listening_progress 都有 stage+week 字段，
            可直接按课程周过滤；weeks 表提供周主题与语法重点。
  * 本月  = 自然月，按 created_at 的 'YYYY-MM' 前缀过滤。
            用参数化 LIKE 而不是 strftime()，因为 strftime 在 PostgreSQL 上不存在，
            而本项目 SQLite / Neon(PG) 双兼容。
  * 算不出的指标一律返回 None（前端显示 —），绝不填 0 或编数字。
"""
from datetime import date, timedelta

from db import get_conn
import services as svc


# ---------------- 小工具 ----------------
def _i(v):
    """聚合结果安全转 int；None → 0。"""
    try:
        return int(v or 0)
    except Exception:
        return 0


def _avg(v):
    """均分：无记录返回 None（前端显示 —），有记录则四舍五入成整数。"""
    if v is None:
        return None
    try:
        return round(float(v))
    except Exception:
        return None


def _ym():
    """当前自然月 'YYYY-MM'。"""
    return date.today().isoformat()[:7]


def _days_ago(n):
    """n 天前的日期 'YYYY-MM-DD'，可与 created_at 直接做字符串比较
    （created_at 为 ISO 格式，字典序即时间序，SQLite 与 PG 通用）。"""
    return (date.today() - timedelta(days=n)).isoformat()


def _prev_month_ym():
    """上一个自然月 'YYYY-MM'。"""
    y, m = date.today().year, date.today().month
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def _demo_filter():
    """示例数据（is_demo=1）不参与任何真实统计。

    返回一段可直接拼进 WHERE 的 SQL；老库还没有 is_demo 列时返回空串，
    保证统计不会因为缺列而崩掉。
    """
    try:
        conn = get_conn()
        try:
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(errors)")}
            except Exception:
                cols = {r[0] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'errors'")}
        finally:
            conn.close()
        return " AND (is_demo IS NULL OR is_demo = 0)" if "is_demo" in cols else ""
    except Exception:
        return ""


# ---------------- 环比四项（本周vs上周 / 本月vs上月）----------------
def _week_metrics(conn, stage, week):
    """某一课程周的四项核心指标：学习天数 / 新学词汇 / 单词复习 / 错误率。"""
    if week is None or week < 1:
        return None
    d = conn.execute(
        "SELECT COUNT(DISTINCT date) AS n FROM history WHERE stage=? AND week=?",
        (stage, week)).fetchone()
    w = conn.execute(
        "SELECT COUNT(*) AS n FROM word_output WHERE stage=? AND week=?",
        (stage, week)).fetchone()
    r = conn.execute(
        "SELECT SUM(total_correct) AS c, SUM(total_wrong) AS w FROM reviews "
        "WHERE stage=? AND week=?", (stage, week)).fetchone()
    s = conn.execute(
        "SELECT COUNT(*) AS n, SUM(good) AS good FROM sentences "
        "WHERE stage=? AND week=?", (stage, week)).fetchone()
    total, good = _i(s["n"]), _i(s["good"])
    return {
        "days": _i(d["n"]),
        "new_words": _i(w["n"]),
        "reviews": _i(r["c"]) + _i(r["w"]),
        "answers": total,
        # 错误率口径统一为「错句数 / 有效作答数」；没有作答时返回 None（前端显示 —）
        "err_rate": round((total - good) / total * 100) if total else None,
    }


def _month_metrics(conn, ym):
    """某一自然月的四项核心指标，口径与周一致（便于直接环比）。"""
    like = ym + "%"
    d = conn.execute(
        "SELECT COUNT(DISTINCT date) AS n FROM history WHERE date LIKE ?",
        (like,)).fetchone()
    w = conn.execute(
        "SELECT COUNT(*) AS n FROM word_output WHERE first_at LIKE ?",
        (like,)).fetchone()
    r = conn.execute(
        "SELECT SUM(total_correct) AS c, SUM(total_wrong) AS w FROM reviews "
        "WHERE created_at LIKE ?", (like,)).fetchone()
    s = conn.execute(
        "SELECT COUNT(*) AS n, SUM(good) AS good FROM sentences "
        "WHERE created_at LIKE ?", (like,)).fetchone()
    total, good = _i(s["n"]), _i(s["good"])
    return {
        "days": _i(d["n"]),
        "new_words": _i(w["n"]),
        "reviews": _i(r["c"]) + _i(r["w"]),
        "answers": total,
        "err_rate": round((total - good) / total * 100) if total else None,
    }


def _cmp_item(name, cur, prev, unit, higher_is_better):
    """一条环比。

    better: True=向好(绿) / False=向差(红) / None=持平或没有可比数据(灰)。
    错误率是「越低越好」，所以 higher_is_better=False。
    """
    if cur is None or prev is None:
        return {"name": name, "cur": cur, "prev": prev, "unit": unit,
                "pct": None, "better": None}
    diff = cur - prev
    if diff == 0:
        better = None
    else:
        better = (diff > 0) if higher_is_better else (diff < 0)
    pct = round(diff / prev * 100) if prev else None
    return {"name": name, "cur": cur, "prev": prev, "unit": unit,
            "pct": pct, "better": better}


def _compare_block(cur_m, prev_m, title, prev_label):
    """把两周/两月的四项指标拼成前端直接渲染的对比块。"""
    def g(m, k):
        return (m or {}).get(k)
    return {
        "title": title,
        "prev_label": prev_label,
        "items": [
            _cmp_item("学习天数", g(cur_m, "days"), g(prev_m, "days"), "天", True),
            _cmp_item("新学词汇", g(cur_m, "new_words"), g(prev_m, "new_words"), "个", True),
            _cmp_item("单词复习", g(cur_m, "reviews"), g(prev_m, "reviews"), "次", True),
            _cmp_item("错误率", g(cur_m, "err_rate"), g(prev_m, "err_rate"), "%", False),
        ],
    }


def _trend_8w(conn, demo_sql):
    """近 8 周错误趋势：每周「错误次数 / 薄弱项数」，better=与该周前一周相比。

    绿=比前一周少（向好），红=比前一周多（向差），持平或首周为 null（灰）。
    """
    today = date.today()
    out = []
    for i in range(7, -1, -1):
        end = today - timedelta(days=7 * i)
        start = end - timedelta(days=7)
        erow = conn.execute(
            "SELECT COALESCE(SUM(times), 0) AS n FROM errors "
            "WHERE last_at >= ? AND last_at < ?" + demo_sql,
            (start.isoformat(), end.isoformat())).fetchone()
        wrow = conn.execute(
            "SELECT COUNT(DISTINCT error_type) AS n FROM errors "
            "WHERE level = '🟡' AND last_at >= ? AND last_at < ?" + demo_sql,
            (start.isoformat(), end.isoformat())).fetchone()
        out.append({
            "label": "本周" if i == 0 else f"{start.month}/{start.day}",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "errors": _i(erow["n"]),
            "weak": _i(wrow["n"]),
        })
    for idx, it in enumerate(out):
        if idx == 0:
            it["better"] = None
        else:
            prev = out[idx - 1]["errors"]
            it["better"] = (it["errors"] < prev) if it["errors"] != prev else None
    return out


# ---------------- 主入口 ----------------
def build_report():
    conn = get_conn()
    try:
        p = svc.get_progress() or {}
        stage = _i(p.get("stage"))
        week = _i(p.get("week")) or 1

        # ===== 本周（课程周）=====
        wrow = conn.execute(
            "SELECT title, grammar FROM weeks WHERE stage=? AND week_no=?",
            (stage, week)).fetchone()
        title = (wrow["title"] if wrow else "") or ""
        grammar = (wrow["grammar"] if wrow else "") or ""

        srow = conn.execute(
            "SELECT COUNT(*) AS n, SUM(good) AS good, AVG(score) AS avg "
            "FROM sentences WHERE stage=? AND week=?", (stage, week)).fetchone()

        # SRS 复习次数：reviews 表没有每次复习的历史行，只有累计的
        # total_correct / total_wrong，因此取本周词汇上的累计复习作答次数。
        # （history 表只记 'learn_vocab' 背词动作，不含复习，无法用于此项。）
        rrow = conn.execute(
            "SELECT SUM(total_correct) AS c, SUM(total_wrong) AS w "
            "FROM reviews WHERE stage=? AND week=?", (stage, week)).fetchone()

        qrow = conn.execute(
            "SELECT COUNT(*) AS n, AVG(score) AS avg FROM quizzes "
            "WHERE stage=? AND week=?", (stage, week)).fetchone()

        lrow = conn.execute(
            "SELECT SUM(listening_total) AS t, SUM(listening_done) AS d "
            "FROM listening_progress WHERE stage=? AND week=?",
            (stage, week)).fetchone()

        demo_sql = _demo_filter()

        # 本周高频错误类型：errors 表没有 stage/week 字段，按近 7 天自然日统计
        # （is_demo=1 的示例数据不计入，避免污染真实画像）
        err_types = []
        for r in conn.execute(
                "SELECT error_type AS t, SUM(times) AS n FROM errors "
                "WHERE created_at >= ?" + demo_sql + " GROUP BY error_type "
                "ORDER BY n DESC LIMIT 5", (_days_ago(7),)).fetchall():
            err_types.append({"type": r["t"], "current": _i(r["n"])})

        # 🆚 本周 vs 上周：第 1 周没有「上周」，prev 全为 None，前端显示 —
        cur_w = _week_metrics(conn, stage, week)
        prev_w = _week_metrics(conn, stage, week - 1) if week > 1 else None
        week_cmp = _compare_block(cur_w, prev_w, "🆚 本周 vs 上周", "上周")

        week_out = {
            "title": title,
            "grammar": grammar,
            "sentences": {
                "total": _i(srow["n"]),
                "good": _i(srow["good"]),
                "avg": _avg(srow["avg"]),
            },
            # listening_done = 答对数，listening_total = 总题数（同 /api/activity 口径）
            "reviews": _i(rrow["c"]) + _i(rrow["w"]),
            "quiz_count": _i(qrow["n"]),
            "quiz_avg": _avg(qrow["avg"]),
            "listening": {"answered": _i(lrow["t"]), "correct": _i(lrow["d"])},
            "err_types": err_types,
            "compare": week_cmp,
            "trend_8w": _trend_8w(conn, demo_sql),
        }

        # ===== 本月（自然月）=====
        like = _ym() + "%"

        msrow = conn.execute(
            "SELECT COUNT(*) AS n, SUM(good) AS good, AVG(score) AS avg "
            "FROM sentences WHERE created_at LIKE ?", (like,)).fetchone()
        mlrow = conn.execute(
            "SELECT SUM(listening_total) AS t, SUM(listening_done) AS d "
            "FROM listening_progress WHERE created_at LIKE ?", (like,)).fetchone()
        mqrow = conn.execute(
            "SELECT COUNT(*) AS n FROM quizzes WHERE created_at LIKE ?",
            (like,)).fetchone()
        # 新学词汇：以 word_output 首次主动输出时间落入本月计
        mwrow = conn.execute(
            "SELECT COUNT(*) AS n FROM word_output WHERE first_at LIKE ?",
            (like,)).fetchone()
        menew = conn.execute(
            "SELECT COUNT(*) AS n FROM errors WHERE created_at LIKE ?" + demo_sql,
            (like,)).fetchone()
        mefix = conn.execute(
            "SELECT COUNT(*) AS n FROM errors WHERE fixed=1 AND fixed_at LIKE ?"
            + demo_sql, (like,)).fetchone()

        # 🆚 本月 vs 上月（自然月，第 1 个月同样只显示 —，不编数字）
        cur_m = _month_metrics(conn, _ym())
        prev_m = _month_metrics(conn, _prev_month_ym())
        month_cmp = _compare_block(cur_m, prev_m, "🆚 本月 vs 上月", "上月")

        month_out = {
            "label": _ym(),
            "sentences": {
                "total": _i(msrow["n"]),
                "good": _i(msrow["good"]),
                "avg": _avg(msrow["avg"]),
            },
            "listening": {"answered": _i(mlrow["t"]), "correct": _i(mlrow["d"])},
            "quiz_count": _i(mqrow["n"]),
            "words_output": _i(mwrow["n"]),
            "errors": {"new": _i(menew["n"]), "fixed": _i(mefix["n"])},
            "compare": month_cmp,
            "trend_8w": _trend_8w(conn, demo_sql),
        }

        return {
            "ok": True,
            "progress": {"stage": stage, "week": week},
            "week": week_out,
            "month": month_out,
            "trend_8w": _trend_8w(conn, demo_sql),
        }
    finally:
        conn.close()
