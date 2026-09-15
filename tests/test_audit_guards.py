# -*- coding: utf-8 -*-
"""回归测试：审阅报告四疑点的「永久守卫」。

对应一次性审计脚本（/tmp/q1~q4），这里固化成 pytest，防止将来回归：
  ① 同一道题多次/跨天造句 → 历史完整、attempt 升序、最新 = attempt 最大者
  ② 组合句 3 复习 + 2 新的严格配比（穷举复习词 0~12）
  ③ stage 从调用方一路进到 AI 的 system prompt（Phase 规则原样带上）
  ④ 并发/连点/超时/残缺 AI 都不产生重复行或脏数据，唯一索引硬兜底

运行: pytest tests/test_audit_guards.py -q
"""
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("EOS_DB", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_TZ", "Asia/Shanghai")
    for m in [m for m in list(sys.modules)
              if m in ("db", "scenario", "services", "srs", "ai_correct",
                       "ai_service", "report", "main")]:
        del sys.modules[m]
    import db
    db.init_db()
    db.ensure_unique_indexes()
    yield db
    for m in [m for m in list(sys.modules)
              if m in ("db", "scenario", "services", "srs", "ai_correct",
                       "ai_service", "report", "main")]:
        del sys.modules[m]
    try:
        os.remove(path)
    except OSError:
        pass


# ============================================================ 疑点 ①
def test_multi_attempt_history_intact_and_latest_is_max(env):
    """同一道题连造 3 次：历史 3 条、attempt 升序、最新 = attempt 最大者。"""
    import ai_service as ais
    db = env
    for i in range(3):
        ais.correct_sentence("I take the medicine every day.", 4, 2, 1,
                             "medicine", "basic:medicine:0")
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT attempt FROM sentences WHERE stage=4 AND week=2 AND day=1"
        " AND task_key=? ORDER BY attempt", ("basic:medicine:0",)).fetchall()
    conn.close()
    atts = [r["attempt"] for r in rows]
    assert atts == [1, 2, 3], "历史必须完整保留且 attempt 升序"
    # 前端「最新」= 数组最后一条 = attempt 最大者
    assert atts[-1] == max(atts)


def test_attempts_visible_next_day(env):
    """跨天：改了 created_at 也必须仍能读到（不按当天过滤）。"""
    import ai_service as ais
    from ai_service import today_attempts
    db = env
    ais.correct_sentence("I take the medicine every day.", 4, 2, 1,
                         "medicine", "basic:medicine:0")
    conn = db.get_conn()
    conn.execute("UPDATE sentences SET created_at='2020-01-01T00:00:00'")
    conn.commit()
    conn.close()
    got = today_attempts(db.get_conn(), 4, 2, 1, "2020-01-01T00:00:00")
    by_tk = {g["task_key"]: g for g in got}
    assert "basic:medicine:0" in by_tk, "跨天后历史记录不能凭空消失"
    assert len(by_tk["basic:medicine:0"]["attempts"]) == 1


