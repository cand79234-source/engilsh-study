"""English OS - 个人英语学习 OS 后端入口（纯本地，无 AI）。"""
import hmac
import json
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta
from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import os

from db import init_db, get_conn, ts, today_str, STAGES, insert_get_id, _using_pg, app_now
import services as svc
import srs
from ai_service import (correct_sentence, ERROR_TYPES, attempts_of,
                        today_attempts, analyze)
import fileimport
import report as _report
import training as _training
import link as _link
import ai_correct as _ai
import scenario as _scenario
import weakness as _weakness

app = FastAPI(title="English OS")

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
        # 跨域/JSON 预检直接放行：交给 CORS 中间件去回 ACAO 头，
        # 鉴权不要拦预检请求（前端不带口令的 OPTIONS 不该被 401 顶回去）
        if request.method == "OPTIONS":
            return await call_next(request)
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


# 中间件注册顺序：Starlette 是「后注册的先执行 / 离请求越近」。
# 我们要 CORS 处理 OPTIONS 预检（含 ACAO 头），所以 CORS 后注册。
# TokenAuth 早于 CORS 执行，但已对 OPTIONS 放行（见上面 dispatch），CORS 再补头。
app.add_middleware(TokenAuthMiddleware)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

init_db()


# ---------- 健康检查（不鉴权，供 Render / 保活用） ----------
@app.get("/api/health")
def health():
    # UptimeRobot 每 5 分钟戳一次这里做保活 → 顺手推进"按天补齐"。
    # 情景是 AI 生成的，导入时只保当天那批，后面几天的量靠这里慢慢补
    # （详见 scenario.backfill_step：今天那批优先，齐了再按 (week, day) 往后）。
    # spawn 到后台线程，不阻塞也不拖慢健康检查本身。
    _scenario.spawn(_scenario.backfill_step)
    return {"ok": True, "auth_required": bool(ACCESS_TOKEN)}


# ---------- AI 英语批改（豆包 / 火山方舟） ----------
# 密钥与模型都只读环境变量：ARK_API_KEY、ARK_MODEL（见 ai_correct.py 顶部说明）。
# 前端拿不到密钥，也永远不直接请求豆包；本接口只服务「英语批改」这一件事，
# 不参与每日任务、单词、学习计划、数据统计等其它功能。
@app.get("/api/ai/status")
def ai_status():
    """前端用它判断 AI 是否可用。只回布尔和模型名，不含任何密钥。"""
    lim = _ai.limits()
    return {"ok": True, "enabled": _ai.ai_enabled(), "model": _ai.model_name(),
            "min_chars": lim["min_chars"], "max_chars": lim["max_chars"]}


@app.post("/api/ai/correct")
def ai_correct_api(body: dict, request: Request):
    """把用户写的英文交给豆包批改。返回：原文/修改后/错误清单/更自然的说法/总评。"""
    body = body or {}
    text = body.get("text") or ""
    ctx = str(body.get("context") or "")[:60]     # 可选：本周语法重点，仅作参考
    ip = (request.client.host if request.client else "") or ""
    data, err = _ai.correct(text, client_ip=ip, context=ctx)
    if err:
        # 统一用 200 + ok:false：err 已是友好中文，前端直接 toast 即可
        return JSONResponse({"ok": False, "error": err})
    return dict(ok=True, **data)


# ---------- 隐身学习模式（碎片时间：当天词 → 搭配 → 例句 → 造句 → AI 批改） ----------
# 只新增「学习入口」，不新建词库/进度/统计：
#   词   —— 直接调上面的 today()，与正常学习页同一个来源
#   完成 —— 前端调现有的 /api/word/master，写同一张 day_items 表
#   造句 —— 走现有 correct_sentence，写同一张 sentences / word_output / errors
# 所以：正常页面学完的词，隐身模式不会再问；隐身模式学完的，正常页面也认。
@app.get("/api/stealth/next")
def stealth_next():
    """取当天还没完成的第一个词（含搭配、例句，以及**已预生成好的情景**）。

    情景（设计方案 §2）在「导入时」就已批量生成、「点记住了」时兜底补生成，
    所以这里只按 used_count 轮换取一条，**不做懒生成**
    —— 保证用户打开造句时「先看到场景，再写第一句」，而不是写完才给场景。
    """
    t = today()                      # 复用现有今日数据，保证两个入口同一份
    words = t.get("words") or []
    if t.get("is_rest_day"):
        return {"ok": True, "done": True, "rest_day": True, "message": "今天是 Day7 休息日，没有新词。"}
    pending = [w for w in words if (w.get("mastered") or 0) < 2 and w.get("word")]
    done_n = len(words) - len(pending)
    prog = {"done": done_n, "total": len(words)}
    if not pending:
        return {"ok": True, "done": True, "progress": prog, "message": "今天的词都过完了"}
    w = pending[0]
    grammar = t.get("grammar") or ""
    sc = _scenario.with_label(_scenario.pick(w.get("word") or ""))
    if sc:
        # 该词哪一层缺就补哪一层（不阻塞本次返回）
        _scenario.refill_if_low(w.get("word") or "", grammar)
    return {"ok": True, "done": False, "word": w, "scenario": sc,
            "grammar": grammar, "progress": prog}


@app.get("/api/scenario/next")
def scenario_next(word: str = "", cur: int = 0, tier: str = ""):
    """🔁 换场景：取同一个词的下一条情景（**只从库存轮换，不现场生成**，§2.5）。

    任何分数都能点，点一次换一条；cur = 当前正在看的情景 id（用来跳过它）。
    tier = 按题型分层取景（basic=small / combo=large），
    该层级没库存时自动退回不过滤。库存见底时后台补池，前端无感。
    """
    w = (word or "").strip()
    if not w:
        return JSONResponse({"ok": False, "error": "缺少参数 word"})
    sc = _scenario.with_label(_scenario.pick(w, exclude_id=cur, tier=tier))
    if not sc:
        # 这个词还没有该层的情景 —— 顺手推进一步"按天补齐"（不阻塞本次返回）。
        # 注意：**只有真的一条都没有时**才在这里补，正常取景（看一眼）绝不触发，
        # 避免"刷一下页面就在后台烧一堆 token"。
        _scenario.spawn(_scenario.backfill_step)
        return {"ok": True, "scenario": None,
                "message": "这个词还没有情景，正在后台生成，稍后点 🔁 再看"}
    _scenario.refill_if_low(w)
    return {"ok": True, "scenario": sc}


@app.post("/api/stealth/submit")
def stealth_submit(body: dict, request: Request):
    """提交一句造句：先本地规则批改入库，再让豆包批改给反馈。"""
    body = body or {}
    word = (body.get("word") or "").strip()
    text = (body.get("sentence") or "").strip()
    if not word or not text:
        return JSONResponse({"ok": False, "error": "先写一个句子再提交"})
    if len(text) > 1000:
        return JSONResponse({"ok": False, "error": "句子太长了，请控制在 1000 字符以内"})
    p = svc.get_progress()
    grammar = (body.get("grammar") or "").strip()
    ip = (request.client.host if request.client else "") or ""
    # ① 本地规则批改：入库 sentences、更新五星、进错题本 —— 与正常造句页完全同一套
    local = {}
    try:
        local = correct_sentence(text, p["stage"], p["week"], p["day"], word,
                                 "stealth:" + word, grammar,
                                 "用「%s」造句" % word) or {}
    except Exception as e:
        print("[stealth] 本地批改失败（已跳过）: %s" % e)
    # ② 豆包批改（可选）。ai=0 时完全不调豆包：不花额度、不用等、也不触发冷却，
    #    连点提交下一个词也不会被限流挡住。① 已入库的记录不受影响。
    ai = None
    err = None
    if body.get("ai", 1):
        ai, err = _ai.correct(text, client_ip=ip, context=grammar, word=word)

    # ③ 错误标签入库 + 薄弱项触发判定（设计方案 §4）
    #    AI 模式用 AI 给的 error_tags；AI 没跑通就用本地规则的错误类型顶上，
    #    这样没配 Key / 断网时「反复同错」的统计也不会断。
    tags = []
    if ai and not err:
        tags = list(ai.get("error_tags") or [])
    elif not ai:
        tags = [e.get("type") for e in _stealth_local_errors(local) if e.get("type")]
    if tags:
        _weakness.record(word, tags[:8])
    pend = _weakness.pending(word)

    return {"ok": True, "ai": ai, "ai_error": err,
            "weakness": ({"word": word, "tag": pend["tag"], "hits": pend["hits"],
                          "explain": _weakness.cached(word, pend["tag"])}
                         if pend else None),
            "local": {"score": local.get("score"), "verdict": local.get("verdict"),
                      "ok": local.get("ok"), "corrected": local.get("corrected", ""),
                      "level": local.get("level", ""),
                      "explanation": local.get("explanation", ""),
                      # 本地错误字段是 where/correct/explanation，
                      # 这里归一化成和豆包一样的 wrong/right/explain，前端一套渲染通吃
                      "errors": _stealth_local_errors(local)}}


