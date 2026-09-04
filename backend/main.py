"""English OS - 个人英语学习 OS 后端入口（纯本地，无 AI）。"""
import hmac
import json
import re
from datetime import date
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import os

from db import init_db, get_conn, ts, today_str, STAGES
import services as svc
import srs
from ai_service import (correct_sentence, ERROR_TYPES, attempts_of,
                        today_attempts, analyze)
import fileimport

app = FastAPI(title="English OS")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- 访问口令（公网部署保护） ----------
# 不设 EOS_TOKEN → 完全开放（本地个人使用，run.sh 默认如此）。
# 设了 EOS_TOKEN → 所有 /api/* 接口都要求携带口令，否则 401。
# 口令通过请求头 X-Auth-Token 或 Authorization: Bearer <token> 传递。
ACCESS_TOKEN = (os.environ.get("EOS_TOKEN") or "").strip()
# 健康检查/保活不鉴权：Render 的 healthCheck 与 UptimeRobot 无法带自定义头
PUBLIC_API_PATHS = {"/api/health"}


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """公网部署时防止任何人随意读写你的学习数据。"""

    async def dispatch(self, request, call_next):
        if not ACCESS_TOKEN:
            return await call_next(request)          # 本地模式：不鉴权
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            return await call_next(request)
        token = (request.headers.get("x-auth-token") or "").strip()
        if not token:
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not hmac.compare_digest(token, ACCESS_TOKEN):
            return JSONResponse(
                {"ok": False, "error": "未授权：缺少或错误的访问口令（EOS_TOKEN）。"},
                status_code=401)
        return await call_next(request)


# 注意顺序：CORS 先注册（外层，负责 OPTIONS 预检），鉴权后注册（内层）
app.add_middleware(TokenAuthMiddleware)

init_db()


