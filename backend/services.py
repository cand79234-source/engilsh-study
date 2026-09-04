"""进度、错误分析、周测 业务逻辑。"""
import json
import re
from datetime import date, datetime, timedelta
from db import get_conn, ts, today_str, ERROR_TYPES
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
    since = (date.today() - timedelta(days=days)).isoformat()
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
        since30 = (date.today() - timedelta(days=30)).isoformat()
        count_30 = conn.execute(
            "SELECT COUNT(*) n FROM errors WHERE error_type=? AND created_at >= ?",
            (t, since30)).fetchone()["n"]
        level = "🔴" if n >= 10 else ("🟡" if n >= 5 else "🔵")
        result.append({
            "type": t, "count_30d": count_30, "total": total, "level": level,
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
    since7 = (date.today() - timedelta(days=7)).isoformat()
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
    today = date.today().isoformat()
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
        "is_sunday": date.today().weekday() == 6,
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
    # 阶段0
    (0, 1): "家庭", (0, 2): "日常", (0, 3): "爱好", (0, 4): "日常",
    (0, 5): "交通", (0, 6): "日常", (0, 7): "地点", (0, 8): "天气",
    (0, 9): "食物", (0, 10): "健康", (0, 11): "日常", (0, 12): "日常",
    # 阶段1-5：按周标题/主题映射，用于无词时兜底填充
    (1, 1): "日常", (1, 2): "家庭", (1, 3): "日常", (1, 4): "日常",
    (1, 5): "工作", (1, 6): "健康", (1, 7): "日常", (1, 8): "爱好",
    (1, 9): "日常", (1, 10): "交通", (1, 11): "购物", (1, 12): "日常",
    (2, 1): "日常", (2, 2): "日常", (2, 3): "日常", (2, 4): "日常",
    (2, 5): "工作", (2, 6): "日常", (2, 7): "日常", (2, 8): "健康",
    (2, 9): "日常", (2, 10): "日常", (2, 11): "日常", (2, 12): "日常",
    (3, 1): "工作", (3, 2): "日常", (3, 3): "日常", (3, 4): "日常",
    (3, 5): "日常", (3, 6): "日常", (3, 7): "日常", (3, 8): "日常",
    (3, 9): "日常", (3, 10): "日常", (3, 11): "健康", (3, 12): "日常",
    (4, 1): "工作", (4, 2): "日常", (4, 3): "日常", (4, 4): "日常",
    (4, 5): "日常", (4, 6): "日常", (4, 7): "日常", (4, 8): "日常",
    (4, 9): "日常", (4, 10): "日常", (4, 11): "日常", (4, 12): "日常",
    (5, 1): "工作", (5, 2): "日常", (5, 3): "日常", (5, 4): "日常",
    (5, 5): "日常", (5, 6): "日常", (5, 7): "工作", (5, 8): "日常",
    (5, 9): "日常", (5, 10): "日常", (5, 11): "工作", (5, 12): "日常",
}


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
    vocab = json.loads(row["vocab_json"] or "[]")
    theme = THEME_BY_WEEK.get((stage, week))
    if not vocab and _dictionary_count() > 0:
        filled = pick_words_for_theme(theme, 20)
        # 标记内置预设词，供导入合并时区分并排除（避免混入用户没填的词）
        for it in filled:
            it.setdefault("source", "builtin")
        vocab = filled
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
FUNC_CATEGORIES = [
    ("自我介绍", "用「{w}」写一句关于你自己的话（名字/身份/来自哪里）",
     ["name", "family", "friend", "meet", "hello", "student", "job", "home",
      "名字", "来自", "家人", "朋友", "认识", "学生", "工作", "家", "我是"]),
    ("描述日常", "用「{w}」写你现在/每天都做的事",
     ["always", "usually", "morning", "every", "habit", "start", "work", "study",
      "每天", "经常", "习惯", "通常", "早上", "开始", "工作", "学习", "日常"]),
    ("过去经历", "用「{w}」写一件你昨天或上周做过的事（一般过去时）",
     ["yesterday", "ago", "last", "went", "visited", "finished",
      "昨天", "上周", "以前", "曾经", "过去", "完成", "去了"]),
    ("计划安排", "用「{w}」写你打算/周末要做的事",
     ["plan", "will", "going", "weekend", "tomorrow", "next",
      "计划", "周末", "明天", "下次", "将要", "打算"]),
    ("喜好偏好", "用「{w}」写你喜欢或不喜欢做的事（like/enjoy/hate+doing）",
     ["like", "enjoy", "hate", "love", "favorite", "hobby",
      "喜欢", "讨厌", "爱好", "最爱", "享受", "厌恶"]),
    ("建议看法", "用「{w}」给一个建议或表达你的看法",
     ["should", "must", "advice", "think", "because", "good", "better",
      "应该", "建议", "因为", "看法", "最好", "认为", "意见"]),
]


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
        seed_date = date.today()
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
        "用今天学的一个词，写一句你昨天做过的事",
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


# ==================== 三段式造句计划 ====================
# 用户定的学习节奏：
#   ① 基础：当天每个词各造一句（会用）
#   ② 升级：挑 5 个重点词，给升级方向+示范句，把原句改得更自然（用得更自然）
#   ③ 组合：10 组，每组 2-3 个词写成连续表达；复习词混进这里（真正放进表达里）
# 说明：组合表达只是任务引导，不强制写多长、不强制用满词。

# 升级方向库（纯本地规则：给方向 + 可照搬的示范，不是 AI 润色）
UPGRADE_DIRECTIONS = [
    ("加原因", "用 because / since 说明「为什么」，句子立刻有内容",
     "I am busy. → I am busy because I have a deadline this week."),
    ("加结果或目的", "用 so / so that 接上「结果」或「目的」",
     "I work hard. → I work hard so that I can finish the report on time."),
    ("补具体信息", "补上时间 / 地点 / 方式，别让句子悬在半空",
     "I have a meeting. → I have a meeting with my manager at 3 pm."),
    ("换成固定搭配", "把泛泛的说法换成这个词的固定搭配，地道度立刻上来",
     "I finished the report. → I met the deadline for the report."),
    ("两句并一句", "把两个短句用 and / but / which 连起来，避免全是简单句",
     "I have a meeting. It is at 3. → I have a meeting which starts at 3."),
    ("加程度或频率", "用 usually / quite / a bit 等，让语气更真实",
     "This task is hard. → This task is quite hard for me."),
]


def _word_key(w):
    return (w.get("word") or "").strip().lower()


def _display(w):
    """词 + 中文，用于提示文案。"""
    word = w.get("word") or ""
    meaning = (w.get("meaning") or "").strip()
    return f"{word}（{meaning}）" if meaning else word


def _pick_focus_words(today_new, due_vocab, n=5):
    """挑出 n 个「重点升级词」。

    优先级：用户★标记 > 复习词错误率高 > 搭配丰富(升级空间大) > 还没掌握
    复习词与今日新词重复时只保留新词那份（避免同一词既基础又升级）。
    """
    scored = []
    seen = set()
    for w in today_new:
        key = _word_key(w)
        if not key or key in seen:
            continue
        seen.add(key)
        s = 0.0
        if w.get("focus"):                       # 用户自己标的 ★，最高优先
            s += 100
        s += min(len(w.get("collocations") or []), 4) * 2.0
        if not w.get("mastered"):
            s += 1.0
        scored.append((s, key, w, False))
    for w in due_vocab:
        key = _word_key(w)
        if not key or key in seen:
            continue
        seen.add(key)
        er = w.get("error_rate") or 0
        # 复习词基准分高于普通新词（错过的词更该升级），错误率权重最高
        s = 50 + er * 30 + (w.get("priority") or 0) * 5
        scored.append((s, key, w, True))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(w, is_review) for _, _, w, is_review in scored[:n]]