@app.get("/api/weakness/explain")
def weakness_explain(word: str = "", tag: str = "", hits: int = 0):
    """薄弱项 AI 讲解（§4）：同一个错误反复出现时，让 mini 用 ≤2 句中文讲清根因。

    同一个「词 + 标签」只生成一次（讲过就落库，之后直接命中，不重复烧 token）。
    没到触发阈值（同一错 ≥3 次）时不讲，返回空文本 —— 偶发错误不值得打扰。
    """
    text, err = _weakness.explain(word, tag, hits)
    if err:
        return JSONResponse({"ok": False, "error": err})
    return {"ok": True, "explain": text}


def _stealth_local_errors(local):
    """本地规则的错误明细 → 统一格式 [{type, wrong, right, explain}]。"""
    out = []
    for e in (local.get("errors") or []):
        out.append({"type": e.get("type") or "其它",
                    "wrong": e.get("where") or "",
                    "right": e.get("correct") or "",
                    "explain": e.get("explanation") or ""})
    for i in (local.get("task_issues") or []):
        out.append({"type": i.get("type") or "任务要求",
                    "wrong": i.get("where") or "",
                    "right": i.get("correct") or "",
                    "explain": i.get("explanation") or ""})
    return out


@app.get("/stealth")
def stealth_page():
    """隐身学习独立小窗口页面（自带黑白样式，不动现有页面任何配色）。"""
    f = os.path.join(FRONTEND_DIR, "stealth.html")
    if os.path.exists(f):
        return FileResponse(f)
    return JSONResponse({"ok": False, "error": "stealth.html 不存在"})


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
    # day 字段可能缺失 / 为 null / 为字符串数字，统一安全取值。
    # （v.get("day", 默认) 只在 key 不存在时给默认；key 存在但值是 null 时
    #   返回 None，int(None) 会炸 —— 这里用 or 兜住。）
    def _day_of(v, default):
        try:
            return int(v.get("day") or default)
        except (TypeError, ValueError):
            return int(default)

    has_day = any(v.get("day") for v in vocab_all)
    if has_day:
        vocab = [v for v in vocab_all if _day_of(v, p["day"]) == p["day"]]
        if not vocab:  # 某天无内容则回退到第一天或全量，保证有可学
            vocab = [v for v in vocab_all if _day_of(v, 1) == 1] or vocab_all
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

    # 造句引导：两段式（①每词一句 ②10组组合，复习词混进组合）
    # ② 升级已于 2026-09-10 删除 —— 这里的休息日空计划也同步去掉 upgrade 字段。
    grammar = week["grammar"]
    due_vocab = srs.due_vocab_words()
    if is_rest_day:
        plan = {"basic": [], "combo": [],
                "meta": {"basic_count": 0, "combo_count": 0,
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
    # 情景兜底生成（设计方案 §2.1 触发点②）：
    # 学完一个词点「记住了」时，若它还没有情景就后台补一批，
    # 确保用户之后去造句时「先有场景，再写第一句」。已生成的词自动跳过。
    if mastered == 2:
        try:
            _scenario.ensure_for_word_bg(key)
        except Exception as e:
            print("[scenario] 记住了兜底生成失败:", e)
    return {"ok": True}


# ---------- 造句 + 本地规则批改（无任何 AI 参与） ----------
# 编译好的正则缓存。原来是普通 dict，长期运行会被用户造过的每个词永久占住一格；
# 这里改成有上限的 LRU（超出就丢最久没用的），缓存的是可重建的编译结果，丢掉不影响正确性。
_WORD_RE_CACHE = OrderedDict()
_WORD_RE_CACHE_MAX = 2000


def _word_used_in(sentence, word):
    """判断某个词是否真的出现在用户写的句子里。

    用「词边界 + 容忍常见屈折后缀」匹配：
      - work 能命中 work / works / worked / working
      - 但不会命中 network（有词边界，\b 已排除）
    目的：组合题只有用户真正用到的词才该 ±1 星，没用到的不动。
    """
    w = (word or "").strip().lower().strip(".,!?;:\"'()")
    if not w or not sentence:
        return False
    pat = _WORD_RE_CACHE.get(w)
    if pat is None:
        # 只容忍真正的屈折变化：复数/三单 -s -es，过去 -ed，进行 -ing。
        # 不放 -ly：real 会误命中 really（副词派生是另一个词，不算用到了 real）。
        # 词根至少 3 字母才允许后缀，避免 "go" 之类过短词乱匹配。
        if len(w) >= 3:
            pat = re.compile(r"\b" + re.escape(w) + r"(?:s|es|ed|ing)?\b", re.I)
        else:
            pat = re.compile(r"\b" + re.escape(w) + r"\b", re.I)
        _WORD_RE_CACHE[w] = pat
        if len(_WORD_RE_CACHE) > _WORD_RE_CACHE_MAX:
            _WORD_RE_CACHE.popitem(last=False)      # 丢最久未使用的
    else:
        _WORD_RE_CACHE.move_to_end(w)
    return bool(pat.search(sentence))


@app.post("/api/sentence/check")
def sentence_check(body: dict, request: Request):
    """提交一句造句，本地规则批改并入库；配置了 ARK_API_KEY 时再由豆包 AI 批改一次。

    每次作答都在 sentences 表追加一行（attempt 递增），绝不覆盖上一次答案。
    判定有错才进错题本；写对了只把该词此前的错题标为「已改正」，不删除。
    AI 只负责批改（错误标签进薄弱项统计 weak_hits），不改写 sentences / errors 的本地入库链路；
    没配 Key / AI 失败时前端拿到的就是原来的本地规则结果（本地 fallback）。
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
    # ③ 造句五星：以批改三态为唯一信号，更新「主动输出熟练度」（与 SRS 完全无关）
    # PASS → +1 星；NEEDS_REVIEW / UNCERTAIN → -1 星；星级限制 0~5。
    status = result.get("status") or ("PASS" if result.get("ok") else "NEEDS_REVIEW")
    score = result.get("score", 0)

    # 只对「句子里真的用到」的词更新五星。
    # 组合题的 word 是 2~3 个建议词的名单，原实现对名单里每个词都 ±1，
    # 于是用户只写了其中一个词，另外几个也跟着白涨星 / 无辜扣星 —— 星级失真。
    # 现在先在用户原句里找命中，找不到就不动那个词的星级。
    candidates = [w for w in (word or "").split() if w.strip()]
    used_words = [w for w in candidates if _word_used_in(text, w)]
    head_word = used_words[0].lower() if used_words else (
        candidates[0].lower() if candidates else "")
    for w in used_words:
        # 带上当前课程周：word_output 的 stage/week 是周报「新学词汇」的统计口径
        srs.update_output_star(w, status, score, p["stage"], p["week"])

    result["used_words"] = [w.lower() for w in used_words]
    result["unused_words"] = [w.lower() for w in candidates if w not in used_words]
    # 注意：srs.word_stars 对「还没记录」的词返回 None（设计如此，不编造 0 星）。
    # 用户写的句子若没用上任何建议词，head_word 会回退到 candidates[0]，
    # 而这个词从未 update_output_star 过 —— 直接 ["stars"] 就是
    # TypeError: 'NoneType' object is not subscriptable → 整个批改接口 500。
    # 没记录就如实回 None（前端按「暂无星级」处理），不要崩。
    _star_row = srs.word_stars(head_word) if head_word else None
    result["output_star"] = _star_row["stars"] if _star_row else None

    # ④ 豆包 AI 批改（与 /api/stealth/submit 同一套模式，仅服务于造句批改这一件事）：
    #    AI 的 error_tags 记入 weak_hits（同词同错 ≥3 次 → 前端 ⭐ 薄弱项讲解），
    #    AI 没跑通（没配 Key / 超时 / 限流）就回退本地规则的错误类型，统计不断链。
    ai = None
    ai_err = None
    weak = None
    if body.get("ai"):
        ip = (request.client.host if request.client else "") or ""
        ai, ai_err = _ai.correct(text, client_ip=ip, context=grammar, word=word)
        tags = []
        if ai and not ai_err:
            tags = list(ai.get("error_tags") or [])
        elif not ai:
            tags = [e.get("type") for e in (result.get("errors") or [])
                    if isinstance(e, dict) and e.get("type")]
        if tags:
            _weakness.record(word, tags[:8])
        pend = _weakness.pending(word)
        if pend:
            weak = {"word": word, "tag": pend["tag"], "hits": pend["hits"],
                    "explain": _weakness.cached(word, pend["tag"])}
    result["ai"] = ai
    result["ai_error"] = ai_err
    result["weakness"] = weak
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


@app.post("/api/output/stars")
def output_stars(body: dict):
    """③ 批量读取多个词的造句五星熟练度，返回 {word: stars}（仅含已记录的词）。

    供今日造句计划页给每个词打 ★ 用；未记录的词不返回，不编造 0 星。
    """
    words = body.get("words") or []
    return {"stars": srs.stars_map(words)}


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


@app.get("/api/errors/trend")
def errors_trend(days: int = 90, bucket: str = "week", only_unfixed: int = 0):
    """月报「近四周错误趋势」：按时间桶聚合 errors 表，统计窗口内的错误数量。

    - days  : 向前窗口天数（默认 90）
    - bucket: 'week'（默认，'2026-W35' 形状）或 'day'（'2026-08-30' 形状）
    - only_unfixed: 1 只统计未改正(fixed=0)错误，默认 0 统计全部
    双引擎：SQLite 用 strftime，PostgreSQL 用 to_char/date_trunc，
    通过 _using_pg() 分支，绝不写死某一引擎的 SQL。
    空数据返回空数组（weeks=[] 或 days=[]），不报错。
    """
    conn = get_conn()
    try:
        # 时间桶表达式：两种引擎各一套，输出形状统一为 'YYYY-Www' / 'YYYY-MM-DD'
        if _using_pg():
            if bucket == "day":
                bucket_expr = "to_char(created_at, 'YYYY-MM-DD')"
            else:
                # IYYY/IW 取 ISO 周（周一为一周起点），并用 "W" 字面量保持与 SQLite 同形状
                bucket_expr = "to_char(date_trunc('week', created_at), 'IYYY-\"W\"IW')"
        else:
            if bucket == "day":
                bucket_expr = "strftime('%Y-%m-%d', created_at)"
            else:
                bucket_expr = "strftime('%Y-W%W', created_at)"

        # 窗口下界用 Python 计算后作为参数传入，避开两引擎日期函数差异（TEXT vs timestamp）
        cutoff = (app_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        sql = ("SELECT " + bucket_expr + " AS bucket_label, COUNT(*) AS cnt "
               "FROM errors WHERE created_at >= ?")
        args = [cutoff]
        if only_unfixed:
            sql += " AND fixed=0"
        sql += " GROUP BY bucket_label ORDER BY bucket_label ASC"
        rows = conn.execute(sql, tuple(args)).fetchall()
    finally:
        conn.close()

    if bucket == "day":
        return {"bucket": "day",
                "days": [{"date": r["bucket_label"], "count": r["cnt"]} for r in rows]}
    return {"bucket": "week",
            "weeks": [{"week": r["bucket_label"], "count": r["cnt"]} for r in rows]}


@app.get("/api/errors/{error_type}")
def error_detail(error_type: str):
    return svc.error_detail(error_type)


@app.get("/api/weakness")
def weakness():
    """④ 薄弱项聚合。

    原先只看错误本 + 五星输出，是"半瞎"的：造句得分低不算、复习反复答错不算、
    周测考砸不算、听力听不懂不算、专项训练没达标也不算。

    现在改由 link.build_weakness() 做综合判定，把六个来源都纳入。
    返回结构保持向后兼容（error_types / low_star_words / recommendations），
    另附 sources 分板块明细。
    """
    return _link.build_weakness()


@app.get("/api/word/{word}/profile")
def word_profile(word: str):
    """一个词的全息档案：学习 / 造句 / 错误 / 复习 / 主动输出 / 专项训练的完整轨迹。

    打通各板块的「词」这条主线 —— 此前这些数据分散在七张表里，
    没有任何一个地方能把某个词的全部经历摊开来看。
    """
    r = _link.word_profile(word)
    if r is None:
        return {"ok": False, "word": word, "reason": "no_data"}
    return r


@app.get("/api/training/summary")
def training_summary():
    """专项训练整体成绩汇总（此前这块数据对总结页完全隐形）。"""
    r = _link.training_summary()
    return {"ok": True, "summary": r}


@app.get("/api/error-types")
def error_types():
    return ERROR_TYPES


# ---------- 周测 ----------
@app.get("/api/quiz/{stage}/{week}")
def quiz_get(stage: int, week: int):
    return svc.build_weekly_quiz(stage, week)


@app.post("/api/quiz/grade")
def quiz_grade(body: dict):
    stage = int(body.get("stage", 0))
    week = int(body.get("week", 1))
    r = svc.grade_quiz(stage, week, body.get("answers", {}))
    # 周测答错的题同步进错误本：detail_json 里本来就有题干和知识点标签，
    # 但此前除了算总分之外从没被用过 —— 考砸了没人知道。
    # 这一步失败也不影响周测成绩本身，所以异常被吞掉。
    try:
        r["synced_errors"] = _link.sync_quiz_errors(stage, week, r.get("detail") or [])
    except Exception as e:
        print("[api.quiz_grade] 错题同步失败（不影响成绩）:", e)
        r["synced_errors"] = 0
    return r


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


@app.get("/api/vocab/all")
def vocab_all(q: str = "", limit: int = 500):
    """⑤ 全部词汇浏览：列出用户真正学过的词（学习项 + SRS 卡 + 造句词，去重）。

    只读，不改变 SRS / 五星 / 不产生学习完成记录。支持按英文前缀/子串搜索。
    不扫描 ECDICT 全表（76 万行），只取用户实际接触过的词。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT ref_key AS w FROM day_items WHERE kind='vocab' AND ref_key<>'' "
        "UNION SELECT DISTINCT ref_key FROM reviews WHERE kind='vocab' AND ref_key<>'' "
        "UNION SELECT DISTINCT word FROM sentences WHERE word<>''"
    ).fetchall()
    words = [r["w"] for r in rows if r["w"]]
    if q and q.strip():
        ql = q.strip().lower()
        words = [w for w in words if ql in w.lower()]
    words.sort()
    out = []
    for w in words[:limit]:
        d = conn.execute(
            "SELECT phonetic, pos, meaning FROM dictionary WHERE word=?",
            (w.lower(),)).fetchone()
        out.append({
            "word": w,
            "phonetic": (d["phonetic"] if d else "") or "",
            "pos": (d["pos"] if d else "") or "",
            "meaning": (d["meaning"] if d else "") or "",
        })
    conn.close()
    return {"words": out, "total": len(words)}


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
    # 兜底：导入链路里任何未捕获异常都转成 JSON 错误返回。
    # 否则 FastAPI 直接抛 500，前端 api() 里的 r.json() 会抛
    # "Unexpected token"，用户只看到一句没头没尾的「导入请求失败」，
    # 真实原因（比如 OOM、字段约束冲突）全被吞掉。
    try:
        return fn(
            text,
            forced_stage=int(forced_stage) if forced_stage is not None else None,
            forced_week=int(forced_week) if forced_week is not None else None,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": f"导入失败：{type(e).__name__}: {e}"}


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


# ---------- 活动聚合：最近活动 + 近四周趋势 ----------
# 全部基于真实表（quizzes / listening_progress / sentences / history），
# 不生成任何假数据；算不出的指标返回 null，由前端显示「—」。
def _act_date(s):
    return str(s)[:10] if s else ""


def _iso_week(s):
    """返回 (year, week) 用于按自然周分组；无日期返回 None。"""
    d = _act_date(s)
    try:
        y, m, day = map(int, d.split("-"))
        return datetime(y, m, day).isocalendar()[:2]
    except Exception:
        return None


# 活动聚合单次最多扫多少行历史。
#
# 与导入 OOM 同源的隐患：这些聚合查询原本是「全表读进内存再截断」——
# /api/activity 明明只要最近 4 周，却把 quizzes / listening_progress /
# sentences / history 整张表都拉出来。表现在数据量小没事，但 sentences
# 是高频写入（每次造句一行），攒上几万行后 psycopg2 会一次性把结果集拉到
# 客户端内存，在 Render 512MB 限制下是实打实的定时炸弹。
#
# psycopg2 默认客户端游标，无论 fetchall 还是逐行迭代都会全量拉回，
# 所以「改成流式」没用，只能靠 LIMIT 限制扫描行数。
#
# 取值理由：接口默认只要 4 周，20000 行覆盖 4 周绰绰有余
# （相当于每天 700 条记录）。超出部分是最老的历史，截断不影响近期统计。
ACT_SCAN_LIMIT = 20000

# /api/activity 的「最近动态」每张表最多取多少条。
# 接口最终只返回 50 条，各表取 200 条归并后取前 50 与全表排序取前 50 等价
# （若某条能进全局前 50，它在本表里必然也排在前 50 之内），
# 但把扫描量从 20000 行/表降到 200 行/表。
ACT_RECENT_LIMIT = 200


@app.get("/api/activity")
def activity(weeks: int = 4):
    conn = get_conn()
    try:
        # ① 最近动态：每张表只取最新若干条再归并。
        #    原来每张表都拉 ACT_SCAN_LIMIT 行到 Python 里排序，取前 50 —— 拉了
        #    400 倍无用数据。全局前 50 一定落在「各表前 N 条」里（否则那 N 条
        #    会更早占据前 50），所以按表各取 ACT_RECENT_LIMIT 条就足够，
        #    再按日期归并排序，结果与原来一致。
        acts = []  # (date, type, title, detail, score, ok)
        # 测评
        for r in conn.execute(
                "SELECT kind, stage, week, score, created_at FROM quizzes "
                "WHERE created_at IS NOT NULL "
                f"ORDER BY created_at DESC LIMIT {ACT_RECENT_LIMIT}").fetchall():
            acts.append((_act_date(r["created_at"]), "测评", "测评",
                         f"{r['kind']} · S{r['stage']}W{r['week']}", r["score"], None))
        # 听力
        for r in conn.execute(
                "SELECT stage, week, day, listening_done, listening_total, created_at "
                "FROM listening_progress WHERE created_at IS NOT NULL "
                f"ORDER BY created_at DESC LIMIT {ACT_RECENT_LIMIT}"
            ).fetchall():
            acc = None
            tot = r["listening_total"] or 0
            done = r["listening_done"] or 0
            if tot:
                acc = round(done * 100 / tot)
            acts.append((_act_date(r["created_at"]), "听力", "听力练习",
                         f"S{r['stage']}W{r['week']}D{r['day']}", acc, None))
        # 造句
        for r in conn.execute(
                "SELECT word, score, good, created_at FROM sentences "
                "WHERE created_at IS NOT NULL "
                f"ORDER BY created_at DESC LIMIT {ACT_RECENT_LIMIT}").fetchall():
            acts.append((_act_date(r["created_at"]), "造句", "造句",
                         f"目标词 {r['word']}", r["score"], 1 if r["good"] else 0))
        # 背词
        for r in conn.execute(
                "SELECT detail, created_at FROM history "
                "WHERE action='learn_vocab' AND created_at IS NOT NULL "
                f"ORDER BY created_at DESC LIMIT {ACT_RECENT_LIMIT}").fetchall():
            acts.append((_act_date(r["created_at"]), "背词", "学习单词",
                         r["detail"], None, None))
        acts.sort(key=lambda x: x[0], reverse=True)
        recent = [{"type": t, "title": ti, "detail": d, "date": dt,
                   "score": sc, "ok": ok}
                  for (dt, t, ti, d, sc, ok) in acts[:50]]

        # ② 近 N 周聚合（学习天数 / 听力正确率 / 测评平均分）
        #    聚合全部下推到 SQL：按「天」GROUP BY 后只回传几百行，
        #    不再把整张表拉进 Python。返回结构与原来完全一致。
        week_map = {}

        def _touch(dt):
            iw = _iso_week(dt)
            if iw:
                week_map.setdefault(iw, {"days": set(), "lt": 0, "ld": 0,
                                         "qs": 0, "qn": 0})
                week_map[iw]["days"].add(dt)
            return iw

        def _feed(rows):
            for r in rows:
                dt = _act_date(r["d"])
                iw = _touch(dt)
                if iw is None:
                    continue
                wk = week_map[iw]
                if r["qn"]:
                    wk["qs"] += r["qs"] or 0
                    wk["qn"] += r["qn"] or 0
                wk["ld"] += r["ld"] or 0
                wk["lt"] += r["lt"] or 0

        # 测评：按天汇总总分与非空条数（AVG 按天再平均会失真，所以存 sum/count）
        _feed(conn.execute(
            "SELECT substr(created_at,1,10) AS d, SUM(score) AS qs,"
            " COUNT(score) AS qn, 0 AS ld, 0 AS lt FROM quizzes"
            " WHERE created_at IS NOT NULL AND created_at <> ''"
            " GROUP BY d").fetchall())
        # 听力：按天汇总 done / total
        _feed(conn.execute(
            "SELECT substr(created_at,1,10) AS d, 0 AS qs, 0 AS qn,"
            " SUM(listening_done) AS ld, SUM(listening_total) AS lt"
            " FROM listening_progress"
            " WHERE created_at IS NOT NULL AND created_at <> ''"
            " GROUP BY d").fetchall())
        # 造句 / 背词：只要活跃天
        _feed(conn.execute(
            "SELECT substr(created_at,1,10) AS d, 0 AS qs, 0 AS qn,"
            " 0 AS ld, 0 AS lt FROM sentences"
            " WHERE created_at IS NOT NULL AND created_at <> ''"
            " GROUP BY d").fetchall())
        _feed(conn.execute(
            "SELECT substr(created_at,1,10) AS d, 0 AS qs, 0 AS qn,"
            " 0 AS ld, 0 AS lt FROM history WHERE action='learn_vocab'"
            " AND created_at IS NOT NULL AND created_at <> ''"
            " GROUP BY d").fetchall())

        all_weeks = sorted(week_map.keys())
        recent_weeks = all_weeks[-weeks:]
        out_weeks = []
        for i, iw in enumerate(recent_weeks):
            wk = week_map[iw]
            lt = wk["lt"]
            ld = wk["ld"]
            listen_acc = round(ld * 100 / lt) if lt else None
            quiz_avg = round(wk["qs"] / wk["qn"]) if wk["qn"] else None
            out_weeks.append({
                "label": f"W{i + 1}",
                "learning_days": len(wk["days"]),
                "listening_acc": listen_acc,
                "quiz_avg": quiz_avg,
                "vocab_rate": None,  # 后端无「计划总数」，完成率无法真实计算，返回 null
            })
        return {"recent": recent, "weeks": out_weeks}
    finally:
        conn.close()


@app.get("/api/report")
def api_report():
    """总结页周报/月报统计。

    数据全部来自现有表（sentences / quizzes / listening_progress /
    errors / reviews / weeks / word_output），不编造任何数字；
    算不出的指标返回 None，前端显示「—」。字段口径见 report.py 文件头。

    注：前端 pages.sum 一直请求本接口，此前后端未实现（404），
    导致周报/月报的核心指标全部为空。
    """
    return _report.build_report()


# ---------- 专项训练（补习）----------
# 完整流程：
#   用户把 prompt_md 发给外部 AI → AI 返回含 <<<TEST>>> ... <<<END>>> 标记的一张卷
#   → 用户粘贴回系统(/api/test/parse 解析) → 回合制训练(/api/test/grade 判分) → 记录历史
# AI 调用完全由用户完成，后端只做「解析 / 判分 / 存储」。
def parse_test_text(text):
    """解析含 <<<TEST>>> ... <<<END>>> 标记的训练卷文本，返回结构化 JSON。

    返回：{"sections":[{"section": str, "questions":[
        {"qid","type","prompt","answer"?,"options"?}]}]}
    解析不到（无 TEST 标记 / 格式不符）时返回 {"sections":[]} 而不是崩溃。
    """
    if not text or not isinstance(text, str):
        return {"sections": []}

    # 1) 定位 TEST 块：<<<TEST>>> 起、<<<END>>> 止（END 缺失则取到文末）
    m = re.search(r"<<<\s*TEST\s*>>>(.*?)(?:<<<\s*END\s*>>>|$)",
                  text, re.DOTALL | re.IGNORECASE)
    if not m:
        return {"sections": []}
    body = m.group(1)

    # 2) 按 SECTION 切分（无 SECTION 标记则整体归到一个空名 section）
    sec_re = re.compile(r"<<<\s*SECTION\s+(.*?)\s*>>>", re.DOTALL | re.IGNORECASE)
    sec_splits = list(sec_re.finditer(body))
    if not sec_splits:
        blocks = [(None, body)]
    else:
        blocks = []
        for i, sm in enumerate(sec_splits):
            name = sm.group(1).strip()
            start = sm.end()
            end = sec_splits[i + 1].start() if i + 1 < len(sec_splits) else len(body)
            blocks.append((name, body[start:end]))

    # 3) 逐 section 解析题目
    sections = []
    for name, content in blocks:
        questions = _parse_questions_in_section(content)
        if questions:
            sections.append({"section": name or "", "questions": questions})
    return {"sections": sections}


# 单题标记：Q1. (choice)/(fill)/(judge)/(subjective)
_Q_RE = re.compile(r"Q\s*(\d+)\s*\.\s*\((choice|fill|judge|subjective)\)", re.IGNORECASE)
_ANSWER_RE = re.compile(
    r"ANSWER\s*:\s*(.*?)(?:\n\s*(?:Q\d|<<<)|\Z)", re.DOTALL | re.IGNORECASE)
_OPT_LETTER_RE = re.compile(r"\b([A-Z])\.\s*")


def _parse_questions_in_section(content):
    parts = _Q_RE.split(content)
    questions = []
    # parts: [前置文本, qid, type, 题体, qid, type, 题体, ...]
    for i in range(1, len(parts), 3):
        qid = parts[i].strip()
        qtype = parts[i + 1].strip().lower()
        qbody = parts[i + 2]
        prompt, answer, options = _parse_one_question(qtype, qbody)
        q = {"qid": qid, "type": qtype, "prompt": prompt}
        if answer is not None:
            q["answer"] = answer
        if options is not None:
            q["options"] = options
        questions.append(q)
    return questions


def _parse_one_question(qtype, body):
    """从单题题体里抽答案、题干与选项。返回 (prompt, answer, options)。"""
    am = _ANSWER_RE.search(body)
    answer = am.group(1).strip() if am else None
    head = body[: am.start()].strip() if am else body.strip()

    prompt = head
    options = None
    if qtype == "choice":
        letters = list(_OPT_LETTER_RE.finditer(head))
        if letters:
            prompt = head[: letters[0].start()].strip()
            options = []
            for i, mm in enumerate(letters):
                s = mm.end()
                e = letters[i + 1].start() if i + 1 < len(letters) else len(head)
                options.append(head[s:e].strip())
    return prompt, answer, options


@app.post("/api/test/parse")
def test_parse(body: dict):
    """解析用户粘贴回的外部 AI 训练/测评卷（<<<TEST>>>…<<<END>>>），
    返回前端 renderTestPaper 需要的 {ok, sections, questions, question_count, ...} 形状。"""
    return parse_test_paper(body.get("text") or "")


@app.post("/api/test/projects")
def test_create_project(body: dict):
    """新建一个专项训练项目（补习）。"""
    ability = (body.get("ability") or "").strip()
    problem = (body.get("problem") or "").strip()
    prompt_md = body.get("prompt_md") or ""
    conn = get_conn()
    pid = insert_get_id(
        conn,
        "INSERT INTO training_projects (ability, problem, prompt_md, created_at) VALUES (?,?,?,?)",
        (ability, problem, prompt_md, ts()))
    conn.commit()
    conn.close()
    return {"ok": True, "id": pid}


@app.get("/api/test/projects")
def test_list_projects():
    """列出全部专项训练项目。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, ability, problem, prompt_md, created_at FROM training_projects "
        f"ORDER BY id DESC LIMIT {ACT_SCAN_LIMIT}").fetchall()
    conn.close()
    return {"items": [dict(r) for r in rows]}


# ---------- 专项训练四层落库 ----------
@app.get("/api/training/state")
def training_state_get():
    """读取专项训练四层全量状态（projects / sessions / rounds / attempts）。

    此前这些数据只存在浏览器 localStorage，换设备即丢失，且对错误本、
    薄弱项、总结页完全不可见。现在统一由服务端存储。
    """
    return _training.load_state()


@app.post("/api/training/state")
def training_state_post(body: dict):
    """全量保存专项训练四层状态（按业务 ID 做 upsert）。

    前端的读写模式是「读整个 → 改 → 存整个」，所以这里按全量同步实现：
    列表里有的就 upsert，列表里没有的（且带业务 ID）就删除。
    老数据（project_key 为 NULL 的历史行）一律保留不动。
    """
    return _training.save_state(body)


@app.get("/api/weak/snapshots")
def weak_snapshots_get():
    """读取薄弱项每日快照（供趋势图 / 本周 vs 上周 / 连续周使用）。

    返回 [{'d':'2026-09-05','map':{'@grammar':3, ...}}, ...]，按日期升序。
    后端 `/api/weakness` 只给「近 30 天累计次数」这个会滚动的当前值，
    历史某天的数值今天重算不出来，所以趋势只能靠这份每日快照。
    """
    return {"ok": True, "snapshots": _link.load_snapshots()}


@app.post("/api/weak/snapshots")
def weak_snapshots_post(body: dict):
    """保存某一天的薄弱项快照。

    body: {'d': 'YYYY-MM-DD', 'map': {key: number}}
    同一天重复提交覆盖为最新值；最多保留 60 天。
    """
    n = _link.save_snapshots((body or {}).get("d"), (body or {}).get("map") or {})
    return {"ok": True, "saved": n}


def _grade_training(ability, user_sentence, answer):
    """纯本地规则判分：返回 (score 0-100, ok bool, feedback str)。

    以项目 ability 关键词做启发式：能拿到参考答案就直接比对，
    否则按能力维度 + 句子基本质量给一个过程分。
    """
    u = (user_sentence or "").strip()
    if not u:
        return 0, False, "未作答，请先输入你的回答。"

    a = (answer or "").strip()
    if a:
        def _norm(s):
            return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
        if _norm(u) == _norm(a):
            return 100, True, "回答正确。"
        if _norm(a) and _norm(a) in _norm(u):
            return 80, True, "要点正确，表达略有出入。"
        return 60, False, "与参考答案不一致，请检查。"

    # 无参考答案：按 ability 关键词 + 长度给一个过程分
    ab = (ability or "").lower()
    if "听" in ab or "listening" in ab:
        score, fb = 75, "听力类作答已记录，建议回听原文自行核对。"
    elif "语法" in ab or "grammar" in ab:
        score = 85 if (u[:1].isupper() and re.search(r"[.!?]$", u)) else 65
        fb = "语法类作答已记录，注意首字母大写与句末标点。"
    elif "词汇" in ab or "vocab" in ab:
        score, fb = 75, "词汇类作答已记录。"
    else:
        score, fb = 70, "作答已记录，请结合参考答案自行核对。"
    if len(u.split()) >= 3:
        score = min(95, score + 10)
    return score, score >= 60, fb


@app.post("/api/test/grade")
def test_grade(body: dict):
    """整卷判分（测评 / 补习试卷共用）：比对用户作答与标准答案，写历史，返回成绩。"""
    return grade_test_paper(
        body.get("text") or "",
        body.get("answers") or {},
        body.get("kind") or "week")


# ---------- 听力骨架 ----------
@app.get("/api/listening/next")
def listening_next(n: int = 5):
    """返回下一批听力素材。

    素材来源：`example_sentences`（单词例句 + 中文翻译），属于系统内已有的真实数据，
    不硬编码任何假素材。练习形式：听英文句子（TTS 朗读）→ 看中文翻译 → 自评是否听懂。

    参数：
      n = 返回条数（默认 5，上限 20）

    返回：{"ok":true,"items":[{"id","word","sentence","translation"}...]}
    表为空或不存在时返回空数组，不报错。
    """
    n = max(1, min(int(n or 5), 20))
    conn = get_conn()
    try:
        # 确认素材表存在（老库可能还没建）
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='example_sentences'"
        ).fetchone()
        if not exists:
            return {"ok": True, "items": []}
        rows = conn.execute(
            "SELECT id, word, sentence, translation FROM example_sentences "
            "WHERE sentence IS NOT NULL AND TRIM(sentence)<>'' "
            "ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    items = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        items.append({
            "id": d.get("id"),
            "word": d.get("word") or "",
            "sentence": d.get("sentence") or "",
            "translation": d.get("translation") or "",
        })
    return {"ok": True, "items": items}


@app.post("/api/listening/submit")
def listening_submit(body: dict):
    """提交一次听力作答：仅记录到 reviews 表（kind='listening'）。

    用 INSERT OR IGNORE 避免重复提交触发 UNIQUE 约束报错。
    没有素材数据时本接口也不会报错，只记录一条听力的对错统计。
    """
    item_id = body.get("item_id")
    correct = bool(body.get("correct"))
    conn = get_conn()
    try:
        p = svc.get_progress()
        stage = p.get("stage", 0)
        week = p.get("week", 1)
        day = p.get("day", 1)
    except Exception:
        stage = week = day = 0
    conn.execute(
        "INSERT OR IGNORE INTO reviews "
        "(kind, ref_key, prompt, answer, stage, week, day, last_score, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("listening", str(item_id), "", "1" if correct else "0",
         stage, week, day, 1 if correct else 0, ts()))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- 课程地图（学习页主题/语法来源）----------
@app.get("/api/curriculum")
def curriculum():
    """返回阶段×周的课程地图，供前端学习页显示主题与语法重点。
    权威源是 weeks 表（init_db 已用 SEED 预填），前端不复制数据。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT stage, week_no, title, grammar FROM weeks "
            "ORDER BY stage, week_no").fetchall()
    finally:
        conn.close()
    return [{"stage": r["stage"], "week": r["week_no"],
             "theme": r["title"] or "", "grammar": r["grammar"] or ""}
            for r in rows]


# ---------- 测评 / 补习：试卷解析与判分（前端共用）----------
_Q_BLOCK_RE = re.compile(
    r"Q\s*(\d+)\s*\.\s*\((choice|fill|judge|subjective)\)(.*?)"
    r"(?=Q\s*\d+\s*\.\s*\(|<<<\s*END\s*>>>|\Z)",
    re.DOTALL | re.IGNORECASE)
_SECTION_RE = re.compile(r"<<<\s*SECTION\s+(.*?)\s*>>>", re.DOTALL | re.IGNORECASE)
_TEST_RE = re.compile(r"<<<\s*TEST\s*>>>(.*)<<<\s*END\s*>>>", re.DOTALL | re.IGNORECASE)
_PASSAGE_RE = re.compile(r"<<<\s*PASSAGE\s*>>>(.*?)<<<\s*END\s*>>>", re.DOTALL | re.IGNORECASE)
_ANSWER_RE = re.compile(r"ANSWER:\s*(.+?)(?:\n|$)", re.IGNORECASE)


def _parse_test_questions(content):
    """从单个 SECTION 的题块里抽出题目列表（前端 renderTestPaper 所需形状）。"""
    questions = []
    for m in _Q_BLOCK_RE.finditer(content):
        no = int(m.group(1))
        kind = m.group(2).lower()
        body = m.group(3).strip()
        am = _ANSWER_RE.search(body)
        answer = am.group(1).strip() if am else ""
        pre = body[:am.start()].strip() if am else body
        options = []
        stem = pre
        if kind == "choice":
            for om in re.finditer(r"([A-D])\.\s*(.*?)(?=\s+[A-D]\.\s|\Z)", pre, re.DOTALL):
                options.append(om.group(2).strip())
            first = re.search(r"[A-D]\.\s", pre)
            if first:
                stem = pre[:first.start()].strip()
        questions.append({
            "no": no, "kind": kind, "stem": stem,
            "options": options, "answer": answer, "raw": body,
        })
    return questions


def parse_test_paper(text):
    """解析 <<<TEST>>>…<<<END>>> 试卷，返回前端 renderTestPaper 需要的形状。"""
    if not text or not isinstance(text, str):
        return {"ok": False, "error": "试卷内容为空"}
    m = _TEST_RE.search(text)
    if not m:
        # 兜底：有些试卷省略结尾 <<<END>>>，直接取到文末
        m = re.search(r"<<<\s*TEST\s*>>>(.*)$", text, re.DOTALL | re.IGNORECASE)
    if not m:
        return {"ok": False, "error": "未找到 <<<TEST>>>…<<<END>>> 标记"}
    body = m.group(1)
    sec_splits = list(_SECTION_RE.finditer(body))
    if not sec_splits:
        blocks = [(None, body)]
    else:
        blocks = []
        for i, sm in enumerate(sec_splits):
            name = sm.group(1).strip()
            start = sm.end()
            end = sec_splits[i + 1].start() if i + 1 < len(sec_splits) else len(body)
            blocks.append((name, body[start:end]))
    sections = []
    all_q = []
    for name, content in blocks:
        passage = ""
        pm = _PASSAGE_RE.search(content)
        if pm:
            passage = pm.group(1).strip()
        qcontent = content
        if pm:
            qcontent = content[:pm.start()] + content[pm.end():]
        qs = _parse_test_questions(qcontent)
        if qs:
            sections.append({"name": name or "未命名模块",
                             "passage": passage, "questions": qs})
            all_q.extend(qs)
    if not all_q:
        return {"ok": False, "error": "未解析到任何题目（请确认含 Q1. (choice) … 格式）"}
    return {
        "ok": True, "title": "能力测评", "question_count": len(all_q),
        "questions": all_q, "sections": sections, "warnings": [],
    }


def _norm_ans(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower()).strip()


def grade_test_paper(text, answers, kind):
    """整卷判分：比对用户作答与标准答案，返回正确/总分/模块分布/错题库。
    写入 quizzes 表作为历史（/api/test/history 读取）。"""
    parsed = parse_test_paper(text)
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error", "解析失败")}
    questions = parsed["questions"]
    sec_of = {}
    for sec in parsed["sections"]:
        for q in sec["questions"]:
            sec_of[q["no"]] = sec["name"]
    correct = total = wrong = 0
    by_section = {}
    detail = []
    for q in questions:
        if q["kind"] == "subjective":
            continue  # 自评题不计分
        total += 1
        no = q["no"]
        expected = (q.get("answer") or "").strip()
        got = str(answers.get(str(no), answers.get(no, "")) or "").strip()
        if q["kind"] == "fill":
            exps = [e.strip() for e in expected.split("/") if e.strip()]
            ok = any(_norm_ans(got) == _norm_ans(e) for e in exps) if exps else False
        else:  # choice / judge
            ok = _norm_ans(got) == _norm_ans(expected)
        if ok:
            correct += 1
        else:
            wrong += 1
        sec = sec_of.get(no, "")
        bs = by_section.setdefault(sec, {"correct": 0, "total": 0})
        bs["total"] += 1
        if ok:
            bs["correct"] += 1
        detail.append({"no": no, "ok": ok, "section": sec,
                       "got": got, "expected": expected})
    score = round(correct / total * 100) if total else 0
    passed = score >= 60
    try:
        p = svc.get_progress()
        conn = get_conn()
        # 留存原题 + 作答：历史成绩里能点开回看「当时考了什么、我答了什么」。
        # 老库若还没补上这两列（init_db 迁移未跑），自动退回原来的简写入，不影响判分。
        try:
            conn.execute(
                "INSERT INTO quizzes (kind, stage, week, score, passed, detail_json,"
                " paper_json, answers_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (kind or "week", p.get("stage", 0), p.get("week", 1), score,
                 1 if passed else 0,
                 json.dumps({"correct": correct, "total": total, "questions": detail, "by_section": by_section}, ensure_ascii=False),
                 text or "", json.dumps(answers or {}, ensure_ascii=False), ts()))
        except Exception:
            conn.execute(
                "INSERT INTO quizzes (kind, stage, week, score, passed, detail_json, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (kind or "week", p.get("stage", 0), p.get("week", 1), score,
                 1 if passed else 0,
                 json.dumps({"correct": correct, "total": total, "questions": detail, "by_section": by_section}, ensure_ascii=False), ts()))
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"ok": True, "correct": correct, "total": total, "score": score,
            "passed": passed, "by_section": by_section, "detail": detail, "wrong": wrong}