# ---------- 健康检查（不鉴权，供 Render / 保活用） ----------
@app.get("/api/health")
def health():
    return {"ok": True, "auth_required": bool(ACCESS_TOKEN)}


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
            # 归一化：老词条只有 collocation 字符串，不归一化的话搭配区会空白
            "collocations": svc.normalize_collocations(w),
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
        # 搭配可能以数组(collocations)或老的单数字符串(collocation)传来，统一处理
        colloc = svc.collocation_text(body)
        srs.schedule_review(conn, "vocab", key, f"回忆并造句使用：{key}",
                            (key + " " + colloc).strip(),
                            p["stage"], p["week"], p["day"])
        conn.execute(
            "INSERT INTO history (date, stage, week, day, action, detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (today_str(), p["stage"], p["week"], p["day"], "learn_vocab", key, ts()))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- 造句 + 本地规则批改（无任何 AI 参与） ----------
@app.post("/api/sentence/check")
def sentence_check(body: dict):
    """提交一句造句，本地规则批改并入库。

    每次作答都在 sentences 表追加一行（attempt 递增），绝不覆盖上一次答案。
    判定有错才进错题本；写对了只把该词此前的错题标为「已改正」，不删除。
    """
    p = svc.get_progress()
    text = (body.get("sentence") or "").strip()
    if not text:
        return {"error": "句子不能为空"}
    word = (body.get("word") or "").strip()
    task_key = (body.get("task_key") or "").strip()
    grammar = (body.get("grammar") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    result = correct_sentence(text, p["stage"], p["week"], p["day"],
                              word, task_key, grammar, prompt)
    return result


@app.get("/api/sentence/attempts")
def sentence_attempts(stage: int = None, week: int = None, day: int = None):
    """当天全部造句作答，按题目分组返回（刷新页面后回填历史，含每次尝试）。"""
    p = svc.get_progress()
    st = p["stage"] if stage is None else stage
    wk = p["week"] if week is None else week
    dy = p["day"] if day is None else day
    conn = get_conn()
    groups = today_attempts(conn, st, wk, dy, today_str())
    conn.close()
    return {"stage": st, "week": wk, "day": dy, "groups": groups}


@app.get("/api/sentence/attempts/{task_key:path}")
def sentence_attempts_one(task_key: str):
    """单道题的全部作答历史。"""
    p = svc.get_progress()
    conn = get_conn()
    items = attempts_of(conn, p["stage"], p["week"], p["day"], task_key)
    conn.close()
    return {"task_key": task_key, "attempts": items}


@app.get("/api/sentence/history")
def sentence_history(limit: int = 300):
    """跨天可查的全部造句记录（修复「昨天造的句子看不见」）。

    不按当天 stage/week/day 过滤，直接按时间倒序返回全部作答，
    让用户随时回看自己写过的每一句。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, stage, week, day, word, task_key, attempt, original,"
        " corrected, score, verdict, good, error_type, created_at"
        " FROM sentences ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    items = [{
        "id": r["id"], "stage": r["stage"], "week": r["week"], "day": r["day"],
        "word": r["word"] or "", "task_key": r["task_key"] or "",
        "attempt": r["attempt"], "sentence": r["original"],
        "corrected": r["corrected"], "score": r["score"],
        "verdict": r["verdict"] or ("正确" if r["good"] else "有错误"),
        "ok": bool(r["good"]), "error_type": r["error_type"],
        "created_at": r["created_at"],
    } for r in rows]
    return {"items": items}


@app.post("/api/sentence/preview")
def sentence_preview(body: dict):
    """只看批改结果，不入库（用于前端「重新作答」时的实时预览）。"""
    text = (body.get("sentence") or "").strip()
    if not text:
        return {"error": "句子不能为空"}
    word = (body.get("word") or "").strip()
    grammar = (body.get("grammar") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    res = analyze(text, word, grammar, prompt)
    if res is None:
        return {"error": "无法识别该句子"}
    return res


# ---------- 错题本（只存真错，正确句永不进入） ----------
@app.get("/api/error-bank")
def error_bank(word: str = "", only_unfixed: int = 0):
    """错题本列表。按词聚合，保留每一次首次错误记录（改正只标记不删除）。"""
    conn = get_conn()
    sql = ("SELECT id, error_type, original, corrected, explanation, source,"
           " word, task_key, error_text, sentence_text, times, first_at,"
           " last_at, fixed, fixed_at, created_at"
           " FROM errors WHERE source='sentence'")
    args = []
    if word:
        sql += " AND word=?"
        args.append(word.strip())
    if only_unfixed:
        sql += " AND fixed=0"
    sql += " ORDER BY (fixed=0) DESC, last_at DESC, id DESC"
    rows = conn.execute(sql, tuple(args)).fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows],
            "total": len(rows),
            "unfixed": sum(1 for r in rows if not r["fixed"])}


# ---------- 复习 ----------
@app.get("/api/review/due")
def review_due():
    return {"items": srs.due_reviews(), "summary": srs.due_summary()}


@app.post("/api/review/submit")
def review_submit(body: dict):
    """提交一次复习结果。

    quality 三档（来自闪卡的「忘记 / 模糊 / 记得」）：
      0 = 忘记 → 间隔重排明天、reps 清零、记一次错
      3 = 模糊 → 算答对但间隔折半（记得不牢 → 更快再见）
      5 = 记得 → SM-2 正常递增（1→3→7→×ease）

    不传 quality 时按旧的 correct 布尔处理，保证老调用零回归。
    """
    review_id = body["id"]
    correct = bool(body.get("correct"))
    quality = body.get("quality")
    return srs.submit_review(review_id, correct, quality)


@app.get("/api/review/flashcards")
def review_flashcards():
    """② 今日复习闪卡数据源：只返回单词词义 SRS 到期卡（kind='vocab'）。

    与 /api/review/due 的区别：本接口严格按 kind 过滤，
    保证错误卡（归「薄弱项」）与听力卡（归听力页）不会混进单词闪卡。

    返回项已附带词典音标/词性/中文释义，供翻牌后展示。
    """
    return {"items": srs.flashcard_items()}


@app.get("/api/memory/state/{word}")
def memory_state(word: str):
    """① 三状态隔离读取：同一个词的三个学习维度彼此独立返回。

    - srs      : 词义记忆（reviews kind='vocab'）
    - output   : 主动输出五星（word_output 表，不进 SRS）
    - listening: 听力识别（reviews kind='listening'）

    允许出现「词义 SRS 稳定 + 五星 ★★☆☆☆ + 听力弱」这种组合，
    三个维度互不覆盖、互不重置。
    """
    conn = get_conn()
    vocab = conn.execute(
        "SELECT * FROM reviews WHERE kind='vocab' AND ref_key=?", (word,)).fetchone()
    listening = conn.execute(
        "SELECT * FROM reviews WHERE kind='listening' AND ref_key=?", (word,)).fetchone()
    out = conn.execute(
        "SELECT * FROM word_output WHERE word=?", (word,)).fetchone()
    conn.close()
    return {
        "word": word,
        "srs": dict(vocab) if vocab else None,
        "output": dict(out) if out else None,
        "listening": dict(listening) if listening else None,
    }


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


@app.post("/api/dict/lookup")
def dict_lookup(body: dict = None):
    """欧陆式点词查义：从本地词典(ECDICT)返回音标/词性/中文释义。无 AI、无外部调用。"""
    word = ((body or {}).get("word", "") or "").strip()
    if not word or len(word) > 60:
        return {"found": False, "word": word}
    r = svc.lookup_word(word)
    if not r:
        return {"found": False, "word": word}
    return {
        "found": True,
        "word": r.get("word", word),
        "phonetic": r.get("phonetic", "") or "",
        "pos": r.get("pos", "") or "",
        "meaning": r.get("meaning", "") or "",
    }


@app.get("/api/dbinfo")
def dbinfo():
    """只读诊断：确认当前连的是 Neon(PostgreSQL) 还是本地 SQLite。

    部署后打开 /api/dbinfo 即可一眼确认数据写进了哪里：
    engine=postgresql 才说明数据在 Neon；若显示 sqlite，说明 DATABASE_URL 没生效，
    此时数据写在 Render 本地磁盘，重启/重新部署后会丢失。
    """
    import db as _db
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM dictionary").fetchone()[0]
        themed = conn.execute(
            "SELECT COUNT(*) FROM dictionary WHERE theme!=''").fetchone()[0]
    except Exception:
        total = themed = -1
    finally:
        conn.close()
    return {
        "engine": "postgresql" if _db._using_pg() else "sqlite",
        "target": _db._mask_url(os.environ.get("DATABASE_URL")) if _db._using_pg() else _db.DB_PATH,
        "using_pg": bool(_db._using_pg()),
        "dictionary_words": total,
        "themed_words": themed,
    }


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
