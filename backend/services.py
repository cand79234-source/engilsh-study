"""进度、错误分析、周测 业务逻辑。"""
import json
import re
import zlib
from datetime import date, datetime, timedelta
from db import get_conn, ts, today_str, ERROR_TYPES, app_today
import srs


# ---------- 进度 ----------

def get_progress():
    conn = get_conn()
    row = conn.execute("SELECT * FROM progress WHERE id=1").fetchone()
    conn.close()
    if not row:
        return {"stage": 0, "week": 1, "day": 1, "last_activity": "vocab"}
    return {
        "stage": row["stage"], "week": row["week"], "day": row["day"],
        "last_activity": row["last_activity"] or "vocab",
    }


def set_progress(stage, week, day, last_activity=None):
    """手动调整进度。只改 progress 表（当前位置），绝不触碰历史表。"""
    stage = max(0, min(5, int(stage)))
    week = max(1, min(12, int(week)))
    day = max(1, min(7, int(day)))
    conn = get_conn()
    conn.execute(
        "UPDATE progress SET stage=?, week=?, day=?, last_activity=COALESCE(?, last_activity), updated_at=? WHERE id=1",
        (stage, week, day, last_activity, ts()))
    # 记录一条历史（调整本身，保留原始数据）
    conn.execute(
        "INSERT INTO history (date, stage, week, day, action, detail, created_at) VALUES (?,?,?,?,?,?,?)",
        (today_str(), stage, week, day, "adjust_progress",
         f"手动调整进度到 阶段{stage} Week{week} Day{day}", ts()))
    conn.commit()
    conn.close()
    return {"stage": stage, "week": week, "day": day}


