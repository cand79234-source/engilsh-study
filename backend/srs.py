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
    """处理一次复习结果。

    quality 三档（② 闪卡「忘记 / 模糊 / 记得」）：
      0 = 忘记 → 间隔重排为明天、reps 清零、ease 下调、记一次错
      3 = 模糊 → 算答对但间隔折半（记得不牢 → 更快再见）、reps+1、ease 微降
      5 = 记得 → SM-2 正常递增（1→3→7→×ease）、reps+1、ease 微升

    quality=None 时回退为旧的二值逻辑（correct 布尔），保证老调用零回归。
    """
    conn = get_conn()
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    if not row:
        conn.close()
        return None
    ease = row["ease"] or 2.5
    reps = row["reps"] or 0
    interval = row["interval"] or 0

    # 三档映射；quality 缺失时回退二值判定
    if quality is not None:
        quality = int(quality)
        if quality <= 0:
            mode, q = "again", 0        # 忘记
        elif quality < 5:
            mode, q = "hard", 3         # 模糊
        else:
            mode, q = "good", 5         # 记得
    else:
        mode = "good" if correct else "again"
        q = 1 if correct else 0

    if mode == "again":
        # 答错：间隔重排为1天，提前重新进入复习
        reps = 0
        interval = 1
        ease = max(1.3, ease - 0.2)
        total_wrong = row["total_wrong"] + 1
        total_correct = row["total_correct"]
        last_score = 0
    elif mode == "hard":
        # 模糊：算答对，但不让间隔继续拉长而是折半 → 更快再见
        reps += 1
        interval = max(1, round(interval / 2))
        ease = max(1.3, ease - 0.05)
        total_wrong = row["total_wrong"]
        total_correct = row["total_correct"] + 1
        last_score = 1
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
        total_correct = row["total_correct"] + 1
        last_score = 1

    next_due = _due_date(interval)
    conn.execute(
        "UPDATE reviews SET ease=?, interval=?, reps=?, next_due=?, last_score=?,"
        " total_correct=?, total_wrong=?, last_reviewed=? WHERE id=?",
        (ease, interval, reps, next_due, last_score, total_correct, total_wrong,
         datetime.now().isoformat(timespec="seconds"), review_id))
    conn.commit()
    result = {
        "id": review_id, "correct": last_score == 1, "quality": q, "mode": mode,
        "new_interval": interval,
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


def update_output_star(word, status, score=0):
    """③ 造句五星：更新一个词的「主动输出熟练度」。

    与 SRS 完全无关——这里只读/写 word_output 表，绝不碰 reviews、不排复习卡。
    SRS 管「记不记得」，五星管「能不能主动用」。

    规则：
      - PASS         → +1 星（主动用对了）
      - NEEDS_REVIEW → -1 星（拼写/语法/中英混杂/任务未完成）
      - UNCERTAIN    → -1 星（无法确认正确，不算合格输出）
    星级限制 0~5；同时累加回顾次数与最近表现，保留历史。
    星级只能由实际造句结果自动变化，不提供手动修改入口。
    """
    if not word:
        return None
    w = word.strip().lower()
    if not w:
        return None
    delta = 1 if status == "PASS" else -1
    conn = get_conn()
    now = ts()
    row = conn.execute("SELECT * FROM word_output WHERE word=?", (w,)).fetchone()
    if row:
        stars = max(0, min(5, (row["stars"] or 0) + delta))
        conn.execute(
            "UPDATE word_output SET stars=?, total_attempts=?, last_result=?,"
            " last_score=?, last_at=?, updated_at=? WHERE word=?",
            (stars, (row["total_attempts"] or 0) + 1, status,
             int(score or 0), now, now, w))
    else:
        # 首次：PASS 记 1 星；不合格从 0 星起步（不会变负数）
        stars = max(0, min(5, delta))
        conn.execute(
            "INSERT INTO word_output (word, stars, total_attempts, last_result,"
            " last_score, first_at, last_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (w, stars, 1, status, int(score or 0), now, now, now))
    conn.commit()
    conn.close()
    return stars


def word_stars(word):
    """读取一个词的五星熟练度（未记录返回 None，不编造 0 星）。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM word_output WHERE word=?",
        ((word or "").strip().lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def stars_map(words):
    """批量读取多个词的五星熟练度，返回 {word: stars}（仅含已记录的词）。

    供造句计划页实时给每个词打 ★ 用，不编造 0 星。
    """
    out = {}
    if not words:
        return out
    conn = get_conn()
    for w in set((x or "").strip().lower() for x in words if x and x.strip()):
        row = conn.execute(
            "SELECT word, stars FROM word_output WHERE word=?", (w,)).fetchone()
        if row:
            out[row["word"]] = row["stars"]
    conn.close()
    return out


def star_recycle_due(words, days=14):
    """⑤星不再是终态：返回「超过 days 天没主动输出过」的词，供造句计划回流。

    背景：达到 5 星的词原本被永久移出造句计划，导致「忘了写错也掉不了星」，
    两个维度（SRS 记得 / 五星会用）彻底脱钩。这里给 5 星一个复练冷却期：
      - 冷却期内（< days 天没碰）→ 不进计划，避免重复练已经熟的
      - 超过 days 天没碰     → 回流一次，真的写错就掉星回常规循环
    未记录的词（没造过句）一律不算到期复练，不编造数据。
    """
    out = set()
    if not words or days is None:
        return out
    # ts() 生成本地时间 ISO 串，这里同样用本地时间，保证可比
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat()
    conn = get_conn()
    for w in set((x or "").strip().lower() for x in words if x and x.strip()):
        row = conn.execute(
            "SELECT last_at, first_at FROM word_output WHERE word=?", (w,)).fetchone()
        if not row:
            continue
        # last_at / first_at 都是本地 ts() 生成的 ISO 串，用字符串比较即可定序
        ref = (row["last_at"] or row["first_at"] or "").strip()
        if not ref or ref < cutoff:
            out.add(w)
    conn.close()
    return out


def weak_output_words(threshold=3, limit=20):
    """④ 薄弱项：主动输出熟练度偏低（低于 threshold 星）的词。

    按星级升序、最近活动优先，用于「强弱项」页面给主动输出弱的词开小灶。
    仅聚合已记录的词；未造句过的词不出现在这里（不编造 0 星）。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT w.word, w.stars, w.total_attempts, w.last_result, w.last_at, w.updated_at,"
        " d.meaning, d.pos, d.phonetic"
        " FROM word_output w LEFT JOIN dictionary d ON d.word = w.word"
        " WHERE w.stars < ? ORDER BY w.stars ASC, w.updated_at DESC LIMIT ?",
        (threshold, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def flashcard_items(limit=50):
    """② 今日复习闪卡数据：到期 vocab 卡 + 词典释义（翻牌后才显示中文）。

    - 主测「英文 → 意义识别」：正面只给英文 + 发音，翻牌才给中文。
    - 成熟卡（reps>=2）中约 1/4 反过来测「中文 → 英文」做主动提取，
      但绝不让所有卡片都变成中文默写。
    """
    conn = get_conn()
    cards = due_vocab_reviews(limit=limit)
    out = []
    for c in cards:
        word = (c.get("ref_key") or "").strip()
        if not word:
            continue
        drow = conn.execute(
            "SELECT phonetic, pos, meaning FROM dictionary WHERE word=?",
            (word.lower(),)).fetchone()
        reps = c.get("reps") or 0
        # 成熟卡中约 1/4 做反向主动提取（中文 → 英文）
        reverse = reps >= 2 and (c["id"] % 4 == 0)
        out.append({
            "id": c["id"],
            "word": word,
            "phonetic": (drow["phonetic"] if drow else "") or "",
            "pos": (drow["pos"] if drow else "") or "",
            "meaning": (drow["meaning"] if drow else "") or "",
            "direction": "zh2en" if reverse else "en2zh",
            "reps": reps,
            "interval": c.get("interval") or 0,
        })
    conn.close()
    return out


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
