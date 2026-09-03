"""English OS - 个人英语学习 OS 后端入口（纯本地，无 AI）。"""
import json
import re
from datetime import date
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

from db import init_db, get_conn, ts, today_str, STAGES
import services as svc
import srs
from ai_service import correct_sentence, ERROR_TYPES
import fileimport

app = FastAPI(title="English OS")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

init_db()


# ---------- 主页 ----------
@app.get("/api/home")
def home():
    return svc.home_overview()


@app.get("/api/stages")
def stages():
    return STAGES


# ---------- 进度 ----------
@app.get("/api/progress")
def progress():
    return svc.get_progress()


@app.post("/api/progress")
def set_progress(body: dict):
    return svc.set_progress(
        body.get("stage", 0), body.get("week", 1), body.get("day", 1),
        body.get("last_activity"))


# ---------- 每周内容 ----------
@app.get("/api/week/{stage}/{week}")
def get_week(stage: int, week: int):
    return svc.get_week(stage, week)


@app.post("/api/week")
def save_week(body: dict):
    return svc.update_week(
        body["stage"], body["week"],
        title=body.get("title"), grammar=body.get("grammar"),
        topics=body.get("topics"), vocab=body.get("vocab"))


# ---------- 今日学习（主线） ----------
@app.get("/api/today")
def today():
    """今日主线数据：20词 + 10句造句引导 + 今日语法 + 口语。
    若本周无内容则从本地词库自动填充，保证随时有内容可学。"""
    p = svc.get_progress()
    weekdata = svc.ensure_week_content(p["stage"], p["week"])
    week = weekdata or {"title": "未设置", "grammar": "", "vocab": [], "theme": ""}
    conn = get_conn()

    # 词汇：取本周 vocab（自动填充或用户导入），按 Day 分组：
    # 若词汇带有 day 归属(如一次性导入多组)，只展示"当前 Day"对应的一组；
    # 否则(旧内容无 day 字段)整体展示。
    vocab_all = week["vocab"] or []
    has_day = any(v.get("day") for v in vocab_all)
    if has_day:
        vocab = [v for v in vocab_all if int(v.get("day", p["day"])) == p["day"]]
        if not vocab:  # 某天无内容则回退到第一天或全量，保证有可学
            vocab = [v for v in vocab_all if int(v.get("day", 1)) == 1] or vocab_all
    else:
        vocab = vocab_all
    # 记录今日词掌握状态
    day_items = {}
    for w in vocab:
        key = (w.get("word") or "").strip()
        if not key:
            continue
        row = conn.execute(
            "SELECT * FROM day_items WHERE stage=? AND week=? AND day=? AND kind='vocab' AND ref_key=?",
            (p["stage"], p["week"], p["day"], key)).fetchone()
        mastered = row["mastered"] if row else 0
        # 兼容两种内容形态：
        #  A) 富文本导入(块状)：examples=[{sentence,translation}], collocations=[{phrase,meaning}]
        #  B) 早期/自动填充：example+translation 字符串
        examples = w.get("examples") or []
        if not examples and (w.get("example") or w.get("extra_examples")):
            first = w.get("example") or ""
            extras = w.get("extra_examples") or []
            examples = [{"sentence": first, "translation": w.get("translation", "")}] \
                if first else []
            examples += [{"sentence": s, "translation": ""} for s in extras]
        _pos = w.get("pos", "")
        _ph = w.get("phonetic", "")
        # 导入词常缺词性/音标：从全量词典补（只读补全，不落库，不影响合并/导入逻辑）
        if not _pos or not _ph:
            drow = conn.execute(
                "SELECT pos, phonetic FROM dictionary WHERE word=?", (key,)).fetchone()
            if drow:
                _pos = _pos or (drow["pos"] or "")
                _ph = _ph or (drow["phonetic"] or "")
        day_items[key] = {
            "word": key, "meaning": w.get("meaning", ""), "pos": _pos,
            "phonetic": _ph,
            "collocations": w.get("collocations") or [],
            "examples": examples,
            "ex_source": w.get("ex_source", ""),
            "day": w.get("day", 1), "group_name": w.get("group_name", ""),
            "mastered": mastered,
        }
    conn.close()

    # Day7 = 休息日：不学新词、不出造句任务，只做周测/自由复习
    is_rest_day = (int(p["day"]) == 7)

    # 造句引导：三段式（①每词一句 ②5句升级 ③10组组合，复习词混进组合）
    grammar = week["grammar"]
    due_vocab = srs.due_vocab_words()
    if is_rest_day:
        plan = {"basic": [], "upgrade": [], "combo": [],
                "meta": {"basic_count": 0, "upgrade_count": 0, "combo_count": 0,
                         "review_count": len(due_vocab or []), "grammar": grammar,
                         "note": "Day7 休息日"}}
    else:
        plan = svc.build_sentence_plan(
            vocab, due_vocab, grammar,
            p["stage"], p["week"], p["day"]
        )
    # 兼容旧字段：把基础句的任务文本拍平，老逻辑/老页面仍可读
    prompts = [b["task"] for b in plan["basic"]]
    # 汇总分组信息（用于展示"第1组｜职场… · 今日20词 / 本周共120词"）
    week_total = len(vocab_all)
    cur_group_name = ""
    if has_day:
        _matching = [v for v in vocab_all if int(v.get("day", p["day"])) == p["day"]]
        cur_group_name = (_matching[0].get("group_name", "") if _matching else "")
    return {
        "progress": p,
        "week_title": week["title"],
        "grammar": grammar,
        "theme": week.get("theme", ""),
        "words": [] if is_rest_day else list(day_items.values()),
        "day_group": {"index": p["day"], "name": cur_group_name},
        "week_total_words": week_total,
        "is_rest_day": is_rest_day,
        "sentence_prompts": prompts[:10],
        "sentence_plan": plan,
        "review_pool": due_vocab,
        "sentence_meta": {"count": len(prompts), "grammar": grammar},
        "speaking_topic": f"用英语连续说 2 分钟：介绍你的{week['title']}（用上本周语法：{grammar}）",
    }


