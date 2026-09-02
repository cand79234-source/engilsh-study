# -*- coding: utf-8 -*-
"""周词富文本导入 → 词库回填 + 写入目标周。

流程：
  1. 解析粘贴文本（importer.parse_import）
  2. 若标题含周号则定位目标周；否则沿用当前/调用方指定周
  3. 每个词：补词性/中文（缺省则尝试词库兜底或留空），缺例句则自动配模板句
  4. 生词写回本地词库 dictionary / example_sentences（source='user'/'autogen'）
  5. 整周按"组=天"写入 weeks.vocab_json（每词带 day 字段）
"""
import json
import re

from db import get_conn, ts
import services as svc


def auto_example(word):
    """无用户例句时为生词生成一句可直接朗读的占位句（纯本地，无 AI）。

    不依赖词性猜测（词性后缀并不可靠，猜错会生成病句）。统一用"把该词
    作为本周学习对象"的安全句式——语法永不出错、可朗读、能套用该词，
    仅作内容占位，用户之后可用自带例句覆盖。
    """
    w = word.strip()
    frames = [
        "Let’s learn the word “{W}” and use it in a sentence.",
        "I want to remember the word “{W}” from this week.",
        "We can practice using “{W}” when we talk about work.",
    ]
    i = len(w) % len(frames)
    return frames[i].replace("{W}", w)



def _bank_upsert(conn, word, meaning, pos):
    """把词写回 dictionary（幂等：已有则补全空字段，不覆盖用户已有）。"""
    cur = conn.execute("SELECT * FROM dictionary WHERE word=?", (word.lower(),))
    row = cur.fetchone()
    if row:
        new_pos = row["pos"] or pos
        new_mea = row["meaning"] or meaning
        if new_pos != row["pos"] or new_mea != row["meaning"]:
            conn.execute("UPDATE dictionary SET pos=?, meaning=? WHERE id=?",
                         (new_pos, new_mea, row["id"]))
        return row["id"]
    conn.execute(
        "INSERT INTO dictionary (word, meaning, pos, theme) VALUES (?,?,?,?)",
        (word.lower(), meaning, pos or "", ""))
    return conn.execute("SELECT id FROM dictionary WHERE word=?",
                        (word.lower(),)).fetchone()["id"]


def _bank_example(conn, word, sentence, translation="", source="user"):
    """把一句例句写回 example_sentences（幂等，按 word+sentence 去重，可带中译）。"""
    if not sentence or not sentence.strip():
        return False
    s = sentence.strip()
    dup = conn.execute("SELECT 1 FROM example_sentences WHERE word=? AND sentence=?",
                       (word.lower(), s)).fetchone()
    if dup:
        if translation:
            conn.execute(
                "UPDATE example_sentences SET translation=COALESCE(NULLIF(translation,''),?) WHERE word=? AND sentence=?",
                (translation.strip(), word.lower(), s))
        return False
    conn.execute(
        "INSERT INTO example_sentences (word, sentence, translation, source, created_at) VALUES (?,?,?,?,?)",
        (word.lower(), s, (translation or "").strip(), source, ts()))
    return True


def _norm_examples(examples):
    """把例句规范成 [{"sentence":..., "translation":...}, ...]。兼容字符串或dict。"""
    out = []
    if not examples:
        return out
    for ex in examples:
        if isinstance(ex, dict):
            out.append({"sentence": (ex.get("sentence") or "").strip(),
                        "translation": (ex.get("translation") or "").strip()})
        else:
            s = str(ex).strip()
            if s:
                out.append({"sentence": s, "translation": ""})
    return [e for e in out if e["sentence"]]


def _norm_collocs(collocs):
    """固定搭配规范成 [{"phrase":..., "meaning":...}, ...]。兼容字符串或dict。"""
    out = []
    if not collocs:
        return out
    for c in collocs:
        if isinstance(c, dict):
            phrase = (c.get("phrase") or "").strip()
            if phrase:
                out.append({"phrase": phrase, "meaning": (c.get("meaning") or "").strip()})
        else:
            s = str(c).strip()
            if s:
                out.append({"phrase": s, "meaning": ""})
    return out


def _bank_colloc(conn, word, phrase, meaning):
    """把固定搭配写回 collocations 表（幂等）。"""
    phrase = phrase.strip()
    if not phrase:
        return False
    dup = conn.execute("SELECT 1 FROM collocations WHERE word=? AND phrase=?",
                       (word.lower(), phrase)).fetchone()
    if dup:
        return False
    conn.execute(
        "INSERT INTO collocations (word, phrase, meaning, example, source) VALUES (?,?,?,?,?)",
        (word.lower(), phrase, meaning or "", "", "user"))
    return True