@app.get("/api/test/history")
def test_history(limit: int = 50):
    """历史测评成绩（测评页「📈 历史成绩」与补习共用）。

    除分数外回传 paper_json（原题）/ answers_json（作答）/ detail_json（每题对错与归因），
    前端点开某次成绩即可回看当次卷面，不需要另开页面。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, kind, stage, week, score, passed, created_at, detail_json,"
            " paper_json, answers_json FROM quizzes "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get("/api/test/prompt")
def test_prompt(kind: str = "week"):
    """返回生成测评卷的提示词模板（前端「复制提示词」用）。
    模板内含本机薄弱项上下文 + <<<TEST>>> 输出格式。"""
    return {"prompt": build_test_prompt(kind or "week")}


# 周测 / 月测 / 阶段测 共用一套《通用英语测评试卷生成模板》，
# 三者的区别只有「范围 / 难度 / 题量」三个参数，结构完全一致。
TEST_KIND_CFG = {
    "week":  ("周测",   "本周新学词汇 + 本周薄弱项",     "当前水平",     "12–16"),
    "month": ("月测",   "本月累计薄弱项 + 当月新学",     "当前水平略升", "18–24"),
    "stage": ("阶段测", "本阶段全部目标 + 累计薄弱项",   "阶段目标",     "24–30"),
}

TEST_PROMPT_TPL = """你是一名严格、克制、以测评效果为导向的英语试卷生成器。

