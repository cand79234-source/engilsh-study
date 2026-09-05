# -*- coding: utf-8 -*-
"""板块之间的数据打通。

问题
----
八个板块各存各的表，只有「造句」这一条线是活的（错了会自动进错误本）。
其余板块的数据存进去就再没人读过：

  * 周测：`quizzes.detail_json` 完整记着每道题的题干、知识点标签、对错，
    但除了算个总分之外从未被使用 —— **考砸了没人知道**。
  * 听力：`listening_progress` 只存了分数（答对几题 / 共几题），
    没有存具体哪道题错了，所以无法定位到知识点。
  * 复习：`reviews` 记着每张卡累计对几次错几次，但没喂给薄弱项。
  * 专项训练：此前压根不落库，对其它板块完全隐形。
  * 薄弱项：`/api/weakness` 只看错误本 + 五星输出，是"半瞎"的。

本模块做的事
------------
1. `sync_quiz_errors()`  周测 / 阶段测答错的题 → 自动进错误本（带知识点标签）
2. `build_weakness()`    薄弱项改成综合判定，把造句 / 复习 / 周测 / 听力 /
                         专项训练全部纳入，同时保持原有返回结构不变
3. `word_profile()`      一个词的全息档案：学习 / 造句 / 错误 / 复习 /
                         主动输出 / 专项训练的完整轨迹
4. `training_summary()`  专项训练成绩汇总，让它对总结页可见

原则
----
* **不编造**：查不到就返回 None / 空，绝不填 0 或编数字。
* **不丢数据**：只追加，不覆盖。错误本去重按 (来源, 题号, 周) 累加 times。
* **可重复执行**：同步函数跑多次不会产生重复条目。
"""
import json

from db import get_conn, ts

# 周测错题进错误本时使用的来源标记（错误本靠它区分「自己写的错句」和「选错的题」）
SRC_QUIZ = "周测"


def _row(d):
    """sqlite3.Row / DictRow 统一转 dict。"""
    try:
        return dict(d)
    except Exception:
        return {}


def _scal(conn, sql, args=(), default=0):
    """取单个标量值；查不到或出错返回 default。

    注意：`sqlite3.Row` 只有 keys()、**没有 values()**，而 psycopg2 的 DictRow 有。
    用 r[0] 下标访问是两者都支持的写法 —— 早先用 r.values() 会抛异常并被
    except 吞掉，导致所有聚合查询静默返回 0，听力和训练汇总因此全部失效。
    """
    try:
        r = conn.execute(sql, tuple(args)).fetchone()
        if r is None:
            return default
        v = r[0]
        return default if v is None else v
    except Exception:
        return default


# ---------- 1. 周测错题 → 错误本 ----------
def sync_quiz_errors(stage, week, detail, day=7):
    """把周测 / 阶段测答错的题同步进错误本。

    detail 来自 quizzes.detail_json，形如：
        [{"id":.., "question":.., "user":.., "correct_idx":.., "ok":bool, "tag":..}]

    去重口径：同一 (周, 题号) 的错题只记一条，再次考到同一题仍然答错 → times+1。
    已经改正（fixed=1）的条目重新答错时，会重新打开（fixed 归 0）——
    因为「又错了」说明并没有真正掌握。
    """
    if not detail:
        return 0
    conn = get_conn()
    n = 0
    try:
        for d in detail:
            if not isinstance(d, dict) or d.get("ok"):
                continue
            qid = str(d.get("id") or "").strip()
            tag = str(d.get("tag") or "").strip() or "综合"
            question = str(d.get("question") or "").strip()
            if not question:
                continue
            row = conn.execute(
                "SELECT id FROM errors WHERE source=? AND task_key=? AND stage=? AND week=? "
                "LIMIT 1", (SRC_QUIZ, qid, stage, week)).fetchone()
            if row:
                eid = _row(row).get("id")
                conn.execute(
                    "UPDATE errors SET times=COALESCE(times,1)+1, last_at=?, fixed=0, fixed_at='' "
                    "WHERE id=?", (ts(), eid))
            else:
                conn.execute(
                    "INSERT INTO errors (error_type, original, corrected, explanation, source, "
                    "word, task_key, error_text, sentence_text, times, first_at, last_at, "
                    "fixed, fixed_at, stage, week, day, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,0,'',?,?,?,?)",
                    (tag, question, "", "周测答错 · 知识点：" + tag, SRC_QUIZ,
                     "", qid, question, question,
                     ts(), ts(), stage, week, day, ts()))
            n += 1
        conn.commit()
    except Exception as e:
        print("[link.sync_quiz_errors] 失败（不影响周测成绩）:", e)
    finally:
        conn.close()
    return n


