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

        # 本周高频错误类型：errors 表没有 stage/week 字段，按近 7 天自然日统计
        err_types = []
        for r in conn.execute(
                "SELECT error_type AS t, SUM(times) AS n FROM errors "
                "WHERE created_at >= ? GROUP BY error_type "
                "ORDER BY n DESC LIMIT 5", (_days_ago(7),)).fetchall():
            err_types.append({"type": r["t"], "current": _i(r["n"])})

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
            "SELECT COUNT(*) AS n FROM errors WHERE created_at LIKE ?",
            (like,)).fetchone()
        mefix = conn.execute(
            "SELECT COUNT(*) AS n FROM errors WHERE fixed=1 AND fixed_at LIKE ?",
            (like,)).fetchone()

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
        }

        return {
            "ok": True,
            "progress": {"stage": stage, "week": week},
            "week": week_out,
            "month": month_out,
        }
    finally:
        conn.close()