def import_rich_week(text, forced_stage=None, forced_week=None):
    """主入口。返回导入结果摘要（供前端展示成功/警告/分组统计）。

    周号优先级：文档/文本里明确写了周号（如"第2周"）优先；
    没写才用调用方指定的 forced_week 兜底。
    """
    from importer import parse_import
    parsed = parse_import(text)
    week = parsed.get("week") or forced_week
    stage = forced_stage if forced_stage is not None else parsed.get("stage", 0)
    title = parsed.get("title", "")
    groups = parsed.get("groups", [])
    if not groups:
        return {"ok": False,
                "error": "没有识别到任何单词。请按「单词 — 中文」每行/每块一个单词粘贴，可含例句和固定搭配。",
                "parse": parsed}
    if not week:
        return {"ok": False,
                "error": "标题里没看到周号（如“第2周”）。可在开头写“第N周｜主题”，或指定要导入到第几周。",
                "parse": parsed}

    conn = get_conn()
    added_new = 0
    added_ex = 0
    added_col = 0
    autogen = 0
    # 词库已有词（用于补缺）
    known = {}
    for row in conn.execute("SELECT * FROM dictionary"):
        known[row["word"].lower()] = dict(row)

    new_vocab = []
    for g in groups:
        day = g["day"]
        for w in g["words"]:
            wl = w["word"].lower()
            meaning = w.get("meaning", "")
            pos = w.get("pos", "")
            if wl in known:
                meaning = meaning or known[wl].get("meaning", "")
                pos = pos or known[wl].get("pos", "")
            # 例句规范化：优先用户贴的(含中文)，否则词库已有，否则自动配占位句
            examples = _norm_examples(w.get("examples"))
            user_ex = bool(examples)
            if not examples and wl in known:
                ex_rows = conn.execute(
                    "SELECT sentence, translation FROM example_sentences WHERE word=? ORDER BY difficulty LIMIT 5",
                    (wl,)).fetchall()
                examples = [{"sentence": r["sentence"], "translation": r["translation"] or ""}
                            for r in ex_rows]
            if not examples:
                ex_sentence = auto_example(w["word"])
                examples = [{"sentence": ex_sentence, "translation": ""}]
                ex_src = "autogen"
                _bank_upsert(conn, w["word"], meaning, pos)
                _bank_example(conn, w["word"], ex_sentence, "", "autogen")
                autogen += 1
                added_new += 1
            else:
                ex_src = "user" if user_ex else "bank"
                # 确保写回词库（补全 meaning/pos），例句/搭配写回
                _bank_upsert(conn, w["word"], meaning, pos)
                if wl not in known:
                    added_new += 1
                for ex in examples:
                    if _bank_example(conn, w["word"], ex["sentence"], ex["translation"], ex_src):
                        added_ex += 1
            # 固定搭配写回词库
            collocs = _norm_collocs(w.get("collocations"))
            for c in collocs:
                if _bank_colloc(conn, w["word"], c["phrase"], c["meaning"]):
                    added_col += 1
            new_vocab.append({
                "day": day, "group_name": g["name"],
                "word": w["word"], "meaning": meaning, "pos": pos,
                "collocations": collocs,
                "examples": examples,
                "ex_source": ex_src,
            })
    conn.commit()
    conn.close()

    svc.update_week(stage, week, title=title, vocab=new_vocab)
    _move_progress_to(stage, week)
    total = len(new_vocab)
    day_counts = {}
    for v in new_vocab:
        day_counts[v["day"]] = day_counts.get(v["day"], 0) + 1
    return {
        "ok": True,
        "stage": stage, "week": week, "title": title,
        "total": total, "days": len(groups),
        "day_counts": day_counts,
        "added_new": added_new, "added_examples": added_ex,
        "added_collocations": added_col, "autogen": autogen,
        "warnings": parsed.get("warnings", []),
        "skipped": parsed.get("skipped", []),
        "words": [v["word"] for v in new_vocab],
    }


def _move_progress_to(stage, week, day=1):
    """导入成功后把当前学习位置切到导入的周（Day1）。

    这样无论前端跳转是否触发（旧版 confirm 被关掉、移动端弹框被忽略等），
    下一次刷新「今日任务」都会显示用户自己导入的词，而不是停留在某个
    内置预设周（如第3周 hobby/like/enjoy…）。这是「导入即生效」的兜底保障。
    """
    try:
        conn = get_conn()
        conn.execute(
            "UPDATE progress SET stage=?, week=?, day=?, updated_at=? WHERE id=1",
            (int(stage), int(week), int(day), ts()))
        conn.commit()
        conn.close()
    except Exception as e:
        print("[weekimport] 切进度失败:", e)


