# -*- coding: utf-8 -*-
"""专项训练（补习）四层数据落库。

背景
----
前端此前把 projects / sessions / rounds / attempts 四层数据全存在
localStorage（key = `eos_train_v1`），服务端只有两个空壳表：
`training_projects`（6 字段）和 `training_attempts`（7 字段），
而且**后端代码里从来没有任何一行往 training_attempts 写数据**。
后果有两个：

  1. 换设备 / 清缓存 → 训练记录全部蒸发；
  2. 训练数据对错误本、薄弱项、总结页完全不可见 —— 练了等于白练。

本模块补齐四层落库，对外只暴露两个接口：

  * `GET  /api/training/state`   读取四层全量状态
  * `POST /api/training/state`   保存四层全量状态（按业务 ID 做 upsert）

设计取舍
--------
前端的读写模式是「读整个 → 改 → 存整个」，所以后端就按**全量状态同步**来做，
而不是给每个按钮单独开细粒度接口。这样前端只需在 `trainLoad` / `trainSave`
上加 `await`，业务逻辑一行不改，交互行为完全等价。

老数据保护
----------
只 upsert / 删除**带业务 ID** 的行。`training_projects` 里没有 `project_key`
的历史行（比如走 `/api/test/projects` 建的）一律保留不动，绝不因为同步而误删。

词关联
------
专项训练的题目文本里通常含有一个关键英文词。保存项目时会从题目的 prompt 中
找出本地词典里确实存在的词，写进 item 的 `word` 字段；提交作答时再按
`question_id` 反查填入 `training_attempts.word`，把训练数据挂到「词」这条主线上。
猜不到就留空，不瞎猜。
"""
import json
import re

from db import get_conn

# 一条题目最多检查多少个候选词（防止长题目拖慢导入）
_WORD_SCAN_LIMIT = 20


# ---------- 序列化辅助 ----------
def _j(v, default=None):
    """list/dict → JSON 字符串。空值返回 default。"""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return default


def _p(v, default=None):
    """JSON 字符串 → list/dict。解析失败返回 default。"""
    if v is None or v == "":
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def _b(v):
    """任意真值 → 1/0（SQLite 与 PG 都无原生布尔，统一用整数）。"""
    return 1 if v else 0


def _i(v):
    try:
        return int(v or 0)
    except Exception:
        return 0


def _s(v):
    return "" if v is None else str(v)


# ---------- 词关联：从题目文本里找出确定的英文词 ----------
def _known_words(conn, candidates):
    """批量判断哪些候选词在本地词典里存在。返回命中的集合。"""
    out = set()
    for w in candidates[:_WORD_SCAN_LIMIT]:
        w = (w or "").lower().strip("'-")
        if len(w) < 2:
            continue
        try:
            if conn.execute("SELECT 1 FROM dictionary WHERE word = ? LIMIT 1", (w,)).fetchone():
                out.add(w)
        except Exception:
            break  # 词典表还没灌完也可能走到这，跳过即可，不影响落库
    return out


def _guess_word(conn, text):
    """从题目文本里挑出第一个确实存在于词典的英文词；找不到返回 ''。"""
    if not text:
        return ""
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]{1,}", str(text))
    hit = _known_words(conn, toks)
    for t in toks:
        w = t.lower().strip("'-")
        if w in hit:
            return w
    return ""


def _enrich_items(conn, items):
    """给题目补 word 字段（原本没有或为空时猜一个）。

    只在原值缺失时填充，不覆盖导入时已经明确给出的词。
    """
    if not isinstance(items, list):
        return items
    for q in items:
        if not isinstance(q, dict):
            continue
        if not (q.get("word") or "").strip():
            q["word"] = _guess_word(conn, q.get("prompt") or "")
    return items


def item_word_map(conn, project_key):
    """读取某项目的 question_id → word 映射，供写入 attempt 时反查。"""
    try:
        r = conn.execute(
            "SELECT items_json FROM training_projects WHERE project_key = ? LIMIT 1",
            (project_key,)).fetchone()
    except Exception:
        return {}
    items = _p(dict(r).get("items_json") if r else None, []) or []
    out = {}
    for q in items:
        if isinstance(q, dict) and q.get("id"):
            out[str(q["id"])] = (q.get("word") or "").strip()
    return out


