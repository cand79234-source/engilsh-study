"""SRS 引擎 - SM-2 + Bespoke 风格紧迫度调度。

规则（对齐需求第十八、十九、二十节）：
- 新学的词/句进入复习队列，第2天首查。
- 答对：间隔递增（1→3→7→…→×ease），连续正确逐渐拉长。
- 答错：interval 归零重排为明天（1天），并提前重新进入复习。
- 复习采用主动回忆：给出词，要求用户自己造句，再由判定。
- 每天自动把到期项注入"今日复习"（需求第十七节）。
"""
from datetime import date, datetime, timedelta
from db import get_conn, ts


def _due_date(interval_days):
    return (date.today() + timedelta(days=int(interval_days))).isoformat()


def schedule_review(conn, kind, ref_key, prompt, answer, stage, week, day,
                    existing=None):
    """创建或获取一张复习卡，新卡 interval=1（明天首查）。"""
    cur = conn.execute(
        "SELECT * FROM reviews WHERE kind=? AND ref_key=? AND prompt=?",
        (kind, ref_key, prompt))
    row = cur.fetchone()
    if row:
        return row["id"], row
    cur = conn.execute(
        "INSERT INTO reviews (kind, ref_key, prompt, answer, stage, week, day,"
        " interval, reps, next_due, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (kind, ref_key, prompt, answer, stage, week, day, 1, 0,
         _due_date(1), ts()))
    conn.commit()
    return cur.lastrowid, None


def submit_review(review_id, correct, quality=None):
    """处理一次复习结果。correct: bool。quality 0=错 1=对。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if not row:
        conn.close()
        return None
    ease = row["ease"] or 2.5
    reps = row["reps"] or 0
    interval = row["interval"] or 0
    q = 1 if correct else 0

    if not correct:
        # 答错：间隔重排为1天，提前重新进入复习
        reps = 0
        interval = 1
        # ease 下调
        ease = max(1.3, ease - 0.2)
        total_wrong = row["total_wrong"] + 1
    else:
        # 答对：SM-2 间隔递增
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 3
        elif reps == 2:
            interval = 7
        else:
            interval = round(interval * ease)
        reps += 1
        ease = min(3.0, ease + 0.1)
        total_wrong = row["total_wrong"]
    total_correct = row["total_correct"] + (1 if correct else 0)

    next_due = _due_date(interval)
    conn.execute(
        "UPDATE reviews SET ease=?, interval=?, reps=?, next_due=?, last_score=?,"
        " total_correct=?, total_wrong=?, last_reviewed=? WHERE id=?",
        (ease, interval, reps, next_due, q, total_correct, total_wrong,
         datetime.now().isoformat(timespec="seconds"), review_id))
    conn.commit()
    result = {
        "id": review_id, "correct": correct, "new_interval": interval,
        "next_due": next_due, "reps": reps, "ease": ease,
        "kind": row["kind"], "ref_key": row["ref_key"],
    }
    conn.close()
    return result


def due_reviews(limit=50, all_types=True, kind=None):
    """今日复习 = 到期卡(next_due<=today) + 今天新学未复习卡(last_score=-1 且当天创建)。

    kind 是三状态隔离的关键闸门：
      - kind='vocab'     → 只有「今日复习闪卡」读（单词词义 SRS）
      - kind='listening' → 只有听力页读（听觉识别 SRS）
      - kind=None（默认）→ 全部，保持原有行为不变（零回归）

    不隔离的后果：听力卡会混进单词闪卡、错误卡会混进复习队列，
    导致「词义 SRS / 主动输出五星 / 听力状态」三个维度互相污染。
    """
    conn = get_conn()
    today = date.today().isoformat()
    # 参数顺序必须与 SQL 中 ? 的出现顺序一致：
    #   WHERE: next_due<=?, created_at>=?, [kind=?]  → ORDER BY: created_at>=?  → LIMIT ?
    params = [today, today + "T00:00:00"]
    kind_sql = ""
    if kind:
        kind_sql = " AND kind = ?"
        params.append(kind)
    params.append(today + "T00:00:00")
    params.append(limit)
    rows = conn.execute(
        "SELECT * FROM reviews WHERE (next_due <= ? OR (created_at >= ? AND last_score = -1))"
        + kind_sql +
        " ORDER BY (created_at >= ?) DESC, next_due, id LIMIT ?",
        params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def due_vocab_reviews(limit=50):
    """今日复习闪卡专用：只取单词词义 SRS 卡（kind='vocab'）。

    绝不返回 error 卡（错误归「薄弱项」）或 listening 卡（归听力页）。
    """
    return due_reviews(limit=limit, kind="vocab")


def due_summary():
    """按类型统计今日复习数量，供主页显示。"""
    conn = get_conn()
    today = date.today().isoformat()
    rows = conn.execute(
        "SELECT kind, COUNT(*) n FROM reviews WHERE (next_due <= ? OR (created_at >= ? AND last_score = -1))"
        " GROUP BY kind",
        (today, today + "T00:00:00")).fetchall()
    conn.close()
    return {r["kind"]: r["n"] for r in rows}


def _priority_score(row, today=None):
    """复习紧迫度：错误率高 + 久未复习 优先。

    score = 错误率*2.0(权重最高) + 陈旧度(0~1)
    - 错误率 = total_wrong / max(1, total_correct + total_wrong)
    - 陈旧度 = min(距上次复习天数, 30) / 30；从未复习过则按创建时间算
    """
    today = today or date.today()
    tw = row.get("total_wrong") or 0
    tc = row.get("total_correct") or 0
    err_rate = tw / max(1, tc + tw)

    last = (row.get("last_reviewed") or row.get("created_at") or "")[:10]
    stale = 0.0
    if last:
        try:
            d = date.fromisoformat(last)
            stale = min(max((today - d).days, 0), 30) / 30.0
        except ValueError:
            stale = 0.0
    # 从未答过的卡(last_score=-1)等同最陈旧，额外加权让它优先露面
    if (row.get("last_score") or -1) == -1:
        stale = max(stale, 0.8)
    return err_rate * 2.0 + stale


def due_vocab_words(limit=None):
    """返回当天到期的 vocab 复习词列表（供造句/组合表达使用）。

    排序：错误率高 + 久未复习 优先（不再单纯按到期时间）。
    每条含 word / meaning / pos / error_rate / priority。
    """
    out = []
    today = date.today()
    for r in due_reviews(limit=200):
        if r.get("kind") != "vocab":
            continue
        word = (r.get("ref_key") or "").strip()
        if not word:
            continue
        # answer 通常存 "word 中文词性"，尝试拆出 meaning
        ans = (r.get("answer") or "").strip()
        meaning = ""
        pos = ""
        if ans:
            parts = ans.split(None, 2)
            if len(parts) >= 2 and parts[0].lower() == word.lower():
                meaning = parts[1]
            else:
                meaning = ans
        tw = r.get("total_wrong") or 0
        tc = r.get("total_correct") or 0
        out.append({
            "word": word, "meaning": meaning, "pos": pos,
            "error_rate": round(tw / max(1, tc + tw), 3),
            "total_wrong": tw, "total_correct": tc,
            "last_reviewed": (r.get("last_reviewed") or "")[:10],
            "priority": round(_priority_score(r, today), 3),
        })
    out.sort(key=lambda x: -x["priority"])
    if limit:
        out = out[:limit]
    return out