# ---------- 词汇掌握 ----------
@app.post("/api/word/master")
def word_master(body: dict):
    """标记某个词掌握，并安排其进入 SRS 复习。"""
    p = svc.get_progress()
    conn = get_conn()
    key = body["word"]
    mastered = body.get("mastered", 1)
    row = conn.execute(
        "SELECT * FROM day_items WHERE stage=? AND week=? AND day=? AND kind='vocab' AND ref_key=?",
        (p["stage"], p["week"], p["day"], key)).fetchone()
    if row:
        conn.execute("UPDATE day_items SET mastered=? WHERE id=?", (mastered, row["id"]))
    else:
        conn.execute(
            "INSERT INTO day_items (stage, week, day, kind, ref_key, payload_json, mastered, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (p["stage"], p["week"], p["day"], "vocab", key, "{}", mastered, ts()))
    # 学习过的词进入复习队列（明天首查）
    if mastered == 2:
        word = body.get("meaning", key)
        srs.schedule_review(conn, "vocab", key, f"回忆并造句使用：{key}",
                            key + " " + body.get("collocation", ""),
                            p["stage"], p["week"], p["day"])
        conn.execute(
            "INSERT INTO history (date, stage, week, day, action, detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (today_str(), p["stage"], p["week"], p["day"], "learn_vocab", key, ts()))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- 造句 + AI 批改 ----------
@app.post("/api/sentence/check")
def sentence_check(body: dict):
    p = svc.get_progress()
    text = body.get("sentence", "").strip()
    if not text:
        return {"error": "句子不能为空"}
    result = correct_sentence(text, p["stage"], p["week"], p["day"])
    return result


# ---------- 复习 ----------
@app.get("/api/review/due")
def review_due():
    return {"items": srs.due_reviews(), "summary": srs.due_summary()}


@app.post("/api/review/submit")
def review_submit(body: dict):
    review_id = body["id"]
    correct = bool(body.get("correct"))
    return srs.submit_review(review_id, correct)


# ---------- 错误 / 薄弱项 ----------
@app.get("/api/errors")
def errors_breakdown():
    return svc.error_breakdown()


@app.get("/api/errors/{error_type}")
def error_detail(error_type: str):
    return svc.error_detail(error_type)


@app.get("/api/error-types")
def error_types():
    return ERROR_TYPES


# ---------- 周测 ----------
@app.get("/api/quiz/{stage}/{week}")
def quiz_get(stage: int, week: int):
    return svc.build_weekly_quiz(stage, week)


@app.post("/api/quiz/grade")
def quiz_grade(body: dict):
    return svc.grade_quiz(
        int(body.get("stage", 0)), int(body.get("week", 1)), body.get("answers", {}))


# ---------- 本地词库（无 AI）----------
@app.get("/api/dict/count")
def dict_count():
    return {"count": svc._dictionary_count()}


@app.get("/api/lookup/{word}")
def lookup(word: str):
    """查一个词的中文/词性/例句/搭配。"""
    r = svc.lookup_word(word)
    if not r:
        return {"found": False, "word": word}
    r["found"] = True
    return r


@app.get("/api/week/{stage}/{week}/ensure")
def week_ensure(stage: int, week: int):
    """确保某周有内容（自动填充），返回该周词汇。"""
    wd = svc.ensure_week_content(stage, week)
    if not wd:
        return {"ok": False, "error": "周不存在"}
    return {"ok": True, "title": wd["title"], "grammar": wd["grammar"],
            "theme": wd.get("theme"), "vocab_count": len(wd["vocab"]),
            "vocab": wd["vocab"]}


@app.post("/api/words/import")
def words_import(body: dict):
    """富文本导入整周/多组词汇。

    支持用户一次性粘贴「第N周｜主题｜N词 + 第N组｜分组名 + word—中文(+例句行)」，
    自动识别周号与分组(=每天)，生词写回词库并自动补例句。纯本地无 AI。
    """
    import weekimport
    text = body.get("text", "") or ""
    forced_week = body.get("week")
    forced_stage = body.get("stage")
    merge = body.get("merge") or False  # 分批导入：只替换本次涉及的天，保留其它天
    if not text.strip():
        return {"ok": False, "error": "内容为空，请先粘贴要导入的词汇。"}
    fn = weekimport.import_rich_week_merge if merge else weekimport.import_rich_week
    return fn(
        text,
        forced_stage=int(forced_stage) if forced_stage is not None else None,
        forced_week=int(forced_week) if forced_week is not None else None,
    )


# ---------- 文件上传导入（docx/pdf/txt/xlsx/html 等多格式） ----------
@app.post("/api/file/extract")
async def file_extract(file: UploadFile = File(...)):
    """上传文件 → 提取纯文本（供前端预览/编辑后再导入）。

    支持 docx（含"聊天粘贴型"换行结构）、pdf、txt/md/csv/json、
    html、rtf、xlsx 等，详见 fileimport.SUPPORTED_EXTS。
    """
    try:
        raw = await file.read()
        if len(raw) > fileimport.MAX_FILE_SIZE:
            return {"ok": False, "error": "文件超过 20MB，请拆分后上传。"}
        filename = file.filename or ""
        text, fmt, warnings = fileimport.extract_text(raw, filename)
        lines = [ln for ln in text.split("\n") if ln.strip()]
        return {
            "ok": True, "filename": filename, "format": fmt,
            "lines": len(lines), "chars": len(text), "text": text,
            "warnings": warnings,
        }
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"文件读取失败：{e}"}


@app.post("/api/words/import/file")
async def words_import_file(
    file: UploadFile = File(...),
    week: str = Form(None),
    stage: str = Form(None),
    merge: bool = Form(False),
):
    """上传文件并直接导入整周词汇（提取→解析→写库一步完成）。

    与 POST /api/words/import 相同逻辑，只是入口从"粘贴文本"换成"上传文件"。
    """
    import weekimport
    try:
        raw = await file.read()
        if len(raw) > fileimport.MAX_FILE_SIZE:
            return {"ok": False, "error": "文件超过 20MB，请拆分后上传。"}
        try:
            text, fmt, warnings = fileimport.extract_text(raw, file.filename or "")
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        fn = weekimport.import_rich_week_merge if merge else weekimport.import_rich_week
        result = fn(
            text,
            forced_stage=int(stage) if stage not in (None, "", "null") else None,
            forced_week=int(week) if week not in (None, "", "null") else None,
        )
        result["format"] = fmt
        if warnings:
            result.setdefault("warnings", []).extend(warnings)
        return result
    except Exception as e:
        return {"ok": False, "error": f"文件导入失败：{e}"}


@app.get("/api/file/formats")
def file_formats():
    """前端展示：支持的文件格式列表。"""
    return {"ok": True, "formats": fileimport.SUPPORTED_EXTS}


@app.post("/api/words/set")
def words_set(body: dict):
    """用户把一批英文单词设为当前周内容（自动从词库匹配中文/词性/例句/搭配）。"""
    raw = body.get("words", "") or ""
    stage = int(body.get("stage", 0))
    week = int(body.get("week", 1))
    # 支持逗号/空格/换行分隔
    tokens = []
    for chunk in re.split(r"[\s,，、]+", raw):
        chunk = chunk.strip()
        if chunk:
            tokens.append(chunk)
    result = svc.import_user_words(tokens, stage, week)
    matched = result["matched"]
    unmatched = result["unmatched"]
    if matched:
        # 组装词条存到该周
        vocab = [{
            "word": m["word"], "meaning": m["meaning"], "pos": m["pos"],
            "collocation": m["collocations"][0]["phrase"] if m["collocations"] else "",
            "example": m["examples"][0]["sentence"] if m["examples"] else "",
            "translation": m["examples"][0]["translation"] if m["examples"] else "",
        } for m in matched]
        svc.update_week(stage, week, vocab=vocab)
    return {"ok": True, "matched": len(matched),
            "unmatched": unmatched, "words": [m["word"] for m in matched]}


# ---------- 学习历史 ----------
@app.get("/api/history")
def history(limit: int = 100):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 静态前端 ----------
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(os.path.join(FRONTEND_DIR, "index.html")):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