【角色】
你只负责按下方确认的测评项目生成试卷，不重新诊断、不新增训练范围。

【本次测评类型】
{kind}  （取值：周测 / 月测 / 阶段测）

【范围与难度】（按类型调节，不要超范围）
- 周测：覆盖本周新学词汇与本周薄弱项，难度 = 当前水平，题量 12–16。
- 月测：覆盖本月累计薄弱项 + 当月新学，难度 = 当前水平略升，题量 18–24。
- 阶段测：覆盖本阶段全部目标与累计薄弱项，难度 = 阶段目标，题量 24–30。
范围：{scope}
难度：{difficulty}
题量：{count}

【输入：学习者当前数据】
水平：{level}
阶段：{stage}
已学：{learned}
薄弱项（来自错误本，仅作出题参考）：
{weakness}

【核心原则】
1. 单错 ≠ 薄弱项：偶发一次的错误（🔵记忆项）不得直接当成必练项，
   只在「近30天≥2次（🟡薄弱项）」或诊断已认定为「🔴阻塞项」时才围绕它出题。
2. 围绕已确认薄弱项出题，但不得因此无限扩张到其他未授权能力。
3. 题型服务于目标：听力真考听觉、阅读真考理解，不把阅读伪装成听力。
4. 测评结果回流：试卷作答后由系统记录，进入下一轮诊断。