# ---------- 读取 ----------
def _load_projects(conn):
    try:
        rows = conn.execute(
            "SELECT project_key, ability, problem, priority, intervention_level, "
            "training_goal, training_boundary, forbidden_json, exit_standard, "
            "exit_rule_json, status, items_json, created_at, updated_at, "
            "stage, week "
            "FROM training_projects "
            "WHERE project_key IS NOT NULL AND project_key <> '' ORDER BY id"
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "project_id": _s(d.get("project_key")),
            "ability": _s(d.get("ability")) or "OTHER",
            "problem": _s(d.get("problem")),
            "priority": _s(d.get("priority")) or "P2",
            "intervention_level": _s(d.get("intervention_level")) or "SUGGESTED",
            "training_goal": _s(d.get("training_goal")),
            "training_boundary": _s(d.get("training_boundary")),
            "forbidden": _p(d.get("forbidden_json"), []) or [],
            "exit_standard": _s(d.get("exit_standard")),
            "exit_rule": _p(d.get("exit_rule_json"), {}) or {},
            "status": _s(d.get("status")) or "NOT_STARTED",
            "items": _p(d.get("items_json"), []) or [],
            "created_at": _s(d.get("created_at")),
            "updated_at": _s(d.get("updated_at")),
            "stage": d.get("stage"),
            "week": d.get("week"),
        })
    return out


def _load_sessions(conn):
    try:
        rows = conn.execute(
            "SELECT session_id, project_key, started_at, ended_at, round_count, "
            "valid_attempts, correct_count, incorrect_count, hint_count, "
            "independent_correct_count, consecutive_independent_correct, "
            "final_status, next_step, created_at "
            "FROM training_sessions ORDER BY id"
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "session_id": _s(d.get("session_id")),
            "project_id": _s(d.get("project_key")),
            "started_at": _s(d.get("started_at")),
            "ended_at": d.get("ended_at"),
            "round_count": _i(d.get("round_count")),
            "valid_attempts": _i(d.get("valid_attempts")),
            "correct_count": _i(d.get("correct_count")),
            "incorrect_count": _i(d.get("incorrect_count")),
            "hint_count": _i(d.get("hint_count")),
            "independent_correct_count": _i(d.get("independent_correct_count")),
            "consecutive_independent_correct": _i(d.get("consecutive_independent_correct")),
            "final_status": d.get("final_status"),
            "next_step": d.get("next_step"),
            "created_at": _s(d.get("created_at")),
        })
    return out


def _load_rounds(conn):
    try:
        rows = conn.execute(
            "SELECT round_id, session_id, project_key, idx, started_at, ended_at "
            "FROM training_rounds ORDER BY id"
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "round_id": _s(d.get("round_id")),
            "session_id": _s(d.get("session_id")),
            "project_id": _s(d.get("project_key")),
            "index": _i(d.get("idx")),
            "started_at": _s(d.get("started_at")),
            "ended_at": d.get("ended_at"),
        })
    return out


def _load_attempts(conn):
    try:
        rows = conn.execute(
            "SELECT attempt_id, project_key, session_id, round_id, question_id, "
            "user_answer, is_correct, manual, used_hint, hint_level, "
            "is_independent, word, created_at "
            "FROM training_attempts "
            "WHERE attempt_id IS NOT NULL AND attempt_id <> '' ORDER BY id"
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "attempt_id": _s(d.get("attempt_id")),
            "project_id": _s(d.get("project_key")),
            "session_id": _s(d.get("session_id")),
            "round_id": _s(d.get("round_id")),
            "question_id": _s(d.get("question_id")),
            "user_answer": _s(d.get("user_answer")),
            "is_correct": bool(d.get("is_correct")),
            "manual": bool(d.get("manual")),
            "used_hint": bool(d.get("used_hint")),
            "hint_level": _i(d.get("hint_level")),
            "is_independent": bool(d.get("is_independent")),
            "word": _s(d.get("word")),
            "created_at": _s(d.get("created_at")),
        })
    return out