def get_week(stage, week):
    """获取某周内容（含词汇/主题/语法）。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM weeks WHERE stage=? AND week_no=?", (stage, week)).fetchone()
    conn.close()
    if not row:
        return None
    vocab = json.loads(row["vocab_json"] or "[]")
    return {
        "stage": row["stage"], "week": row["week_no"], "title": row["title"],
        "grammar": row["grammar"], "topics": row["topics"], "vocab": vocab,
    }


def normalize_collocations(word_obj):
    """把词条的搭配统一成 [{phrase, meaning, example}] 数组。

    历史上有两种写法混在一起：
      - 新格式：collocations = [{"phrase": ..., "meaning": ..., "example": ...}, ...]
      - 老格式（自动填充/导入生成）：collocation = "an apple / eat an apple"（斜杠分隔字符串）
    接口只读新字段时，老词条的搭配会凭空消失（前端搭配区空白）。
    这里做一次归一化，两种格式都能拿到数组。
    """
    out = []
    for c in (word_obj.get("collocations") or []):
        if isinstance(c, dict):
            phrase = (c.get("phrase") or "").strip()
            if phrase:
                out.append({"phrase": phrase,
                            "meaning": c.get("meaning") or "",
                            "example": c.get("example") or ""})
        elif isinstance(c, str) and c.strip():
            out.append({"phrase": c.strip(), "meaning": "", "example": ""})
    if out:
        return out
    raw = word_obj.get("collocation")
    if isinstance(raw, str) and raw.strip():
        for part in re.split(r"\s*/\s*|\s*[;；]\s*", raw.strip()):
            part = part.strip()
            if part:
                out.append({"phrase": part, "meaning": "", "example": ""})
    return out


def collocation_text(word_obj_or_body):
    """把搭配拍平成一行文本，供 SRS 卡片答案面等纯文本场景使用。"""
    items = normalize_collocations(word_obj_or_body)
    return " / ".join(i["phrase"] for i in items)


def update_week(stage, week, title=None, grammar=None, topics=None, vocab=None):
    """用户编辑每周内容（需求第七节）。vocab 为词对象列表。"""
    conn = get_conn()
    existing = conn.execute("SELECT * FROM weeks WHERE stage=? AND week_no=?", (stage, week)).fetchone()
    if existing:
        if vocab is None:
            # 关键修复：vocab=None 表示「这次不动词汇」（例如只改标题或语法）。
            # 原来无条件写 json.dumps(vocab or []) ，会把整周词汇覆盖成 []，
            # 用户改个标题就丢掉一整周的词 —— 真实数据丢失。
            # 只有调用方明确传 [] 才代表主动清空。
            conn.execute(
                "UPDATE weeks SET title=COALESCE(?,title), grammar=COALESCE(?,grammar),"
                " topics=COALESCE(?,topics) WHERE stage=? AND week_no=?",
                (title, grammar, topics, stage, week))
        else:
            conn.execute(
                "UPDATE weeks SET title=COALESCE(?,title), grammar=COALESCE(?,grammar),"
                " topics=COALESCE(?,topics), vocab_json=? WHERE stage=? AND week_no=?",
                (title, grammar, topics, json.dumps(vocab, ensure_ascii=False), stage, week))
    else:
        conn.execute(
            "INSERT INTO weeks (stage, week_no, title, grammar, topics, vocab_json) VALUES (?,?,?,?,?,?)",
            (stage, week, title or "未命名周", grammar or "", topics or "",
             json.dumps(vocab or [], ensure_ascii=False)))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- 错误 / 薄弱项分析 ----------

def error_breakdown(days=90):
    """按错误类型聚合，返回频率排序 + 最近信息 + 规律 + 补课建议（需求第十五/十六节）。"""
    conn = get_conn()
    since = (app_today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT error_type, COUNT(*) n FROM errors WHERE created_at >= ? GROUP BY error_type",
        (since,)).fetchall()
    counts = {r["error_type"]: r["n"] for r in rows}
    for t in ERROR_TYPES:
        counts.setdefault(t, 0)
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    result = []
    for t, n in ranked:
        if n == 0:
            continue
        recent = conn.execute(
            "SELECT original, corrected, explanation, created_at FROM errors"
            " WHERE error_type=? ORDER BY id DESC LIMIT 8", (t,)).fetchall()
        total = conn.execute("SELECT COUNT(*) n FROM errors WHERE error_type=?", (t,)).fetchone()["n"]
        # 30天内频次
        since30 = (app_today() - timedelta(days=30)).isoformat()
        since60 = (app_today() - timedelta(days=60)).isoformat()
        count_30 = conn.execute(
            "SELECT COUNT(*) n FROM errors WHERE error_type=? AND created_at >= ?",
            (t, since30)).fetchone()["n"]
        # 前一个 30 天（30–60 天前）：用来判断这类错是越来越少（向好）还是越来越多（向差）
        prev_30 = conn.execute(
            "SELECT COUNT(*) n FROM errors WHERE error_type=? "
            "AND created_at >= ? AND created_at < ?",
            (t, since60, since30)).fetchone()["n"]
        # 等级优先取 errors.level 里最高的一级（与 ai_service 写入的 🟡/🔵/🔴 同源）：
        # 近30天同错≥2次 → 🟡，单次 → 🔵；老库没有 level 列时退回按次数判断。
        level = None
        try:
            lvrow = conn.execute(
                "SELECT MAX(CASE level WHEN '🔴' THEN 3 WHEN '🟡' THEN 2 "
                "WHEN '🔵' THEN 1 ELSE 0 END) lv FROM errors WHERE error_type=?",
                (t,)).fetchone()
            lv = int(lvrow["lv"] or 0) if lvrow else 0
            if lv:
                level = "🔴" if lv >= 3 else ("🟡" if lv == 2 else "🔵")
        except Exception:
            level = None
        if not level:
            level = "🔴" if n >= 10 else ("🟡" if n >= 5 else "🔵")
        trend = "better" if count_30 < prev_30 else ("worse" if count_30 > prev_30 else "flat")
        result.append({
            "type": t, "count_30d": count_30, "prev_30d": prev_30, "total": total,
            "level": level, "trend": trend,
            "recent": [dict(r) for r in recent],
            "patterns": _find_patterns(conn, t),
            "remedy": REMEDY_BY_TYPE.get(t, ""),
        })
    conn.close()
    return result


# 各错误类型 → 建议补课（本地规则）
REMEDY_BY_TYPE = {
    "介词": "近期介词出错较多。重点复习常见搭配：go to、arrive at/in、work at、live in、listen to、good at。建议花 5-10 分钟，把上面搭配各造一句正确的句子。",
    "冠词": "冠词薄弱。重点看名词前是否需要 a/an/the：可数单数前要用 a/an，特指用 the，泛指复数不加。建议口头把错误句的正确版本读 3 遍。",
    "时态": "时态容易混。先判断句子的时间词：yesterday/ago 用过去式，tomorrow/will 用将来，now 用进行时，every day 用一般现在。建议复习常用动词的过去式。",
    "主谓一致": "主谓一致需加强。he/she/it/单数名词作主语时，一般现在时动词要加 -s/-es。建议先写主语，再检查动词形式。",
    "固定搭配": "固定搭配要背。错误多来自 like/enjoy/finish+doing、want/need+to do、go+to+地点 这类结构。建议把错过的搭配整理成小卡反复看。",
    "单复数": "单复数易错。注意可数名词前有 a/an 或数字时用单数，表示多个或泛指时用复数，much 接不可数、many 接可数。",
    "词序": "词序需调整。英语陈述句一般是 主语+动词+宾语，疑问句把助动词提前。形容词放在名词前。建议读句子时注意语序。",
    "拼写": "拼写要加强。建议把易错词按读音分节记忆，用词卡每天复习拼错的单词。",
    "词性": "词性混淆。注意一个词可能是名词也可能是动词，看它在句中的位置判断该用哪种词性。建议查词时看词性标注。",
    "句型": "句型结构需熟悉。复习基本句型：主谓、主谓宾、there be、It is...to do...。",
    "其他": "建议把这类错误单独记录，逐条对照正确写法复习。",
}


def _find_patterns(conn, error_type, limit=4):
    """从该类型错误历史中提取高频错误片段，作为'错误规律'。"""
    rows = conn.execute(
        "SELECT original, corrected FROM errors WHERE error_type=?"
        " ORDER BY id DESC LIMIT 60", (error_type,)).fetchall()
    frags = {}
    for r in rows:
        # 取原文与修正里不同的片段做简化归纳（取原文词）
        o = (r["original"] or "").strip()
        c = (r["corrected"] or "").strip()
        key = o if len(o) <= 40 else o[:40]
        if key:
            frags[key] = frags.get(key, 0) + 1
    # 返回出现次数最高的几个错误原文
    top = sorted(frags.items(), key=lambda x: -x[1])[:limit]
    return [{"wrong": w, "times": t} for w, t in top]


def top_weaknesses(limit=3):
    """主页展示的重点薄弱项（取前 N）。"""
    return error_breakdown()[:limit]


def error_detail(error_type):
    """单个错误类型的完整历史 + 规律总结 + 补课建议。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM errors WHERE error_type=? ORDER BY id DESC LIMIT 60",
        (error_type,)).fetchall()
    total = conn.execute("SELECT COUNT(*) n FROM errors WHERE error_type=?", (error_type,)).fetchone()["n"]
    items = [dict(r) for r in rows]
    # 最近7天次数
    since7 = (app_today() - timedelta(days=7)).isoformat()
    count_7 = conn.execute(
        "SELECT COUNT(*) n FROM errors WHERE error_type=? AND created_at >= ?",
        (error_type, since7)).fetchone()["n"]
    conn.close()
    return {
        "type": error_type, "total": total, "count_7d": count_7,
        "items": items,
        "patterns": items_patterns(items, error_type),
        "remedy": REMEDY_BY_TYPE.get(error_type, ""),
    }


def items_patterns(items, error_type):
    """从已取出的 items 归纳规律（离线计算，无需再查库）。"""
    frags = {}
    for r in items:
        key = (r["original"] or "").strip()
        if len(key) <= 40 and key:
            frags[key] = frags.get(key, 0) + 1
    top = sorted(frags.items(), key=lambda x: -x[1])[:4]
    return [{"wrong": w, "times": t} for w, t in top]


# ---------- 周测 ----------