def _build_basic(today_new, grammar):
    """① 基础：当天每个词各一句，给一个功能场景（会用）。"""
    out = []
    for i, w in enumerate(today_new, 1):
        key = _word_key(w)
        if not key:
            continue
        scores = _classify_word(w)
        cat_idx = max(scores, key=scores.get) if scores else 0
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


def _build_upgrade(today_new, due_vocab, grammar, seed, n=5):
    """② 升级：n 个重点词，每个给「升级方向 + 示范句」。"""
    picked = _pick_focus_words(today_new, due_vocab, n)
    out = []
    for i, (w, is_review) in enumerate(picked, 1):
        # 用 seed 轮换升级方向，保证同一天不同词拿到不同方向
        di = (seed + i * 7) % len(UPGRADE_DIRECTIONS)
        dname, ddesc, dsample = UPGRADE_DIRECTIONS[di]
        # 若该词有固定搭配，优先把它作为「升级原料」提示出来
        collocs = w.get("collocations") or []
        use_colloc = collocs[0].get("phrase", "") if collocs else ""
        reason = "你标了★" if w.get("focus") else (
            "这个词你错过，值得改好" if is_review else
            ("搭配多，升级空间大" if len(collocs) >= 2 else "先用熟，再改好"))
        out.append({
            "i": i, "word": w.get("word"), "meaning": w.get("meaning") or "",
            "is_review": is_review,
            "direction": dname, "direction_desc": ddesc,
            "sample": dsample,
            "collocation": use_colloc,
            "reason": reason,
            "task": f"把「{_display(w)}」那句升级：{ddesc}"
                    + (f"（可用搭配：{use_colloc}）" if use_colloc else "")
                    + (f"（语法：{grammar}）" if grammar else ""),
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

    combos, ri, ni = [], 0, 0
    for gi in range(n):
        group = []
        # 每组尽量带 1 个复习词（复习词靠组合表达来过，不再单独出基础句）
        if ri < len(review_words):
            group.append({"word": review_words[ri].get("word"),
                          "meaning": review_words[ri].get("meaning") or "",
                          "review": True})
            ri += 1
        while len(group) < per and ni < len(new_words):
            group.append({"word": new_words[ni].get("word"),
                          "meaning": new_words[ni].get("meaning") or "",
                          "review": False})
            ni += 1
        if not group:
            # 词不够：允许复读已用过的词（重复本身就是复习）
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

        # 场景：用组内首个词的功能分类作为表达框架
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
                        seed_date=None, n_upgrade=5, n_combo=10):
    """三段式造句计划。返回 {basic, upgrade, combo, meta}。"""
    if seed_date is None:
        seed_date = date.today()
    seed = abs(hash(f"{stage}-{week}-{day}-{seed_date.isoformat()}")) % (10 ** 6)

    # ③ 造句五星：达到 5 星的词「主动输出已稳定」，不再进入常规造句计划
    # （基础句不再出它；升级/组合也不强制编排它）。SRS 是否复习由各自逻辑决定。
    cand_words = [w.get("word") for w in (today_new or [])] + \
                 [w.get("word") for w in (due_vocab or [])]
    starred = srs.stars_map(cand_words)
    five_star = {w for w, s in starred.items() if s >= 5}
    if five_star:
        today_new = [w for w in (today_new or [])
                     if w.get("word", "").strip().lower() not in five_star]
        due_vocab = [w for w in (due_vocab or [])
                     if w.get("word", "").strip().lower() not in five_star]

    basic = _build_basic(today_new, grammar)
    upgrade = _build_upgrade(today_new, due_vocab, grammar, seed, n_upgrade)
    combo = _build_combos(today_new, due_vocab, grammar, seed, n_combo)

    return {
        "basic": basic,
        "upgrade": upgrade,
        "combo": combo,
        "meta": {
            "basic_count": len(basic),
            "upgrade_count": len(upgrade),
            "combo_count": len(combo),
            "review_count": len(due_vocab or []),
            "grammar": grammar,
            "note": "①基础每词一句 ②5句升级(给方向+示范) ③10组组合(复习词混在这里，不强制长度)",
        },
    }