# ============================================================ 疑点 ②
def test_combo_ratio_strict_3_review_2_new(env):
    """组合句每组必须严格 3 复习 + 2 新；复习词零重复。"""
    import services as svc
    for n_rev in range(0, 13):
        review = [{"word": "r%d" % i, "meaning": ""} for i in range(n_rev)]
        new = [{"word": "n%d" % i, "meaning": ""} for i in range(20)]
        groups = svc._build_combos(new, review, "", seed=1, n=10, per=5)
        for combo in groups:
            g = combo["words"]
            assert len(g) == 5, "每组必须 5 个词（复习 %d）" % n_rev
            nrev = sum(1 for x in g if x.get("review"))
            nnew = sum(1 for x in g if not x.get("review"))
            assert (nrev, nnew) == (3, 2), \
                "复习=%d 时出现 %d+%d，违反 3+2" % (n_rev, nrev, nnew)
        # 复习词不许跨组重复
        used = [x["word"] for combo in groups for x in combo["words"]
                if x.get("review")]
        assert len(used) == len(set(used)), "复习词跨组重复了（复习 %d）" % n_rev
        # 复习不够时组数必然变少，绝不能硬凑
        assert len(groups) == min(10, n_rev // 3)


# ============================================================ 疑点 ③
def test_stage_reaches_ai_system_prompt(env, monkeypatch):
    """stage 必须真的进 system prompt（Phase 规则 + CEFR），而不只是写在代码里。"""
    import scenario as sc
    captured = []

    def fake_ark(system, user, **kw):
        captured.append((system, user))
        return {"scenarios": [{"tier": "large", "scenario": "s", "scenario_cn": "中"}]}, None

    monkeypatch.setattr(sc.ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(sc.ai_correct, "ark_json", fake_ark)
    sc.generate_for_word("medicine", "", need_tier="large", stage=4)
    assert captured, "应当调用了 AI"
    system, user = captured[0]
    assert "Learner stage" in system
    assert "【场景难度硬性规则】" in system, "Phase 硬性规则没进 system prompt"
    assert "Phase 4" in system
    assert "Learner stage: Stage 4" in user, "user 里没有真实 stage"
    assert "Target CEFR: B1" in user, "user 里没写目标 CEFR"


def test_small_tier_never_calls_ai(env, monkeypatch):
    """基础句（small）绝不再调 AI —— 直接返回 0，一次都不打。"""
    import scenario as sc
    called = []
    monkeypatch.setattr(sc.ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(sc.ai_correct, "ark_json",
                        lambda *a, **k: called.append(1) or ({}, None))
    n = sc.generate_for_word("medicine", "", need_tier="small", stage=4)
    assert n == 0
    assert not called, "small 层不该调 AI"


# ============================================================ 疑点 ④
def test_concurrent_submit_no_duplicate_attempt(env):
    """20 线程并发提交同一道题：无撞号、无空洞、不崩。"""
    import ai_service as ais
    db = env
    errs = []

    def worker(_):
        try:
            ais.correct_sentence("I take the medicine every day.", 4, 2, 1,
                                 "medicine", "basic:medicine:0")
        except Exception as e:      # noqa: BLE001
            errs.append(repr(e))

    ths = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    assert not errs, "并发不应抛异常: %s" % errs[:2]
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT attempt) d FROM sentences"
        " WHERE task_key=?", ("basic:medicine:0",)).fetchone()
    atts = [r["attempt"] for r in conn.execute(
        "SELECT attempt FROM sentences WHERE task_key=? ORDER BY attempt",
        ("basic:medicine:0",)).fetchall()]
    conn.close()
    assert rows["n"] == rows["d"] == 20, "并发产生了重复 attempt"
    assert atts == list(range(1, 21)), "attempt 必须恰好连号（无空洞/错位）"


def test_timeout_and_partial_ai_leave_no_dirty_data(env):
    """AI 超时（ai=None）/ 残缺（无 score）：只留本地分，不写半个 AI 结果。"""
    import ai_service as ais
    db = env
    ais.correct_sentence("I take the medicine every day.", 4, 2, 1,
                         "medicine", "basic:medicine:t", ai=None)
    ais.correct_sentence("I take the medicine every day.", 4, 2, 1,
                         "medicine", "basic:medicine:b", ai={"corrected": "x"})
    conn = db.get_conn()
    for tk in ("basic:medicine:t", "basic:medicine:b"):
        r = conn.execute(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN ai_score IS NULL THEN 1 ELSE 0 END) noai,"
            " SUM(CASE WHEN final_source='ai' THEN 1 ELSE 0 END) srcai"
            " FROM sentences WHERE task_key=?", (tk,)).fetchone()
        assert r["n"] == 1, "%s 超时/残缺不该重复" % tk
        assert r["noai"] == 1, "%s 不该写入半个 AI 结果" % tk
        assert (r["srcai"] or 0) == 0
    conn.close()


def test_unique_index_blocks_duplicate_attempt(env):
    """唯一索引 ux_sentences_attempt 必须真的挡住同 attempt 重复行。"""
    import ai_service as ais       # noqa: F401
    db = env
    ais.correct_sentence("I take the medicine every day.", 4, 2, 1,
                         "medicine", "basic:medicine:0")
    conn = db.get_conn()
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO sentences (stage, week, day, word, task_key, attempt,"
            " original, corrected, error_type, explanation, ai_source, good,"
            " score, verdict, errors_json, opts_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (4, 2, 1, "medicine", "basic:medicine:0", 1, "dup", "", "", "",
             "rule", 1, 90, "", "[]", "[]", "2020-01-01T00:00:00"))
    conn.rollback()
    conn.close()