QUIZ_GRAMMAR_TEMPLATES = [
    ("__ work at a bank.", "I", ["I", "Me", "My", "Mine"], 0, "主谓/人称代词"),
    ("She __ to school every day.", "goes", ["go", "goes", "going", "gone"], 1, "一般现在时三单"),
    ("We __ a movie last night.", "watched", ["watch", "watches", "watched", "watching"], 2, "一般过去时"),
    ("There __ many books on the desk.", "are", ["is", "are", "be", "am"], 1, "there be"),
    ("I like __ books.", "reading", ["read", "reads", "reading", "to reading"], 2, "like doing"),
    ("He __ at home yesterday.", "was", ["is", "am", "was", "were"], 2, "be 过去式"),
    ("__ you like tea?", "Do", ["Does", "Do", "Is", "Are"], 1, "助动词"),
    ("I have __ apple.", "an", ["a", "an", "the", "x"], 1, "冠词"),
    ("She lives __ Beijing.", "in", ["at", "in", "on", "to"], 1, "介词"),
    ("They __ playing now.", "are", ["is", "are", "am", "be"], 1, "现在进行"),
]


def build_weekly_quiz(stage, week, count=10):
    """生成一份周测（语法题），结合本周语法，超过模板则用模板。返回题目列表。"""
    conn = get_conn()
    w = conn.execute("SELECT * FROM weeks WHERE stage=? AND week_no=?", (stage, week)).fetchone()
    conn.close()
    grammar = w["grammar"] if w else ""
    # 用模板（10题）；可扩展用 LLM 生成更多，第一版用本地题库。
    qs = []
    for i, (tpl, ans, opts, idx, tag) in enumerate(QUIZ_GRAMMAR_TEMPLATES):
        qs.append({
            "id": i + 1, "question": tpl.replace("__", "____"), "options": opts,
            "answer": idx, "tag": tag,
        })
    return {"grammar": grammar, "questions": qs}


def grade_quiz(stage, week, answers):
    """批改周测，返回得分与是否通过（>=75% 语法题）。"""
    quiz = build_weekly_quiz(stage, week)
    qs = quiz["questions"]
    correct = 0
    detail = []
    for q in qs:
        user_ans = answers.get(str(q["id"]))
        ok = (user_ans is not None and int(user_ans) == q["answer"])
        if ok:
            correct += 1
        detail.append({
            "id": q["id"], "question": q["question"], "user": user_ans,
            "correct_idx": q["answer"], "ok": ok, "tag": q["tag"],
        })
    total = len(qs)
    pct = round(correct / total * 100) if total else 0
    passed = pct >= 75

    conn = get_conn()
    conn.execute(
        "INSERT INTO quizzes (kind, stage, week, score, passed, detail_json, created_at) VALUES ('weekly',?,?,?,?,?,?)",
        (stage, week, pct, 1 if passed else 0, json.dumps(detail, ensure_ascii=False), ts()))
    conn.execute(
        "INSERT INTO history (date, stage, week, day, action, detail, created_at) VALUES (?,?,?,?,?,?,?)",
        (today_str(), stage, week, 7, "weekly_quiz",
         f"语法{pct}% {'通过' if passed else '未通过'}", ts()))
    conn.commit()
    conn.close()
    return {"pct": pct, "correct": correct, "total": total, "passed": passed,
            "detail": detail, "grammar": quiz["grammar"]}


# ---------- 主页聚合 ----------

def home_overview():
    """主页数据：当前进度 + 今日主线完成度 + 今日复习 + 薄弱项。"""
    p = get_progress()
    conn = get_conn()
    # 主线：今日词汇/造句完成情况
    today_vocab_total = 20
    today_vocab_done = conn.execute(
        "SELECT COUNT(*) n FROM day_items WHERE stage=? AND week=? AND day=? AND kind='vocab' AND mastered>0",
        (p["stage"], p["week"], p["day"])).fetchone()["n"]
    today_sentence_done = conn.execute(
        "SELECT COUNT(*) n FROM sentences WHERE stage=? AND week=? AND day=?",
        (p["stage"], p["week"], p["day"])).fetchone()["n"]
    today_sentence_total = 10
    week = get_week(p["stage"], p["week"])
    # 复习到期（今日复习 = 到期 + 今天新学未复习）
    due = []
    today = app_today().isoformat()
    for r in conn.execute(
            "SELECT * FROM reviews WHERE (next_due <= ? OR (created_at >= ? AND last_score = -1))"
            " ORDER BY (created_at >= ?) DESC, next_due LIMIT 30",
            (today, today + "T00:00:00", today + "T00:00:00")).fetchall():
        due.append({
            "id": r["id"], "kind": r["kind"], "ref_key": r["ref_key"],
            "prompt": r["prompt"], "answer": r["answer"],
            "interval": r["interval"], "day": r["day"],
        })
    conn.close()
    return {
        "progress": p,
        "week_title": week["title"] if week else "未设置",
        "week_grammar": week["grammar"] if week else "",
        "week_vocab_count": len(week["vocab"]) if week else 0,
        "today": {
            "vocab_done": min(today_vocab_done, 20), "vocab_total": 20,
            "sentence_done": today_sentence_done, "sentence_total": 10,
        },
        "due_reviews": due,
        "weaknesses": top_weaknesses(3),
        "is_sunday": app_today().weekday() == 6,
    }


# ================= 本地词库（无 AI）=================

def _dictionary_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM dictionary").fetchone()["c"]
    conn.close()
    return n


def lookup_word(word):
    """从本地词库查一个词：中文/词性/例句/搭配。找不到返回 None。"""
    w = word.strip().lower()
    conn = get_conn()
    row = conn.execute("SELECT * FROM dictionary WHERE word=?", (w,)).fetchone()
    if not row:
        # 尝试去掉复数等简单还原
        conn.close()
        return None
    examples = [dict(x) for x in conn.execute(
        "SELECT sentence, translation, grammar_tags FROM example_sentences WHERE word=? ORDER BY difficulty LIMIT 8", (w,)).fetchall()]
    collocs = [dict(x) for x in conn.execute(
        "SELECT phrase, meaning, example FROM collocations WHERE word=?", (w,)).fetchall()]
    conn.close()
    return {
        "word": row["word"], "meaning": row["meaning"], "pos": row["pos"],
        "phonetic": row["phonetic"], "theme": row["theme"], "tag": row["tag"],
        "examples": examples, "collocations": collocs,
    }


def batch_lookup(words):
    """批量查词：返回 {word: {...}}，只返回能在词库找到的词。用于'贴词自动匹配'。"""
    out = {}
    for w in words:
        w2 = w.strip()
        if not w2:
            continue
        r = lookup_word(w2)
        if r:
            out[w2] = r
        else:
            out[w2] = None
    return out