【输出格式（必须输出系统可解析的试卷文本）】
在回答末尾输出以下内容（<<<TEST>>> 与 <<<END>>> 之间只放题目，不写解释）：

<<<TEST>>>
<<<SECTION 听力>>>
<<<PASSAGE>>>
（英文听力原文；系统提供🔊朗读，无需音频文件）
<<<END>>>
Q1. (choice) [听力] 题干
A. ... B. ... C. ... D. ...
ANSWER: A
Q2. (fill) [听力] 听后填空：I arrived ______ the station.
ANSWER: at

<<<SECTION 词汇>>>
Q3. (choice) 单词 "apple" 的意思是？
A. 苹果 B. 香蕉 C. 橙子 D. 葡萄
ANSWER: A
Q4. (fill) 填空：He ______ to work by bus.
ANSWER: goes

<<<SECTION 语法>>>
Q5. (judge) 判断正误：She go to school every day.
ANSWER: FALSE
Q6. (fill) 用所给词填空：______ (be) there any water?
ANSWER: Is

<<<SECTION 阅读>>>
<<<PASSAGE>>>
（短文正文，可多行）
<<<END>>>
Q7. (choice) 根据短文，Tom 几点吃早餐？
A. 6:30 B. 7:00 C. 7:30 D. 8:20
ANSWER: C

