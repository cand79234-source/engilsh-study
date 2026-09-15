# -*- coding: utf-8 -*-
"""总结页「周报 / 月报」真实统计。

背景：前端 pages.sum 一直请求 /api/report，但这个路由从未实现（线上 404），
导致周报/月报的核心指标（造句、复习、周测、听力、错误）全部显示 —。
本模块补上这个接口，全部数据来自现有表，零编造。

口径约定：
  * 本周  = **自然周**（周一 00:00:00 → 周日 23:59:59，Asia/Shanghai）。
            这是页面上写「本周」的所有数字的统一口径：造句数 / 均分 / 周测 /
            高频错误，全部落在本自然周内。
            修复前用的是「今天往前 7×24 小时」（_days_ago(7)），跨周时会把
            上周数据算进本周、也会漏掉本周一，统计边界是错的。
  * 本周 vs 上周（环比）：用**课程周**（progress.stage + progress.week）——
            课程周是学习进度的单位（第 N 周），与自然周不是一回事，
            环比就该按课程周来，这里明确区分并在返回体里标注口径。
  * 本月  = 自然月，按 created_at 的 'YYYY-MM' 前缀过滤。
            用参数化 LIKE 而不是 strftime()，因为 strftime 在 PostgreSQL 上不存在，
            而本项目 SQLite / Neon(PG) 双兼容。
  * 算不出的指标一律返回 None（前端显示 —），绝不填 0 或编数字。
"""
from datetime import date, timedelta
import calendar

from db import get_conn, app_today, ensure_plan_goals, china_week_range
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
    return app_today().isoformat()[:7]


def _days_ago(n):
    """n 天前的日期 'YYYY-MM-DD'，可与 created_at 直接做字符串比较
    （created_at 为 ISO 格式，字典序即时间序，SQLite 与 PG 通用）。

    ⚠️ 只用于「近 N 天」这种**滚动窗口**语义（如错误趋势）。
    **不要**再用它冒充「本周」——本周请用 china_week_range()（自然周）。
    """
    return (app_today() - timedelta(days=n)).isoformat()


def _prev_month_ym():
    """上一个自然月 'YYYY-MM'。"""
    y, m = app_today().year, app_today().month
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