def _build_word_entry(conn, known, day, gname, w, counters):
    """处理单个词：补缺 + 例句/搭配规范化 + 写回词库。返回 vocab 词条。"""
    wl = w["word"].lower()
    meaning = w.get("meaning", "")
    pos = w.get("pos", "")
    if wl in known:
        meaning = meaning or known[wl].get("meaning", "")
        pos = pos or known[wl].get("pos", "")
    examples = _norm_examples(w.get("examples"))
    user_ex = bool(examples)
    ex_src = ""
    if not examples and wl in known:
        ex_rows = conn.execute(
            "SELECT sentence, translation FROM example_sentences WHERE word=? ORDER BY difficulty LIMIT 5",
            (wl,)).fetchall()
        examples = [{"sentence": r["sentence"], "translation": r["translation"] or ""}
                    for r in ex_rows]
    if not examples:
        ex_sentence = auto_example(w["word"])
        examples = [{"sentence": ex_sentence, "translation": ""}]
        ex_src = "autogen"
        _bank_upsert(conn, w["word"], meaning, pos)
        _bank_example(conn, w["word"], ex_sentence, "", "autogen")
        counters["autogen"] += 1
        counters["added_new"] += 1
    else:
        ex_src = "user" if user_ex else "bank"
        _bank_upsert(conn, w["word"], meaning, pos)
        if wl not in known:
            counters["added_new"] += 1
        for ex in examples:
            if _bank_example(conn, w["word"], ex["sentence"], ex["translation"], ex_src):
                counters["added_examples"] += 1
    collocs = _norm_collocs(w.get("collocations"))
    for c in collocs:
        if _bank_colloc(conn, w["word"], c["phrase"], c["meaning"]):
            counters["added_collocations"] += 1
    return {
        "day": day, "group_name": gname,
        "word": w["word"], "meaning": meaning, "pos": pos,
        "collocations": collocs,
        "examples": examples,
        "ex_source": ex_src,
        "focus": bool(w.get("focus")),   # ★ 用户标记的重点词 → 优先进入"升级"环节
    }


def import_rich_week_merge(text, forced_stage=None, forced_week=None):
    """把富文本按「天」合并进目标周：本次解析出的天(第N组)会替换该天旧词，
    其余天保持不变。适合分多次粘贴、逐步拼出整周的场景。

    周号优先级：文本里明确写了周号优先；没写才用 forced_week（如前端传的当前周）。
    """
    from importer import parse_import
    parsed = parse_import(text)
    week = parsed.get("week") or forced_week
    stage = forced_stage if forced_stage is not None else parsed.get("stage", 0)
    title = parsed.get("title", "")
    groups = parsed.get("groups") or []
    if not groups:
        return {"ok": False, "error": "没有识别到任何单词。", "parse": parsed}

    existing = svc.get_week(stage, week) or {"title": title, "vocab": []}
    base_vocab = existing.get("vocab") or []
    # 保留其它天的旧词，本次涉及的天将被替换
    touched_days = {g["day"] for g in groups}
    kept = [v for v in base_vocab if v.get("day") not in touched_days]

    conn = get_conn()
    known = {}
    for row in conn.execute("SELECT * FROM dictionary"):
        known[row["word"].lower()] = dict(row)
    counters = {"added_new": 0, "added_examples": 0,
                "added_collocations": 0, "autogen": 0}
    new_vocab = list(kept)
    for g in groups:
        day = g["day"]
        for w in g["words"]:
            new_vocab.append(_build_word_entry(conn, known, day, g["name"], w, counters))
    conn.commit()
    conn.close()

    # 覆盖此前的整周 vocab（合并后即为最终每周词汇）
    svc.update_week(stage, week, title=title or existing.get("title"), vocab=new_vocab)
    _move_progress_to(stage, week)
    # 统计各天词数。注意：kept 里可能混有旧版/自动填充的无 day 字段条目，
    # 用 get 防 KeyError（此前在"已有自动填充内容的周"上合并导入会 500）。
    day_counts = {}
    for v in new_vocab:
        d = v.get("day") or 0
        day_counts[d] = day_counts.get(d, 0) + 1
        if not v.get("day"):
            v["day"] = 0  # 未分组的旧词统一归 0，前端不会按天展示它们
    return {
        "ok": True, "stage": stage, "week": week,
        "title": title or existing.get("title"),
        "total": len(new_vocab), "days": len(day_counts),
        "day_counts": day_counts,
        "added_new": counters["added_new"],
        "added_examples": counters["added_examples"],
        "added_collocations": counters["added_collocations"],
        "autogen": counters["autogen"],
        "warnings": parsed.get("warnings", []),
        "skipped": parsed.get("skipped", []),
        "words": [v["word"] for v in new_vocab],
    }