<<<SECTION 主动输出>>>
Q8. (subjective) 用 because 说一句你今天没出门的原因。（学习者自评，系统不自动判分）
<<<END>>>

硬性规则：
1. 题号 Q1… 从 1 连续编号，不跳号。
2. (choice) 四选一，A/B/C/D 每行一个，ANSWER 单字母；(fill) 用 6 个下划线表示空位；(judge) ANSWER 仅 TRUE/FALSE；(subjective) 学习者自评。
3. SECTION 名称用中文模块名：听力 / 词汇 / 语法 / 阅读 / 主动输出。
4. 听力先 <<<PASSAGE>>> 给原文再出题；阅读先 <<<PASSAGE>>> 给短文再出题。
5. ANSWER 行是大题最后一行。
6. 不使用 Markdown 语法、不加代码块围栏。
7. <<<TEST>>> 与 <<<END>>> 之间不写任何解释。
"""


def _error_levels():
    """每个错误类型当前的最高等级：🔴阻塞(3) > 🟡薄弱(2) > 🔵记忆(1)。

    等级由 ai_service.correct_sentence 写入 errors.level：
    近30天同错≥2次 → 🟡，单次 → 🔵，诊断认定 → 🔴。
    """
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT error_type AS t, MAX(CASE level WHEN '🔴' THEN 3 "
                "WHEN '🟡' THEN 2 WHEN '🔵' THEN 1 ELSE 0 END) AS lv "
                "FROM errors GROUP BY error_type").fetchall()
        finally:
            conn.close()
        return {r["t"]: int(r["lv"] or 0) for r in rows}
    except Exception:
        return {}


def build_test_prompt(kind):
    """生成「周测 / 月测 / 阶段测」提示词：同一套框架，只按类型调范围/难度/题量。"""
    lvmap = _error_levels()
    ctx = ""
    try:
        errs = svc.error_breakdown()[:8]
        if errs:
            lines = []
            for e in errs:
                lv = lvmap.get(e["type"], 0)
                icon = "🔴" if lv >= 3 else ("🟡" if lv == 2 else "🔵")
                lines.append(
                    f"- {e['type']} {icon}  近30天 {e['count_30d']} · 累计 {e['total']}"
                    + ("（记忆项：偶发，不作为必练项）" if icon == "🔵" else ""))
            ctx += "【薄弱项（近30天次数 · 累计次数 · 等级）】\n" + "\n".join(lines) + "\n"
    except Exception:
        pass
    try:
        low = srs.weak_output_words(threshold=3)[:5]
        if low:
            ctx += "【主动输出偏弱词】" + "、".join(w["word"] for w in low) + "\n"
    except Exception:
        pass

    label, scope, difficulty, count = TEST_KIND_CFG.get(kind, TEST_KIND_CFG["week"])
    try:
        prog = svc.get_progress() or {}
    except Exception:
        prog = {}
    learned = ""
    try:
        learned = ", ".join(w["word"] for w in srs.weak_output_words(threshold=6)[:10])
    except Exception:
        learned = ""

    return TEST_PROMPT_TPL.format(
        kind=label, scope=scope, difficulty=difficulty, count=count,
        level=prog.get("level") or "（未测定）",
        stage=f"第 {prog.get('stage', 0)} 阶段 · 第 {prog.get('week', 1)} 周",
        learned=learned or "（暂无记录）",
        weakness=(ctx or "（暂无已确认薄弱项，请按常规 A2 难度出题）"),
    )




# ---------- 听力模块（前端：粘贴 AI 材料 → 解析入库 → 练习 → 提交）----------
_LISTEN_RE = re.compile(r"<<<\s*LISTENING[^>]*>>>(.*?)(?:<<<\s*END\s*>>>|$)", re.DOTALL | re.IGNORECASE)


def parse_listening_text(text):
    """解析 <<<LISTENING v1>>>…<<<END>>> 听力材料，返回结构化 dict。"""
    if not text:
        return None
    m = _LISTEN_RE.search(text)
    body = m.group(1) if m else text
    title = ""
    tm = re.search(r"^\s*TITLE:\s*(.+)$", body, re.MULTILINE)
    if tm:
        title = tm.group(1).strip()
    dialogue = []
    dm = re.search(r"<<<\s*DIALOGUE\s*>>>(.*?)(?:<<<)", body, re.DOTALL | re.IGNORECASE)
    if dm:
        dialogue = [l.strip() for l in dm.group(1).splitlines() if l.strip()]
    blanks = []
    bm = re.search(r"<<<\s*BLANKS\s*>>>(.*?)(?:<<<)", body, re.DOTALL | re.IGNORECASE)
    if bm:
        for l in bm.group(1).splitlines():
            l = l.strip()
            if not l:
                continue
            sep = "—" if "—" in l else (" - " if " - " in l else None)
            if sep:
                w, s = l.split(sep, 1)
                blanks.append({"word": w.strip(), "sentence": s.strip()})
    passage = ""
    pm = re.search(r"<<<\s*PASSAGE\s*>>>(.*?)(?:<<<Q1|<<|$)", body, re.DOTALL | re.IGNORECASE)
    if pm:
        passage = pm.group(1).strip()
    questions = []
    qm = re.search(r"<<<\s*Q1\s*>>>(.*)$", body, re.DOTALL | re.IGNORECASE)
    if qm:
        qtext = qm.group(1)
        segs = re.split(r"<<<\s*Q\d+\s*>>>", qtext)
        for seg in segs:
            seg = seg.strip()
            if not seg:
                continue
            qm2 = re.search(r"Question:\s*(.+?)(?:\n|$)", seg, re.IGNORECASE)
            question = qm2.group(1).strip() if qm2 else seg.split("\n")[0].strip()
            opts = re.findall(r"^([A-D])\.\s*(.+)$", seg, re.MULTILINE)
            options = [o[1].strip() for o in opts]
            am = re.search(r"ANSWER:\s*(.+?)(?:\n|$)", seg, re.IGNORECASE)
            answer = am.group(1).strip() if am else ""
            questions.append({"question": question, "options": options, "answer": answer})
    return {"title": title, "dialogue": dialogue, "blanks": blanks,
            "passage": passage, "questions": questions}


@app.post("/api/listening/import")
def listening_import(body: dict):
    stage = int(body.get("stage", 0) or 0)
    week = int(body.get("week", 1) or 1)
    day = int(body.get("day", 1) or 1)
    text = body.get("text") or ""
    parsed = parse_listening_text(text)
    if not parsed:
        return {"ok": False, "error": "无法解析：未找到 <<<LISTENING v1>>> 标记"}
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM listening_materials WHERE stage=? AND week=? AND day=?",
            (stage, week, day)).fetchone()
        replaced = bool(exists)
        conn.execute(
            "INSERT OR REPLACE INTO listening_materials "
            "(stage, week, day, title, dialogue_json, passage, questions_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (stage, week, day, parsed["title"],
             json.dumps(parsed["dialogue"], ensure_ascii=False),
             parsed["passage"],
             json.dumps(parsed["questions"], ensure_ascii=False), ts()))
        conn.commit()
    finally:
        conn.close()
    warnings = []
    if not parsed["dialogue"]:
        warnings.append("未解析到对话（DIALOGUE），Part B 将用系统兜底")
    if not parsed["passage"] or not parsed["questions"]:
        warnings.append("未解析到短文或选择题（PASSAGE/Q），Part C 不显示")
    return {
        "ok": True,
        "title": parsed["title"] or f"阶段{stage}·W{week}·D{day}",
        "dialogue_lines": len(parsed["dialogue"]),
        "blank_count": len(parsed["blanks"]),
        "question_count": len(parsed["questions"]),
        "replaced": replaced,
        "warnings": warnings,
    }


@app.get("/api/listening/{stage}/{week}/{day}")
def listening_get(stage: int, week: int, day: int):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT title, dialogue_json, passage, questions_json FROM listening_materials "
            "WHERE stage=? AND week=? AND day=?", (stage, week, day)).fetchone()
        prog = conn.execute(
            "SELECT listening_done, listening_total, parts_json FROM listening_progress "
            "WHERE stage=? AND week=? AND day=?", (stage, week, day)).fetchone()
    finally:
        conn.close()
    material = None
    if row:
        material = {
            "title": row["title"],
            "dialogue": json.loads(row["dialogue_json"] or "[]"),
            "passage": row["passage"] or "",
            "questions": json.loads(row["questions_json"] or "[]"),
        }
    progress = None
    if prog:
        progress = {
            "listening_done": prog["listening_done"] or 0,
            "listening_total": prog["listening_total"] or 0,
            "parts": json.loads(prog["parts_json"] or "{}"),
        }
    return {"material": material, "progress": progress}


@app.post("/api/listening/answer")
def listening_answer(body: dict):
    stage = int(body.get("stage", 0) or 0)
    week = int(body.get("week", 1) or 1)
    day = int(body.get("day", 1) or 1)
    part = str(body.get("part") or "A")
    correct = int(body.get("correct", 0) or 0)
    total = int(body.get("total", 0) or 0)
    conn = get_conn()
    try:
        prog = conn.execute(
            "SELECT parts_json FROM listening_progress "
            "WHERE stage=? AND week=? AND day=?", (stage, week, day)).fetchone()
        parts = json.loads(prog["parts_json"] or "{}") if prog else {}
        parts[part] = {"correct": correct, "total": total}
        new_done = sum(p.get("correct", 0) for p in parts.values())
        new_tot = sum(p.get("total", 0) for p in parts.values())
        conn.execute(
            "INSERT OR REPLACE INTO listening_progress "
            "(stage, week, day, listening_done, listening_total, parts_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (stage, week, day, new_done, new_tot,
             json.dumps(parts, ensure_ascii=False), ts()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------- 静态前端 ----------
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(os.path.join(FRONTEND_DIR, "index.html")):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