# ---------------- 主入口 ----------------
def build_report():
    conn = get_conn()
    try:
        p = svc.get_progress() or {}
        stage = _i(p.get("stage"))
        week = _i(p.get("week")) or 1

        # ===== 本周（自然周：周一 00:00:00 → 周日 23:59:59，Asia/Shanghai）=====
        # 页面上写「本周」的数字全部用这个范围，不再用「今天往前 7 天」。
        wk_start, wk_end = china_week_range()
        wrow = conn.execute(
            "SELECT title, grammar FROM weeks WHERE stage=? AND week_no=?",
            (stage, week)).fetchone()
        title = (wrow["title"] if wrow else "") or ""
        grammar = (wrow["grammar"] if wrow else "") or ""

        srow = conn.execute(
            "SELECT COUNT(*) AS n, SUM(good) AS good, AVG(score) AS avg "
            "FROM sentences WHERE created_at >= ? AND created_at <= ?",
            (wk_start, wk_end)).fetchone()

        # SRS 复习次数：reviews 表没有每次复习的历史行，只有累计的
        # total_correct / total_wrong，因此取本周词汇上的累计复习作答次数。
        # （history 表只记 'learn_vocab' 背词动作，不含复习，无法用于此项。）
        rrow = conn.execute(
            "SELECT SUM(total_correct) AS c, SUM(total_wrong) AS w "
            "FROM reviews WHERE stage=? AND week=?", (stage, week)).fetchone()

        qrow = conn.execute(
            "SELECT COUNT(*) AS n, AVG(score) AS avg FROM quizzes "
            "WHERE created_at >= ? AND created_at <= ?",
            (wk_start, wk_end)).fetchone()

        lrow = conn.execute(
            "SELECT SUM(listening_total) AS t, SUM(listening_done) AS d "
            "FROM listening_progress WHERE stage=? AND week=?",
            (stage, week)).fetchone()

        demo_sql = _demo_filter()

        # 本周高频错误类型：按**自然周**统计（is_demo=1 的示例数据不计入，避免污染真实画像）
        err_types = []
        for r in conn.execute(
                "SELECT error_type AS t, SUM(times) AS n FROM errors "
                "WHERE created_at >= ? AND created_at <= ?" + demo_sql
                + " GROUP BY error_type ORDER BY n DESC LIMIT 5",
                (wk_start, wk_end)).fetchall():
            err_types.append({"type": r["t"], "current": _i(r["n"])})

        # 🆚 本周 vs 上周：这是**课程周**（stage+week）环比，与上面的自然周指标口径不同，
        # 在返回体 period 里明确标注，避免被误解成同一口径。
        # 第 1 周没有「上周」，prev 全为 None，前端显示 —。
        cur_w = _week_metrics(conn, stage, week)
        prev_w = _week_metrics(conn, stage, week - 1) if week > 1 else None
        week_cmp = _compare_block(cur_w, prev_w, "🆚 本周 vs 上周", "上周")

        srecs = conn.execute(
            "SELECT original, corrected, good, score, verdict, error_type, created_at "
            "FROM sentences WHERE created_at >= ? AND created_at <= ?"
            " ORDER BY created_at DESC LIMIT 50",
            (wk_start, wk_end)).fetchall()
        sentence_list = [{
            "original": (r["original"] or "")[:500],
            "corrected": (r["corrected"] or "")[:500],
            "good": _i(r["good"]),
            "score": _avg(r["score"]),
            "verdict": (r["verdict"] or "")[:10],
            "error_type": (r["error_type"] or "")[:20],
            "created_at": (r["created_at"] or "")[:16],
        } for r in srecs]

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
            "sentence_list": sentence_list,
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
        }

        # ===== 计划完成情况（周目标 + 月目标 = 周目标 × 当月周数）=====
        ensure_plan_goals(conn)
        grow = conn.execute(
            "SELECT vocab, sentence, listen FROM plan_goals WHERE scope='week'").fetchone()
        gv = _i(grow["vocab"]) if grow else 0
        gs = _i(grow["sentence"]) if grow else 0
        gl = _i(grow["listen"]) if grow else 0
        ym_now = _ym()
        dim = calendar.monthrange(int(ym_now[:4]), int(ym_now[5:7]))[1]
        weeks_in_month = (dim - 1) // 7 + 1
        mv, ms, ml = gv * weeks_in_month, gs * weeks_in_month, gl * weeks_in_month
        plan_progress = {
            "week": {
                "vocab": [_i(cur_w["new_words"]) if cur_w else 0, gv],
                "sentence": [week_out["sentences"]["total"], gs],
                "listen": [week_out["listening"]["answered"], gl],
            },
            "month": {
                "vocab": [month_out["words_output"], mv],
                "sentence": [month_out["sentences"]["total"], ms],
                "listen": [month_out["listening"]["answered"], ml],
            },
        }

        return {
            "ok": True,
            "progress": {"stage": stage, "week": week},
            # 口径元数据：前端可据此显示「本周（9/14–9/20）」这类真实边界，
            # 也方便排查「数字对不上」时确认各自统计口径。
            "period": {
                "timezone": "Asia/Shanghai",
                "week": {"start": wk_start, "end": wk_end,
                         "label": "%s ~ %s" % (wk_start[:10], wk_end[:10]),
                         "rule": "natural_week_mon_sun"},
                "month": {"label": _ym(), "rule": "natural_month"},
                "week_compare_rule": "course_week(stage+week)",
            },
            "week": week_out,
            "month": month_out,
            "plan_progress": plan_progress,
        }
    finally:
        conn.close()