def pick_words_for_theme(theme, limit=20):
    """从词库按主题选词；不足则用通用基础词补足。返回 [{word,meaning,pos,...}]。"""
    conn = get_conn()
    rows = []
    if theme:
        rows += conn.execute(
            "SELECT * FROM dictionary WHERE theme=? ORDER BY bnc, word LIMIT ?", (theme, limit)).fetchall()
    if len(rows) < limit:
        have = {r["word"] for r in rows}
        # 仅从「有主题归属」的精选词补足，避免全量 ECDICT 词（theme 为空）污染空周预设填充
        more = conn.execute(
            "SELECT * FROM dictionary WHERE theme!='' ORDER BY bnc, word").fetchall()
        for r in more:
            if len(rows) >= limit:
                break
            if r["word"] not in have:
                rows.append(r)
    conn.close()
    result = []
    for r in rows:
        item = lookup_word(r["word"])
        if item:
            result.append({
                "word": item["word"], "meaning": item["meaning"], "pos": item["pos"],
                "collocation": item["collocations"][0]["phrase"] if item["collocations"] else "",
                "example": item["examples"][0]["sentence"] if item["examples"] else "",
                "translation": item["examples"][0]["translation"] if item["examples"] else "",
            })
    return result


THEME_BY_WEEK = {
    # 阶段0 基础重建（12 周）
    (0, 1): "家庭", (0, 2): "工作", (0, 3): "爱好", (0, 4): "时间",
    (0, 5): "交通", (0, 6): "日常", (0, 7): "地点", (0, 8): "天气",
    (0, 9): "食物", (0, 10): "健康", (0, 11): "日常", (0, 12): "日常",
    # 阶段1 旅行生存英语（12 周）
    (1, 1): "交通", (1, 2): "交通", (1, 3): "地点", (1, 4): "地点",
    (1, 5): "地点", (1, 6): "食物", (1, 7): "日常", (1, 8): "地点",
    (1, 9): "交通", (1, 10): "地点", (1, 11): "爱好", (1, 12): "日常",
    # 阶段2 工作沟通英语（16 周）
    (2, 1): "工作", (2, 2): "人际", (2, 3): "工作", (2, 4): "时间",
    (2, 5): "工作", (2, 6): "工作", (2, 7): "工作", (2, 8): "工作",
    (2, 9): "工作", (2, 10): "工作", (2, 11): "工作", (2, 12): "工作",
    (2, 13): "工作", (2, 14): "工作", (2, 15): "人际", (2, 16): "工作",
    # 阶段3 社会与信息输入（16 周）
    (3, 1): "日常", (3, 2): "日常", (3, 3): "日常", (3, 4): "工作",
    (3, 5): "日常", (3, 6): "地点", (3, 7): "天气", (3, 8): "健康",
    (3, 9): "日常", (3, 10): "人际", (3, 11): "爱好", (3, 12): "交通",
    (3, 13): "家庭", (3, 14): "人际", (3, 15): "日常", (3, 16): "日常",
    # 阶段4 雅思输出突破（20 周）
    (4, 1): "日常", (4, 2): "日常", (4, 3): "日常", (4, 4): "日常",
    (4, 5): "日常", (4, 6): "工作", (4, 7): "工作", (4, 8): "地点",
    (4, 9): "地点", (4, 10): "交通", (4, 11): "天气", (4, 12): "天气",
    (4, 13): "健康", (4, 14): "食物", (4, 15): "日常", (4, 16): "人际",
    (4, 17): "家庭", (4, 18): "日常", (4, 19): "日常", (4, 20): "爱好",
    # 阶段5 雅思综合与强化（20 周）
    (5, 1): "日常", (5, 2): "日常", (5, 3): "天气", (5, 4): "工作",
    (5, 5): "地点", (5, 6): "健康", (5, 7): "日常", (5, 8): "日常",
    (5, 9): "家庭", (5, 10): "日常", (5, 11): "日常", (5, 12): "日常",
    (5, 13): "天气", (5, 14): "爱好", (5, 15): "日常", (5, 16): "日常",
    (5, 17): "日常", (5, 18): "日常", (5, 19): "日常", (5, 20): "日常",
}


def _normalize_vocab(vocab):
    """把 vocab 归一化成 dict 数组，返回 (归一化列表, 是否有改动)。

    weeks.vocab_json 在历史上有两种形状：
      旧：["word1", "word2"]                    —— 纯字符串数组
      新：[{"word": ..., "day": ..., ...}]      —— 对象数组
    /api/today 里 `v.get("day")` 只兼容新形状，遇到旧形状直接
    AttributeError → 500 → 学习页显示「加载失败」。
    在唯一读取入口统一归一化：字符串升级为 {"word": 字符串}，
    不丢任何信息（字符串本来就没有 day/meaning 可言）。
    """
    if not isinstance(vocab, list):
        return [], bool(vocab)
    out, changed = [], False
    for v in vocab:
        if isinstance(v, str):
            s = v.strip()
            if s:
                out.append({"word": s})
            changed = True          # 空字符串丢弃也算形状变化
        elif isinstance(v, dict):
            out.append(v)
        else:
            changed = True          # 其它怪形状直接丢弃
    return out, changed