# ---------- 2. 薄弱项：综合判定 ----------
def _weak_from_sentences(conn, limit=5):
    """造句得分低的词。"""
    try:
        rows = conn.execute(
            "SELECT word, COUNT(*) n, AVG(score) avg, MAX(created_at) last "
            "FROM sentences WHERE word IS NOT NULL AND word <> '' "
            "GROUP BY word HAVING n >= 2 AND avg < 75 "
            "ORDER BY avg ASC, n DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    return [{"word": _row(r).get("word"), "count": _row(r).get("n") or 0,
             "avg": round(float(_row(r).get("avg") or 0), 1),
             "last_at": _row(r).get("last")} for r in rows]


def _weak_from_reviews(conn, limit=5):
    """复习里反复答错的卡（按错误率排序）。"""
    try:
        rows = conn.execute(
            "SELECT kind, ref_key, prompt, total_correct, total_wrong "
            "FROM reviews WHERE (total_correct + total_wrong) >= 2 "
            "ORDER BY (total_wrong * 1.0 / (total_correct + total_wrong)) DESC, "
            "total_wrong DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = _row(r)
        c, w = int(d.get("total_correct") or 0), int(d.get("total_wrong") or 0)
        tot = c + w
        out.append({"kind": d.get("kind"), "ref_key": d.get("ref_key"),
                    "prompt": (d.get("prompt") or "")[:60],
                    "correct": c, "wrong": w,
                    "wrong_rate": round(w / tot * 100) if tot else None})
    return [x for x in out if (x["wrong_rate"] or 0) >= 40]


def _weak_from_quizzes(conn, limit=5):
    """没通过的周测及其中答错的知识点。"""
    try:
        rows = conn.execute(
            "SELECT stage, week, score, passed, detail_json, created_at FROM quizzes "
            "ORDER BY id DESC LIMIT 20").fetchall()
    except Exception:
        return []
    failed = []
    tag_hits = {}
    for r in rows:
        d = _row(r)
        if int(d.get("passed") or 0):
            continue
        failed.append({"stage": d.get("stage"), "week": d.get("week"),
                       "score": d.get("score"), "at": d.get("created_at")})
        for q in (json.loads(d.get("detail_json") or "[]") or []):
            if not isinstance(q, dict) or q.get("ok"):
                continue
            t = str(q.get("tag") or "").strip() or "综合"
            tag_hits[t] = tag_hits.get(t, 0) + 1
    tags = [{"type": t, "count": c} for t, c in
            sorted(tag_hits.items(), key=lambda kv: -kv[1])[:limit]]
    return {"failed": failed[:limit], "tags": tags}


def _weak_from_listening(conn):
    """听力正确率。明细没存，所以只能给整体水平，不编造具体错题。"""
    done = _scal(conn, "SELECT SUM(listening_done) FROM listening_progress", default=0)
    total = _scal(conn, "SELECT SUM(listening_total) FROM listening_progress", default=0)
    if not total:
        return None
    return {"answered": int(done or 0), "total": int(total or 0),
            "rate": round(int(done or 0) / int(total) * 100)}


def _weak_from_training(conn, limit=5):
    """专项训练里一直没达标的项目。"""
    try:
        rows = conn.execute(
            "SELECT project_key, COUNT(*) n, "
            "SUM(CASE WHEN final_status='PASS' THEN 1 ELSE 0 END) passed, "
            "SUM(valid_attempts) valid, SUM(correct_count) correct "
            "FROM training_sessions WHERE project_key IS NOT NULL AND project_key <> '' "
            "GROUP BY project_key ORDER BY passed ASC, n DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = _row(r)
        n = int(d.get("n") or 0)
        passed = int(d.get("passed") or 0)
        valid = int(d.get("valid") or 0)
        correct = int(d.get("correct") or 0)
        out.append({"project_id": d.get("project_key"), "sessions": n, "passed": passed,
                    "rate": round(correct / valid * 100) if valid else None})
    return [x for x in out if x["passed"] == 0]


def build_weakness():
    """综合薄弱项。返回结构向后兼容（error_types / low_star_words / recommendations），
    另附 sources 分板块明细。"""
    import services as svc
    import srs

    conn = get_conn()
    try:
        errs = svc.error_breakdown()
    except Exception:
        errs = []
    try:
        low = srs.weak_output_words(threshold=3)
    except Exception:
        low = []

    sents = _weak_from_sentences(conn)
    revs = _weak_from_reviews(conn)
    quiz = _weak_from_quizzes(conn)
    listen = _weak_from_listening(conn)
    train = _weak_from_training(conn)
    conn.close()

    recs = []
    for e in (errs or [])[:3]:
        if (e.get("count_30d") or 0) > 0:
            recs.append({"kind": "error", "label": "错误类型 · " + str(e.get("type")),
                         "detail": f"近30天 {e['count_30d']} 次，累计 {e['total']} 次",
                         "advice": e.get("remedy") or ""})
    for w in (low or [])[:3]:
        recs.append({"kind": "output", "label": "主动输出 · " + str(w.get("word")),
                     "detail": f"五星 {w.get('stars')}/5，最近表现 {w.get('last_result') or '—'}",
                     "advice": "建议用该词再造一句关于你自己的话，把熟练度拉到 3 星以上。"})
    # 新增来源：造句
    for w in sents[:3]:
        recs.append({"kind": "sentence", "label": "造句均分偏低 · " + str(w.get("word")),
                     "detail": f"造了 {w.get('count')} 句，均分 {w.get('avg')}",
                     "advice": "回看这几句的批改说明，重点注意反复被指出的问题。"})
    # 新增来源：复习
    for w in revs[:3]:
        recs.append({"kind": "review", "label": "复习反复出错 · " + str(w.get("ref_key") or w.get("prompt") or "—"),
                     "detail": f"对 {w.get('correct')} 次、错 {w.get('wrong')} 次（错 {w.get('wrong_rate')}%）",
                     "advice": "这张卡总是记不住，建议用它在句子里再造一次。"})
    # 新增来源：周测
    for t in (quiz or {}).get("tags", [])[:3]:
        recs.append({"kind": "quiz", "label": "周测知识点 · " + str(t.get("type")),
                     "detail": f"近期周测错了 {t.get('count')} 次",
                     "advice": "已在错误本里生成对应条目，去错误本按知识点过一遍。"})
    # 新增来源：专项训练
    for t in train[:3]:
        recs.append({"kind": "training", "label": "专项训练未达标 · " + str(t.get("project_id")),
                     "detail": f"练了 {t.get('sessions')} 次仍未通过" +
                               (f"，正确率 {t.get('rate')}%" if t.get("rate") is not None else ""),
                     "advice": "换个角度重新练，或先回错误本把基础知识点补上。"})

    return {
        # 向后兼容：前端原有的三个字段一个不少
        "error_types": errs,
        "low_star_words": low,
        "recommendations": recs,
        # 新增：分板块明细，供总结页 / 后续页面使用
        "sources": {
            "sentences": sents,
            "reviews": revs,
            "quizzes": quiz,
            "listening": listen,
            "training": train,
        },
    }


# ---------- 3. 词全息档案 ----------
def word_profile(word):
    """一个词在系统里留下的全部痕迹。

    返回 None 表示查不到任何相关记录（前端按「还没接触过」处理）。
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    conn = get_conn()
    try:
        # 词典基本信息
        drow = conn.execute(
            "SELECT phonetic, meaning, pos, tag FROM dictionary WHERE word=? LIMIT 1",
            (w,)).fetchone()
        info = _row(drow) if drow else {}

        # 学习记录
        lrow = conn.execute(
            "SELECT stage, week, day, mastered, created_at FROM day_items "
            "WHERE LOWER(ref_key)=? ORDER BY id DESC LIMIT 1", (w,)).fetchone()
        learned = _row(lrow) if lrow else None

        # 造句
        try:
            srows = conn.execute(
                "SELECT original, corrected, score, verdict, good, created_at "
                "FROM sentences WHERE LOWER(word)=? ORDER BY id DESC LIMIT 20", (w,)).fetchall()
        except Exception:
            srows = []
        sents = [_row(r) for r in srows]
        s_avg = round(sum(int(r.get("score") or 0) for r in sents) / len(sents), 1) if sents else None

        # 错误
        try:
            erows = conn.execute(
                "SELECT error_type, original, corrected, explanation, source, times, "
                "fixed, first_at, last_at FROM errors WHERE LOWER(word)=? "
                "ORDER BY id DESC LIMIT 20", (w,)).fetchall()
        except Exception:
            erows = []
        errs = [_row(r) for r in erows]

        # 复习
        try:
            rrow = conn.execute(
                "SELECT reps, total_correct, total_wrong, next_due, last_score, last_reviewed "
                "FROM reviews WHERE LOWER(ref_key)=? ORDER BY id DESC LIMIT 1", (w,)).fetchone()
        except Exception:
            rrow = None
        rev = _row(rrow) if rrow else None

        # 主动输出
        try:
            orow = conn.execute(
                "SELECT stars, total_attempts, last_result, last_score, first_at, last_at "
                "FROM word_output WHERE LOWER(word)=? LIMIT 1", (w,)).fetchone()
        except Exception:
            orow = None
        outp = _row(orow) if orow else None

        # 专项训练
        try:
            trows = conn.execute(
                "SELECT session_id, is_correct, used_hint, question_id, created_at "
                "FROM training_attempts WHERE LOWER(word)=? ORDER BY id DESC LIMIT 20",
                (w,)).fetchall()
        except Exception:
            trows = []
        trains = [_row(r) for r in trows]
    finally:
        conn.close()

    has_any = any([info, learned, sents, errs, rev, outp, trains])
    if not has_any:
        return None

    t_correct = sum(1 for r in trains if r.get("is_correct"))
    return {
        "ok": True,
        "word": w,
        "dict": {"phonetic": info.get("phonetic") or "", "meaning": info.get("meaning") or "",
                 "pos": info.get("pos") or "", "tag": info.get("tag") or ""} if info else None,
        "learned": learned,
        "sentences": {
            "count": len(sents),
            "good": sum(1 for r in sents if r.get("good")),
            "avg": s_avg,
            "latest": [{"text": r.get("original"), "corrected": r.get("corrected"),
                        "score": r.get("score"), "verdict": r.get("verdict"),
                        "at": r.get("created_at")} for r in sents[:5]],
        },
        "errors": {
            "count": len(errs),
            "times": sum(int(r.get("times") or 1) for r in errs),
            "fixed": sum(1 for r in errs if r.get("fixed")),
            "types": sorted({r.get("error_type") for r in errs if r.get("error_type")}),
            "latest": [{"type": r.get("error_type"), "original": r.get("original"),
                        "corrected": r.get("corrected"), "explanation": r.get("explanation"),
                        "source": r.get("source"), "fixed": r.get("fixed"),
                        "at": r.get("last_at") or r.get("first_at")} for r in errs[:5]],
        },
        "reviews": rev,
        "output": outp,
        "training": {
            "count": len(trains),
            "correct": t_correct,
            "rate": round(t_correct / len(trains) * 100) if trains else None,
        } if trains else None,
    }


# ---------- 4. 专项训练汇总 ----------
def training_summary():
    """专项训练整体成绩，让总结页能看见这块数据（此前完全隐形）。"""
    conn = get_conn()
    try:
        n_proj = _scal(conn, "SELECT COUNT(*) FROM training_projects "
                             "WHERE project_key IS NOT NULL AND project_key <> ''", default=0)
        n_sess = _scal(conn, "SELECT COUNT(*) FROM training_sessions", default=0)
        passed = _scal(conn, "SELECT COUNT(*) FROM training_sessions WHERE final_status='PASS'",
                       default=0)
        valid = _scal(conn, "SELECT SUM(valid_attempts) FROM training_sessions", default=0)
        correct = _scal(conn, "SELECT SUM(correct_count) FROM training_sessions", default=0)
        indep = _scal(conn, "SELECT SUM(independent_correct_count) FROM training_sessions",
                      default=0)
        n_att = _scal(conn, "SELECT COUNT(*) FROM training_attempts "
                            "WHERE attempt_id IS NOT NULL AND attempt_id <> ''", default=0)
    finally:
        conn.close()
    if not n_sess and not n_att:
        return None
    return {
        "projects": int(n_proj or 0),
        "sessions": int(n_sess or 0),
        "passed": int(passed or 0),
        "valid_attempts": int(valid or 0),
        "correct": int(correct or 0),
        "independent_correct": int(indep or 0),
        "attempts": int(n_att or 0),
        "rate": round(int(correct or 0) / int(valid) * 100) if valid else None,
    }