def load_state():
    """返回四层全量状态，形状与前端 localStorage 里的 eos_train_v1 一致。"""
    conn = get_conn()
    try:
        return {
            "ok": True,
            "projects": _load_projects(conn),
            "sessions": _load_sessions(conn),
            "rounds": _load_rounds(conn),
            "attempts": _load_attempts(conn),
        }
    finally:
        conn.close()


# ---------- 保存 ----------
def _delete_missing(conn, table, col, keep):
    """删掉前端已经移除的行。

    keep 为空表示前端把这一层整个清空了 —— 此时要删掉所有带业务 ID 的行，
    不能因为 `NOT IN ()` 是非法 SQL 就直接跳过（那样删不掉任何东西）。

    无论哪种情况都只删 col 非空的行：老数据（col 为 NULL）一律保留。
    """
    try:
        if keep:
            ph = ",".join(["?"] * len(keep))
            conn.execute(
                f"DELETE FROM {table} WHERE {col} IS NOT NULL AND {col} <> '' "
                f"AND {col} NOT IN ({ph})", list(keep))
        else:
            conn.execute(
                f"DELETE FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
    except Exception as e:
        print(f"[training._delete_missing] {table} 跳过:", e)


def _save_projects(conn, items):
    keys = []
    for p in items:
        if not isinstance(p, dict):
            continue
        k = _s(p.get("project_id")).strip()
        if not k:
            continue
        keys.append(k)
        items_json = _j(_enrich_items(conn, p.get("items") or []), "[]")
        conn.execute(
            "INSERT INTO training_projects (project_key, ability, problem, priority, "
            "intervention_level, training_goal, training_boundary, forbidden_json, "
            "exit_standard, exit_rule_json, status, items_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_key) DO UPDATE SET "
            "ability=excluded.ability, problem=excluded.problem, "
            "priority=excluded.priority, intervention_level=excluded.intervention_level, "
            "training_goal=excluded.training_goal, training_boundary=excluded.training_boundary, "
            "forbidden_json=excluded.forbidden_json, exit_standard=excluded.exit_standard, "
            "exit_rule_json=excluded.exit_rule_json, status=excluded.status, "
            "items_json=excluded.items_json, updated_at=excluded.updated_at",
            (k,
             _s(p.get("ability")) or "OTHER",
             _s(p.get("problem")),
             _s(p.get("priority")) or "P2",
             _s(p.get("intervention_level")) or "SUGGESTED",
             _s(p.get("training_goal")),
             _s(p.get("training_boundary")),
             _j(p.get("forbidden"), "[]"),
             _s(p.get("exit_standard")),
             _j(p.get("exit_rule"), "{}"),
             _s(p.get("status")) or "NOT_STARTED",
             items_json,
             _s(p.get("created_at")),
             _s(p.get("updated_at"))))
    _delete_missing(conn, "training_projects", "project_key", keys)
    return len(keys)


def _save_sessions(conn, items):
    keys = []
    for s in items:
        if not isinstance(s, dict):
            continue
        k = _s(s.get("session_id")).strip()
        if not k:
            continue
        keys.append(k)
        conn.execute(
            "INSERT INTO training_sessions (session_id, project_key, started_at, ended_at, "
            "round_count, valid_attempts, correct_count, incorrect_count, hint_count, "
            "independent_correct_count, consecutive_independent_correct, "
            "final_status, next_step) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "project_key=excluded.project_key, started_at=excluded.started_at, "
            "ended_at=excluded.ended_at, round_count=excluded.round_count, "
            "valid_attempts=excluded.valid_attempts, correct_count=excluded.correct_count, "
            "incorrect_count=excluded.incorrect_count, hint_count=excluded.hint_count, "
            "independent_correct_count=excluded.independent_correct_count, "
            "consecutive_independent_correct=excluded.consecutive_independent_correct, "
            "final_status=excluded.final_status, next_step=excluded.next_step",
            (k, _s(s.get("project_id")), _s(s.get("started_at")), s.get("ended_at"),
             _i(s.get("round_count")), _i(s.get("valid_attempts")),
             _i(s.get("correct_count")), _i(s.get("incorrect_count")),
             _i(s.get("hint_count")), _i(s.get("independent_correct_count")),
             _i(s.get("consecutive_independent_correct")),
             s.get("final_status"), s.get("next_step")))
    _delete_missing(conn, "training_sessions", "session_id", keys)
    return len(keys)


def _save_rounds(conn, items):
    keys = []
    for r in items:
        if not isinstance(r, dict):
            continue
        k = _s(r.get("round_id")).strip()
        if not k:
            continue
        keys.append(k)
        conn.execute(
            "INSERT INTO training_rounds (round_id, session_id, project_key, idx, "
            "started_at, ended_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(round_id) DO UPDATE SET "
            "session_id=excluded.session_id, project_key=excluded.project_key, "
            "idx=excluded.idx, started_at=excluded.started_at, ended_at=excluded.ended_at",
            (k, _s(r.get("session_id")), _s(r.get("project_id")),
             _i(r.get("index")), _s(r.get("started_at")), r.get("ended_at")))
    _delete_missing(conn, "training_rounds", "round_id", keys)
    return len(keys)


def _save_attempts(conn, items, word_map=None):
    """写入作答记录。

    word_map: {project_key: {question_id: word}}，用于把作答挂到具体的词上。
    """
    keys = []
    wmap = word_map or {}
    for a in items:
        if not isinstance(a, dict):
            continue
        k = _s(a.get("attempt_id")).strip()
        if not k:
            continue
        keys.append(k)
        pk = _s(a.get("project_id"))
        word = _s(a.get("word")).strip()
        if not word:
            word = (wmap.get(pk) or {}).get(_s(a.get("question_id")), "") or ""
        conn.execute(
            "INSERT INTO training_attempts (attempt_id, project_key, session_id, round_id, "
            "question_id, user_answer, is_correct, manual, used_hint, hint_level, "
            "is_independent, word, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(attempt_id) DO UPDATE SET "
            "session_id=excluded.session_id, round_id=excluded.round_id, "
            "question_id=excluded.question_id, user_answer=excluded.user_answer, "
            "is_correct=excluded.is_correct, manual=excluded.manual, "
            "used_hint=excluded.used_hint, hint_level=excluded.hint_level, "
            "is_independent=excluded.is_independent, word=excluded.word",
            (k, pk, _s(a.get("session_id")), _s(a.get("round_id")),
             _s(a.get("question_id")), _s(a.get("user_answer")),
             _b(a.get("is_correct")), _b(a.get("manual")), _b(a.get("used_hint")),
             _i(a.get("hint_level")), _b(a.get("is_independent")),
             word, _s(a.get("created_at"))))
    _delete_missing(conn, "training_attempts", "attempt_id", keys)
    return len(keys)


def save_state(payload):
    """全量保存四层状态。payload 形状与前端 localStorage 一致。"""
    payload = payload or {}
    projects = payload.get("projects") or []
    sessions = payload.get("sessions") or []
    rounds = payload.get("rounds") or []
    attempts = payload.get("attempts") or []

    conn = get_conn()
    try:
        n_p = _save_projects(conn, projects)
        n_s = _save_sessions(conn, sessions)
        n_r = _save_rounds(conn, rounds)
        # attempts 的 word 需要从所属项目的题目表里反查
        wmap = {}
        for pk in {_s(a.get("project_id")) for a in attempts if isinstance(a, dict)}:
            if pk and pk not in wmap:
                wmap[pk] = item_word_map(conn, pk)
        n_a = _save_attempts(conn, attempts, wmap)
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "counts": {
        "projects": n_p, "sessions": n_s, "rounds": n_r, "attempts": n_a}}