def ensure_week_content(stage, week, force=False):
    """确保某周有 >= 目标 词数内容。若该周 vocab 为空则从词库自动填充（不覆盖用户已填）。
    返回该周最终内容（含 vocab 词条列表，每条带 word/meaning/pos/collocation/example/translation）。"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM weeks WHERE stage=? AND week_no=?", (stage, week)).fetchone()
    # 若 weeks 表无该周记录（老库或越界），返回兜底对象，避免 today() 空指针
    if not row:
        conn.close()
        return {
            "stage": stage, "week": week,
            "title": f"阶段{stage}·第{week}周",
            "grammar": "", "theme": THEME_BY_WEEK.get((stage, week)), "vocab": [],
        }
    try:
        vocab = json.loads(row["vocab_json"] or "[]")
    except Exception:
        vocab = []                  # 库里 JSON 坏了也不能炸掉整个学习页
    vocab, migrated = _normalize_vocab(vocab)
    theme = THEME_BY_WEEK.get((stage, week))
    if not vocab and _dictionary_count() > 0:
        filled = pick_words_for_theme(theme, 20)
        # 标记内置预设词，供导入合并时区分并排除（避免混入用户没填的词）
        for it in filled:
            it.setdefault("source", "builtin")
        vocab = filled
        migrated = True
    if migrated:
        # 旧格式（或坏 JSON / 自动填充结果）写回库，自愈成新格式，
        # 下次读取不再走迁移分支
        conn.execute(
            "UPDATE weeks SET vocab_json=? WHERE stage=? AND week_no=?",
            (json.dumps(vocab, ensure_ascii=False), stage, week))
    conn.commit()
    conn.close()
    return {
        "stage": stage, "week": week, "title": row["title"],
        "grammar": row["grammar"], "theme": theme, "vocab": vocab,
    }


def import_user_words(words, stage, week, overwrite=True):
    """用户贴一批英文单词：逐词匹配词库，组成该周/当天词汇。返回匹配统计与词条。"""
    # words: 用户提供的英文词列表(可含空格逗号换行)。这里假设已切分。
    matched = []
    unmatched = []
    for w in words:
        w2 = w.strip()
        if not w2:
            continue
        item = lookup_word(w2)
        if item:
            matched.append(item)
        else:
            unmatched.append(w2)
    return {"matched": matched, "unmatched": unmatched}


def week_word_count(stage, week):
    """某周当前词数。"""
    row = get_week(stage, week)
    return len(row["vocab"]) if row else 0


# ---------- 动态造句 prompt 生成（纯本地，无 AI） ----------

# 6 个功能/主题类别：名称、单句指令模板、中英文关键词
# 5 星词的复练冷却天数：达到 5 星后，超过这么久没主动输出过就回流一次。
# 目的：5 星是「当前稳定」而非「永久毕业」，忘了写错要能掉星回常规循环。
FIVE_STAR_RECYCLE_DAYS = 14

# 6 个功能/主题类别：名称、单句指令模板、中英文关键词
#
# 【2026-09 改：任务句全部简化】
#   原来每类都带一句任务描述（"写一句关于你自己的话（名字/身份/来自哪里）"
#   "写你打算/周末要做的事"…），结果是同一个词被它的词义锁死在一种句式上，
#   还容易和页面统一追加的语法打架（自带"（一般过去时）" vs 本周语法"一般现在时"）。
#   现在模板一律简化成「用「词」」——**写什么由后面的 AI 情景决定**，
#   类别只剩一个内部标签：它决定这条练习从哪个角度（自我介绍/描述日常/…）出。
#   关键词仍保留，用于给词打分类作为参考，不再直接决定句式。
FUNC_CATEGORIES = [
    ("自我介绍", "用「{w}」",
     ["name", "family", "friend", "meet", "hello", "student", "job", "home",
      "名字", "来自", "家人", "朋友", "认识", "学生", "工作", "家", "我是"]),
    ("描述日常", "用「{w}」",
     ["always", "usually", "morning", "every", "habit", "start", "work", "study",
      "每天", "经常", "习惯", "通常", "早上", "开始", "工作", "学习", "日常"]),
    ("过去经历", "用「{w}」",
     ["yesterday", "ago", "last", "went", "visited", "finished",
      "曾经", "以前", "过去", "完成", "去了", "经历"]),
    ("计划安排", "用「{w}」",
     ["plan", "will", "going", "weekend", "tomorrow", "next",
      "计划", "周末", "明天", "下次", "将要", "打算"]),
    ("喜好偏好", "用「{w}」",
     ["like", "enjoy", "hate", "love", "favorite", "hobby",
      "喜欢", "讨厌", "爱好", "最爱", "享受", "厌恶"]),
    ("建议看法", "用「{w}」",
     ["should", "must", "advice", "think", "because", "good", "better",
      "应该", "建议", "因为", "看法", "最好", "认为", "意见"]),
]


CAT_NAME_TO_INDEX = {name: i for i, (name, _, _) in enumerate(FUNC_CATEGORIES)}


def _classify_word(word_obj):
    """给一个词在 6 个功能类别上打分。返回 {cat_index: score, ...}。"""
    word = (word_obj.get("word") or "").strip().lower()
    meaning = (word_obj.get("meaning") or "").lower()
    pos = (word_obj.get("pos") or "").lower()
    colloc_text = ""
    for c in word_obj.get("collocations") or []:
        colloc_text += " " + (c.get("phrase") or "")
    for ex in word_obj.get("examples") or []:
        colloc_text += " " + (ex.get("sentence") or "") + " " + (ex.get("translation") or "")
    full_text = f"{word} {meaning} {pos} {colloc_text.lower()}"
    scores = {}
    for idx, (cat_name, instr, keywords) in enumerate(FUNC_CATEGORIES):
        score = 0
        for kw in keywords:
            if kw in full_text:
                # 英文词根匹配权重略低，中文释义匹配权重高
                score += 2 if any('\u4e00' <= ch <= '\u9fff' for ch in kw) else 1
        # 词性微调
        if pos.startswith("名") and cat_name in ("自我介绍", "描述日常"):
            score += 1
        if pos.startswith("动") and cat_name in ("描述日常", "喜好偏好", "建议看法"):
            score += 1
        if pos.startswith("副") and cat_name == "描述日常":
            score += 1
        scores[idx] = score
    return scores


def _deterministic_shuffle(seq, seed):
    """用整数 seed 对列表做确定性洗牌（Fisher-Yates），保证同日刷新结果一致。"""
    if not seq:
        return []
    seq = list(seq)
    n = len(seq)
    for i in range(n - 1, 0, -1):
        seed = (seed * 9301 + 49297) % 233280
        j = seed % (i + 1)
        seq[i], seq[j] = seq[j], seq[i]
    return seq


# ---------- 练习角度轮换：随机分配 + 薄弱角度后台加权 ----------
#
# 【为什么改】原来每条基础句的角度由「词义关键词打分」决定（_classify_word），
#   结果是同一个词被它的中文释义永久锁死在一个角度上，6 个角度分布严重不均
#   （实测某天：9 条落在「描述日常」，0 条落在「过去经历」）。
#
# 【现在怎么做】角度 = f(词, 日期)
#   - 同一个词今天永远是这个角度，明天自动换一个；
#   - 词源（当天新词 + 到期复习词）**完全不动**，只换提问角度；
#   - 前端不新增任何类别按钮/数据，用户全程无感；
#   - 每条作答把这个角度记进 sentences.category，后台据此给弱项加权重。
#
# 【为什么统计截止到昨天】当天每提交一次，统计数字就会变；
#   若权重当天跟着变，用户刷新页面题就跳了。所以窗口取最近 N 天但**不含今天**：
#   当天完全稳定，隔天自动带上最新薄弱信号。

ANGLE_HISTORY_DAYS = 30      # 统计窗口（天）
ANGLE_MIN_SAMPLES = 4        # 某个角度样本不足这么多就不判断强弱，一律等权
ANGLE_WEIGHT_MAX = 3.0       # 最弱角度最多拿到 3 倍曝光


def _angle_u01(word, seed_date):
    """词 + 日期 → 稳定的 [0,1) 随机数。同一天同一词恒定，隔天自动换。"""
    raw = "%s|%s" % (seed_date.isoformat(), str(word or "").strip().lower())
    return (zlib.crc32(raw.encode("utf-8")) & 0xFFFFFFFF) / float(0x100000000)


def angle_weights(days=ANGLE_HISTORY_DAYS):
    """读最近 days 天（不含今天）的作答，算出每个练习角度的强弱权重。

    返回 {角度下标: weight}，weight ∈ [1.0, ANGLE_WEIGHT_MAX]。
    低于平均正确率的角度权重变高 → 之后被抽到的概率变大。
    没数据 / 样本不足 / 查询失败一律 1.0 —— 数据不够就不猜，
    且绝不让「记账」这件事把出题拖垮。
    """
    base = {i: 1.0 for i in range(len(FUNC_CATEGORIES))}
    try:
        start = (app_today() - timedelta(days=int(days))).isoformat()
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT category, COUNT(*) n,"
                " COALESCE(SUM(CASE WHEN good=1 THEN 1 ELSE 0 END),0) ok"
                " FROM sentences"
                " WHERE COALESCE(category,'')<>'' AND created_at>=? AND created_at<?"
                " GROUP BY category",
                (start, today_str())).fetchall()
        finally:
            conn.close()
    except Exception as e:          # 老库还没迁移出 category 列等，退回等权
        print("[angle] 难度统计不可用，按等权出题: %s" % e)
        return base

    stat, tot_n, tot_ok = {}, 0, 0
    for r in rows:
        idx = CAT_NAME_TO_INDEX.get((r["category"] or "").strip())
        if idx is None:
            continue
        n = int(r["n"] or 0)
        ok = int(r["ok"] or 0)
        stat[idx] = (n, ok)
        tot_n += n
        tot_ok += ok
    if tot_n <= 0:
        return base
    avg = tot_ok / float(tot_n)
    for idx, (n, ok) in stat.items():
        if n < ANGLE_MIN_SAMPLES:
            continue                       # 样本太少，不评价强弱
        acc = ok / float(n)
        if acc >= avg:
            continue                       # 不比平均差 → 不加餐
        gap = (avg - acc) / max(avg, 0.05)
        base[idx] = round(min(ANGLE_WEIGHT_MAX,
                              1.0 + gap * (ANGLE_WEIGHT_MAX - 1.0)), 3)
    return base


def angle_of_word(word, seed_date=None, weights=None):
    """这个词今天被分到哪个练习角度。返回 FUNC_CATEGORIES 的下标。

    用词本身做哈希的好处：三处（出题 / AI 情景 / 批改入库）各自算都得到同一个值，
    不需要同步状态，也不依赖词在列表里的位置。
    """
    if seed_date is None:
        seed_date = app_today()
    if weights is None:
        weights = angle_weights()
    k = len(FUNC_CATEGORIES)
    ws = [float(weights.get(i, 1.0) or 1.0) for i in range(k)]
    total = sum(ws)
    if total <= 0:
        ws, total = [1.0] * k, float(k)
    r, cum = _angle_u01(word, seed_date) * total, 0.0
    for i, w in enumerate(ws):
        cum += w
        if r < cum:
            return i
    return k - 1


def angle_name_of_word(word, seed_date=None, weights=None):
    """角度下标 → 中文名。批改入库时用它写 sentences.category。"""
    return FUNC_CATEGORIES[angle_of_word(word, seed_date, weights)][0]


def category_report(days=ANGLE_HISTORY_DAYS):
    """分角度台账：练了多少、错了多少、当前权重。**不进任何 UI**，只给后台/调试看。"""
    try:
        start = (app_today() - timedelta(days=int(days))).isoformat()
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT category, COUNT(*) n,"
                " COALESCE(SUM(CASE WHEN good=1 THEN 1 ELSE 0 END),0) ok"
                " FROM sentences WHERE COALESCE(category,'')<>'' AND created_at>=?"
                " GROUP BY category", (start,)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        return {"days": days, "error": str(e), "items": [], "weights": {}}
    got = {(r["category"] or "").strip(): (int(r["n"] or 0), int(r["ok"] or 0))
           for r in rows}
    weights = angle_weights(days)
    items = []
    for i, (name, _, _) in enumerate(FUNC_CATEGORIES):
        n, ok = got.get(name, (0, 0))
        items.append({"category": name, "total": n, "correct": ok, "wrong": n - ok,
                      "acc": round(ok / float(n), 3) if n else None,
                      "weight": weights.get(i, 1.0)})
    return {"days": days, "items": items, "weights": weights}


def build_sentence_prompts(today_new, due_vocab, grammar, stage, week, day, seed_date=None):
    """生成今日 10 条造句引导。

    - today_new: 当天新词列表（每个含 word/meaning/pos/可选 collocations/examples）
    - due_vocab: 当天到期 vocab 复习词列表（结构同上）
    - grammar: 本周语法重点字符串
    - seed_date: 日期对象，默认今天；用于保证每天不同但同日稳定

    规则：
    1. 合并 today_new + due_vocab，去重（today_new 优先）。
    2. 每个词按 6 类关键词打分，选出最佳类别。
    3. 用日期 seed 确定性轮换类别顺序、打乱同类内词序。
    4. 每类取前 2 个词，每个词生成 1 条独立 prompt。
    5. 不足 10 条时，用本周语法点生成通用回填句补齐。
    """
    if seed_date is None:
        seed_date = app_today()
    base_seed = abs(hash(f"{stage}-{week}-{day}-{seed_date.isoformat()}")) % (10**9)

    # 合并池：today_new 优先，due_vocab 补位且去重
    seen = set()
    pool = []
    for w in today_new + (due_vocab or []):
        key = (w.get("word") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        pool.append(w)

    # 给每个词打分并归类
    word_scores = []
    for w in pool:
        scores = _classify_word(w)
        best_cat = max(scores, key=scores.get) if scores else 0
        best_score = scores.get(best_cat, 0)
        word_scores.append((w, best_cat, best_score))

    # 按类别分组
    buckets = {i: [] for i in range(len(FUNC_CATEGORIES))}
    for w, cat, sc in word_scores:
        buckets[cat].append((w, sc))

    # 类别轮换顺序
    cat_order = _deterministic_shuffle(list(range(len(FUNC_CATEGORIES))), base_seed)

    prompts = []
    used_words = set()

    for cat_idx in cat_order:
        cat_name, instr_tmpl, _ = FUNC_CATEGORIES[cat_idx]
        # 同类内按得分降序，再用 seed 洗牌前 K 名避免每天都用同样的词
        items = sorted(buckets.get(cat_idx, []), key=lambda x: -x[1])
        if not items:
            continue
        # 限制取前 6 再洗牌，避免低分词入选
        items = items[:6]
        items = _deterministic_shuffle(items, base_seed + cat_idx + 1)
        # 取最多 2 个未用过的词
        picked = []
        for w, sc in items:
            key = (w.get("word") or "").strip().lower()
            if key not in used_words:
                picked.append(w)
                used_words.add(key)
            if len(picked) >= 2:
                break
        for w in picked:
            word_text = w.get("word") or ""
            meaning_text = w.get("meaning") or ""
            display = f"{word_text}（{meaning_text}）" if meaning_text else word_text
            prompt = f"【{cat_name}】{instr_tmpl.format(w=display)}"
            if grammar:
                prompt += f"（可参考语法：{grammar}）"
            prompts.append(prompt)
        if len(prompts) >= 10:
            break

    # 若不足 10 条，用语法通用句回填
    grammar_fillers = [
        "用今天学的一个词，写一句关于你自己的话",
        "用今天学的一个词，写一句你真实经历过的事",
        "用今天学的一个词，写一句你明天或周末要做的事",
        "用今天学的一个词，写一句你喜欢做的事",
        "用今天学的一个词，给一个建议或表达看法",
    ]
    if grammar:
        grammar_fillers = [f"{base}（提示：{grammar}）" for base in grammar_fillers]
    gi = 0
    while len(prompts) < 10 and gi < 100:
        prompts.append(grammar_fillers[gi % len(grammar_fillers)])
        gi += 1

    return prompts[:10]


# ==================== 两段式造句计划 ====================
# 用户定的学习节奏：
#   ① 基础：当天每个词各造一句（会用）
#   ② 组合：10 组，每组 2-3 个词写成连续表达；复习词混进这里（真正放进表达里）
# 说明：组合表达只是任务引导，不强制写多长、不强制用满词。
#
# 【② 升级已删除（2026-09-10）】
# 原设计：挑 5 个重点词，给「升级方向 + 示范句」，把原句改得更自然。
# 删掉的原因（三条都实测过）：
#   1. 20 个词已经在①基础句里每个都写过一遍，再挑 5 个重写一遍是重复劳动；
#   2. 升级方向是**预置的 6 个方向**（加原因/加结果/…），不是针对用户写的
#      那句话给的，实质只是"换个角度再写一句"，价值低；
#   3. 挑词规则给复习词起手 50 分、新词只有 0-9 分，导致升级句里全是复习词，
#      与"只服务当天新词"的设计意图相反。
# 「给方向 + 示范句」这个价值已挪到**批改反馈**里（用户提交后由 AI 针对他
# 那句话给具体改法），不占题量、而且是针对性的。
#
# 一并删除：UPGRADE_DIRECTIONS / _pick_focus_words / _build_upgrade。
# sentences 表里历史存下的 up:0 / up:1 记录**保留不动**（只是不再产生新的），
# 不影响任何统计与页面。


def _word_key(w):
    return (w.get("word") or "").strip().lower()


def _display(w):
    """词 + 中文，用于提示文案。"""
    word = w.get("word") or ""
    meaning = (w.get("meaning") or "").strip()
    return f"{word}（{meaning}）" if meaning else word


def _build_basic(today_new, grammar, seed_date=None, weights=None):
    """① 基础：当天每个词各一句，给一个练习角度（会用）。

    角度**不再由词义决定** —— 词义打分会让同一个词永久锁死在一个句式上，
    6 个角度分布也严重不均（实测某天 9:0）。现在角度 = f(词, 当天日期)：
    今天这个词从这个角度写，明天自动换；词源一个字不动。
    """
    if seed_date is None:
        seed_date = app_today()
    if weights is None:
        weights = angle_weights()
    out = []
    for i, w in enumerate(today_new, 1):
        key = _word_key(w)
        if not key:
            continue
        cat_idx = angle_of_word(key, seed_date, weights)
        cat_name, instr_tmpl, _ = FUNC_CATEGORIES[cat_idx]
        task = instr_tmpl.format(w=_display(w))
        if grammar:
            task += f"（语法：{grammar}）"
        out.append({
            "i": i, "word": w.get("word"), "meaning": w.get("meaning") or "",
            "category": cat_name, "task": task,
            "focus": bool(w.get("focus")),
        })
    return out


def _build_combos(today_new, due_vocab, grammar, seed, n=10, per=3):
    """③ 组合：n 组，每组 2-3 个词写成连续表达。复习词优先入选且不额外占位。"""
    seen = set()
    review_words, new_words = [], []
    for w in due_vocab:                      # due_vocab 已按"错误率+久未复习"排好序
        k = _word_key(w)
        if k and k not in seen:
            seen.add(k)
            review_words.append(w)
    for w in today_new:
        k = _word_key(w)
        if k and k not in seen:
            seen.add(k)
            new_words.append(w)

    # 洗牌新词（同日稳定），让每天的搭配不重样
    new_words = _deterministic_shuffle(new_words, seed + 99)

    # 每组配比：复习词占多数（2026-09-10 改）
    #
    # 旧配比是「1 复习 + 2 新词」，复习只占 1/3 —— 与"复习为主"的设计意图相反。
    # 现在改成 n_review_per_group 个复习词 + 剩下的位置给新词。
    # 默认 2 复习 + 1 新词（per=3）：复习占 2/3，且**每组还是 3 个词**，
    # 页面「建议 2-3 句」的长度提示不用改，不会突然变长。
    #
    # 【复习词不够时不补（用户定）】
    # 复习词靠 SRS 到期产生，某天可能只有三五个。这时**不重复使用复习词**，
    # 缺的位置直接让新词顶上 —— 复习占比自然退化，但不硬凑。
    # 理由：同一个词一天写两三遍是纯抄，不产生新记忆。
    # 复查（2026-09-10 实测）：库存 8 个复习词时，2A 与 3+1 方案的实际效果
    # 完全一样（都退化到 复习 8 / 新词 20，占 28%）—— 所以不补也不会更差。
    n_review_per_group = min(2, max(1, per - 1)) if len(review_words) else 0
    combos, ri, ni = [], 0, 0
    for gi in range(n):
        group = []
        for _ in range(n_review_per_group):
            if ri >= len(review_words):
                break
            group.append({"word": review_words[ri].get("word"),
                          "meaning": review_words[ri].get("meaning") or "",
                          "review": True})
            ri += 1
        while len(group) < per and ni < len(new_words):
            group.append({"word": new_words[ni].get("word"),
                          "meaning": new_words[ni].get("meaning") or "",
                          "review": False})
            ni += 1
        # 词总量不够凑满 per 个时，宁可不凑（不生成只有 1-2 个词的残缺组）：
        # 单独一个词写不出"连续表达"，那种题是废题还占题量。
        # 直接 break —— 后面所有组都只会更短。
        if len(group) < 2:
            break
        if not group:
            # 复习词和新词都用光了才走到这里。这时允许复读已用过的词：
            # 复习词优先（重复本身就是复习），但只用来补满组，不提前触发。
            if not (review_words or new_words):
                break
            pool = review_words + new_words
            for j in range(per):
                src = pool[(gi * per + j) % len(pool)]
                group.append({"word": src.get("word"),
                              "meaning": src.get("meaning") or "",
                              "review": bool(src.get("error_rate"))})
        if not group:
            break

        # 场景：用组内首个词的功能分类作为表达框架。
        # 配比改了以后首个词固定是复习词，分类跟着复习词走 —— 没问题，
        # 组合表达本来就是用复习词当主线的。
        first = next((w for w in (today_new + due_vocab)
                      if _word_key(w) == _word_key({"word": group[0]["word"]})), None)
        cat = "自由表达"
        if first is not None:
            sc = _classify_word(first)
            cat = FUNC_CATEGORIES[max(sc, key=sc.get)][0]
        names = " + ".join(g["word"] for g in group)
        n_rev = sum(1 for g in group if g["review"])
        combos.append({
            "i": gi + 1, "words": group, "category": cat,
            "scene": f"【{cat}】用 {names} 写一段连续表达",
            "task": f"【{cat}】用 {names} 写一段关于你自己的连续表达"
                    + (f"（{n_rev} 个是到期复习词）" if n_rev else "")
                    + (f"（语法：{grammar}）" if grammar else ""),
            "hint": "建议 2-3 句，写成一小段；写多写少随意，不强制。"
                    "系统只在你提交后告诉你用到了哪几个词。",
            "has_review": n_rev > 0,
        })
    return combos


def build_sentence_plan(today_new, due_vocab, grammar, stage, week, day,
                        seed_date=None, n_combo=10):
    """两段式造句计划。返回 {basic, combo, meta}。

    ⚠️ 返回值里**不再有 upgrade 字段**（2026-09-10 删除升级句）。
    前端已同步删掉「② 升级」整块；旧客户端读到 undefined 也不会报错
    （那边本来就是 `(p.upgrade||[]).map(...)` 的兜底写法）。
    n_upgrade 参数一并移除 —— 留着会让人以为还能打开。
    """
    if seed_date is None:
        seed_date = app_today()
    seed = abs(hash(f"{stage}-{week}-{day}-{seed_date.isoformat()}")) % (10 ** 6)

    # 造句五星：达到 5 星的词「主动输出已稳定」，不进常规造句计划。
    #
    # 但 5 星不是终态——原实现一旦到 5 星就永久剔除，导致「后来忘了、写错了
    # 也掉不了星」，五星维度只升不降，与 SRS 彻底脱钩。
    # 现在改成复练冷却：超过 FIVE_STAR_RECYCLE_DAYS 天没主动输出过就回流一次，
    # 回流后写错 → 掉到 4 星，重回常规循环；写对 → 回 5 星，重新冷却。
    cand_words = [w.get("word") for w in (today_new or [])] + \
                 [w.get("word") for w in (due_vocab or [])]
    starred = srs.stars_map(cand_words)
    five_star = {w for w, s in starred.items() if s >= 5}
    if five_star:
        # 冷却期内（还没到复练时间）的 5 星词才剔除；到期的保留在池子里
        recyclable = srs.star_recycle_due(five_star, days=FIVE_STAR_RECYCLE_DAYS)
        cooldown = five_star - recyclable
        if cooldown:
            today_new = [w for w in (today_new or [])
                         if w.get("word", "").strip().lower() not in cooldown]
            due_vocab = [w for w in (due_vocab or [])
                         if w.get("word", "").strip().lower() not in cooldown]

    # 角度权重：计划在本次生成里只查一次，传给各个环节共用（口径一致）
    weights = angle_weights()

    basic = _build_basic(today_new, grammar, seed_date, weights)
    combo = _build_combos(today_new, due_vocab, grammar, seed, n_combo)

    return {
        "basic": basic,
        "combo": combo,
        "meta": {
            "basic_count": len(basic),
            "combo_count": len(combo),
            "review_count": len(due_vocab or []),
            "grammar": grammar,
            "note": "①基础每词一句 ②10组组合(复习词占多数，不强制长度)",
        },
    }
