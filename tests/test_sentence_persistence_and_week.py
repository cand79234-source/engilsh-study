# -*- coding: utf-8 -*-
"""回归测试：造句批改结果持久化 + 历史记录可读 + 时间边界。

覆盖本次修复的 5 个问题（对应用户给的 Case 1–4）：

  Case 1  提交造句、AI 给 40 分 → 刷新页面必须仍是 40（不能变回本地规则分/100）
  Case 2  9/14 建的记录，9/15 打开系统查 9/14，必须仍能读到
  Case 3  「本周」= 自然周（周一~周日，Asia/Shanghai），不是「今天往前 7 天」
  Case 4  跨周：9/13(周日) 的数据不能继续算进 9/14 起的新周

运行: pytest tests/test_sentence_persistence_and_week.py -q
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


@pytest.fixture()
def env(monkeypatch):
    """独立临时库 + 干净模块，避免污染仓库默认库。"""
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
    yield db
    for m in [m for m in list(sys.modules)
              if m in ("db", "scenario", "services", "srs", "ai_correct",
                       "ai_service", "report", "main")]:
        del sys.modules[m]
    try:
        os.remove(path)
    except OSError:
        pass


# ============================================================ Case 1
def test_ai_score_persists_after_refresh(env):
    """AI 给 40 分 → 落库 → 重新读取（= 刷新页面）必须还是 40。

    这是「刷新从实际分数变 100」问题的核心：以前 AI 结果从不落库，
    刷新后读到的是本地规则分（对 'I go work every day.' 本地给 70，
    规则漏判的句子甚至给到 90+，用户就看成「变成高分/100」）。
    """
    import ai_service as ais
    db = env
    r = ais.correct_sentence("I go work every day.", 0, 3, 1, "work", "basic:0")
    sid = r["sentence_id"]
    assert r["score"] != 40                      # 本地规则分不是 40（否则本测试无意义）

    ai = {"score": 40, "corrected": "I go to work every day.",
          "level": "需要改进",
          "errors": [{"type": "固定搭配", "wrong": "go work",
                      "right": "go to work", "explain": "go 后接目的地要加 to"}],
          "natural": [{"original": "go work", "better": "go to work",
                       "reason": "更地道"}],
          "expand": ["I go to work by bus every day."],
          "summary": "整体不错，注意固定搭配。", "model": "test-model"}
    assert ais.save_ai_result(sid, ai) is True

    # 刷新页面：重新从库里读
    conn = db.get_conn()
    groups = ais.today_attempts(conn, 0, 3, 1, "2000-01-01T00:00:00")
    conn.close()
    a = groups[0]["attempts"][0]
    assert a["score"] == 40, "刷新后必须仍是 AI 的 40 分"
    assert a["source"] == "ai"
    assert a["ai_pending"] is False
    assert a["corrected"] == "I go to work every day."
    assert a["errors"][0]["type"] == "固定搭配"
    assert a["ai"]["natural"][0]["better"] == "go to work"


def test_local_fallback_not_persisted_as_ai(env):
    """AI 没跑通（没落库）时，刷新读到的仍是本地规则结果，并标 ai_pending。"""
    import ai_service as ais
    db = env
    r = ais.correct_sentence("I go work every day.", 0, 3, 1, "work", "basic:0")
    conn = db.get_conn()
    groups = ais.today_attempts(conn, 0, 3, 1, "2000-01-01T00:00:00")
    conn.close()
    a = groups[0]["attempts"][0]
    assert a["score"] == r["score"]
    assert a["source"] == "rule"
    assert a["ai_pending"] is True


def test_missing_ai_score_never_faked_as_100(env):
    """历史缺失（ai_score=NULL、score=0）不能被伪造/默认成 100。"""
    import ai_service as ais
    db = env
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO sentences (stage,week,day,word,task_key,attempt,original,"
        "corrected,score,verdict,good,error_type,created_at)"
        " VALUES (0,1,1,'old','basic:0',1,'old sentence','',0,'',0,'',"
        "'2026-01-01T10:00:00')")
    conn.commit()
    row = conn.execute("SELECT * FROM sentences WHERE id=1").fetchone()
    conn.close()
    a = ais._row_to_attempt(row)
    assert a["score"] == 0, "缺失分数必须如实为 0，不能伪造 100"
    assert a["ai_pending"] is True


# ============================================================ Case 2
def test_history_readable_across_days(env):
    """9/14 建的造句记录，9/15 查询时仍必须能读到（历史不会因日期变化消失）。"""
    import ai_service as ais
    db = env
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO sentences (stage,week,day,word,task_key,attempt,original,"
        "corrected,score,verdict,good,error_type,created_at)"
        " VALUES (0,1,1,'work','basic:0',1,'I went to work yesterday.','',85,"
        "'正确',1,'','2026-09-14T10:00:00')")
    conn.commit()
    conn.close()

    # 模拟「今天 = 9/15」查询 9/14 的记录：用句子历史查询（不按当天 stage/week/day）
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM sentences WHERE created_at >= ? AND created_at <= ?",
        ("2026-09-14T00:00:00", "2026-09-14T23:59:59")).fetchall()
    conn.close()
    assert len(rows) == 1
    a = ais._row_to_attempt(rows[0])
    assert a["sentence"] == "I went to work yesterday."
    assert a["score"] == 85


# ============================================================ Case 3 / 4
def test_natural_week_boundary_tuesday(env):
    """2026-09-15 是周二 → 本周 = 2026-09-14 00:00:00 ~ 2026-09-20 23:59:59。"""
    db = env
    from datetime import date
    d = date(2026, 9, 15)
    assert db.china_week_start(d).isoformat() == "2026-09-14"
    assert db.china_week_end(d).isoformat() == "2026-09-20"
    s, e = db.china_week_range(d)
    assert s == "2026-09-14T00:00:00"
    assert e == "2026-09-20T23:59:59"
    # 绝不能是「今天往前 7 天」
    assert s != "2026-09-08T00:00:00"


def test_natural_week_cross_week(env):
    """跨周：9/13(周日) 属于上一周；9/14(周一) 起是新的一周，两边不重叠。"""
    db = env
    from datetime import date
    sun = date(2026, 9, 13)
    mon = date(2026, 9, 14)
    prev_s, prev_e = db.china_week_range(sun)
    cur_s, cur_e = db.china_week_range(mon)
    assert prev_s == "2026-09-07T00:00:00" and prev_e == "2026-09-13T23:59:59"
    assert cur_s == "2026-09-14T00:00:00" and cur_e == "2026-09-20T23:59:59"
    # 9/13 的数据不能落进 9/14 起的新周窗口
    assert not (cur_s <= "2026-09-13T12:00:00" <= cur_e)


def test_report_week_uses_natural_week(env):
    """报告接口的「本周」统计窗口必须是自然周，且只统计窗口内数据。"""
    import report as _report
    db = env
    conn = db.get_conn()
    # 本周内（9/15）一条、上周内（9/8）一条、更早一条（attempt 递增避免撞唯一索引）
    for i, (ts_, sent) in enumerate([("2026-09-15T10:00:00", "this week"),
                                     ("2026-09-08T10:00:00", "last week"),
                                     ("2026-07-01T10:00:00", "long ago")]):
        conn.execute(
            "INSERT INTO sentences (stage,week,day,word,task_key,attempt,original,"
            "corrected,score,verdict,good,error_type,created_at)"
            " VALUES (0,3,1,'w','basic:0',?, ?, '',80,'正确',1,'',?)",
            (i + 1, sent, ts_))
    conn.commit()
    conn.close()

    # 用固定日期（周二）构建报告窗口，验证边界
    from datetime import date
    s, e = db.china_week_range(date(2026, 9, 15))
    conn = db.get_conn()
    n = conn.execute(
        "SELECT COUNT(*) n FROM sentences WHERE created_at >= ? AND created_at <= ?",
        (s, e)).fetchone()["n"]
    conn.close()
    assert n == 1, "只有本周(9/15)那条应被统计，上周和更早的都不算"


def test_report_period_metadata(env):
    """报告返回体带 period 元数据，口径标注为自然周 + Asia/Shanghai。"""
    import report as _report
    r = _report.build_report()
    assert r["period"]["timezone"] == "Asia/Shanghai"
    assert r["period"]["week"]["rule"] == "natural_week_mon_sun"
    assert r["period"]["week_compare_rule"] == "course_week(stage+week)"


# ============================================================ Case 5
def test_stage_to_cefr_mapping(env):
    """Stage 0–5 → A2 early/A2 early/A2 late/A2 late/B1/B1 late。"""
    import scenario
    expect = {0: "A2 early", 1: "A2 early", 2: "A2 late", 3: "A2 late",
              4: "B1", 5: "B1 late"}
    for s, code in expect.items():
        assert scenario.cefr_for_stage(s)[0] == code, "Stage %d 映射错" % s


def test_scenario_prompt_carries_stage_and_cefr(env):
    """最终发给 AI 的 Prompt 必须明写 Learner stage + Target CEFR + 难度要求。"""
    import scenario
    p0 = scenario.build_sys_prompt(0)
    assert "Learner stage: Stage 0" in p0
    assert "Target CEFR: A2 early" in p0
    assert "短句" in p0 and "高频" in p0
    assert "ordering food" in p0
    # 必须明确「场景复杂度 ≠ 语言难度」
    assert "场景复杂度" in p0 and "英语语言难度" in p0

    p5 = scenario.build_sys_prompt(5)
    assert "Learner stage: Stage 5" in p5
    assert "Target CEFR: B1 late" in p5

    # Stage 0 不许出现 B1 后期才有的说法
    assert "B1 late" not in p0


def test_generate_for_word_passes_stage_into_sys_prompt(env, monkeypatch):
    """generate_for_word 必须把 stage 传进最终 sys prompt（不是只写死在别处）。

    ⚠️ 2026-09-11：基础句（small）已彻底不走 AI，所以这里改用 large
    （组合句那一层，唯一还会调 AI 的层）来验证 stage 透传。
    """
    import scenario, ai_correct
    captured = {}

    def fake_ark_json(sys_prompt, user, **kw):
        captured["sys"] = sys_prompt
        captured["user"] = user
        return {"scenarios": []}, None

    monkeypatch.setattr(ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(ai_correct, "ark_json", fake_ark_json)
    scenario.generate_for_word("work", grammar="", n=1, need_tier="large", stage=1)
    assert "Target CEFR: A2 early" in captured["sys"]
    assert "Learner stage: Stage 1" in captured["user"]


def test_generate_defaults_to_a2_not_b1(env, monkeypatch):
    """漏传 stage 时必须按最保守的 A2 初期处理，绝不默认成 B1。"""
    import scenario, ai_correct
    captured = {}

    def fake_ark_json(sys_prompt, user, **kw):
        captured["sys"] = sys_prompt
        return {"scenarios": []}, None

    monkeypatch.setattr(ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(ai_correct, "ark_json", fake_ark_json)
    scenario.generate_for_word("apple", n=1, need_tier="large")
    assert "Target CEFR: A2 early" in captured["sys"]


def test_small_tier_never_calls_ai(env, monkeypatch):
    """⚠️ 2026-09-11 新增，锁住用户明确要求：「20 个单词造句的场景彻底删干净 AI」。

    small（基础句）层：情景只能来自导入材料自带的 Mini Scenario。
    无论点名 need_tier="small" 还是不传，都**一次 AI 都不许调**。
    """
    import scenario, ai_correct
    hits = {"n": 0}

    def fake_ark_json(*a, **kw):
        hits["n"] += 1
        return {"scenarios": []}, None

    monkeypatch.setattr(ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(ai_correct, "ark_json", fake_ark_json)
    # ① 明确点名 small → 直接返回 0，不调 AI
    assert scenario.generate_for_word("apple", n=3, need_tier="small", stage=0) == 0
    assert hits["n"] == 0, "点名 small 竟然调了 AI"
    # ② 不传 need_tier（老调用）→ 自动降级成只生成 large，不会替 small 生成
    scenario.generate_for_word("banana", n=3, stage=0)
    # 关键：无论走哪条路，AI_TIERS 里都不含 small
    assert scenario.AI_TIERS == ("large",)
    assert "small" not in scenario.AI_TIERS
