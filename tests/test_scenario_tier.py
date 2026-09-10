"""情景分层取景 / 按天补齐的回归测试。

覆盖 2026-09-10 修的三个问题（都是"看一眼就被当成用光了"引起的）：

  1. pick() 取景**不再递增 used_count** —— 用户只是看页面，不该被记成消耗；
  2. 取景轮换靠 exclude_id 循环（不靠消耗），只有 1 条时也不开天窗；
  3. 库存判断改成**按 tier** —— 以前只数总数，「有 3 条 small」就以为够了，
     large 永远 0 条 → 组合句一直没情景；
  4. 生成只产 small / large（medium 是删掉升级句后的死层，白烧额度）；
  5. 按天补齐：backfill_step 从第一天起顺序找缺口，补齐一天再往后走。
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture()
def env(monkeypatch):
    """每个测试用一个独立临时库，避免互相污染。

    ⚠️ 本仓库的测试默认直接写 data/english_os.db（没有 conftest 做隔离），
    反复跑会残留数据、甚至撞 UNIQUE 约束 —— 所以新测试自己建临时库。
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # db.py 读的是 EOS_DB（见 db.py:12）
    os.environ["EOS_DB"] = path
    os.environ.pop("DATABASE_URL", None)
    # 让所有会读库的模块重新按新路径初始化（services 也持连接，必须一起清）
    for m in [m for m in list(sys.modules)
              if m in ("db", "scenario", "services", "srs", "ai_correct")]:
        del sys.modules[m]
    import db
    db.init_db()
    import scenario
    yield scenario, db
    os.environ.pop("EOS_DB", None)
    for m in [m for m in list(sys.modules)
              if m in ("db", "scenario", "services", "srs", "ai_correct")]:
        del sys.modules[m]
    try:
        os.remove(path)
    except OSError:
        pass


def _ins(conn, word, tier, prompt="p"):
    conn.execute(
        "INSERT INTO word_scenarios (word,tier,prompt,grammar,used_count,created_at)"
        " VALUES (?,?,?,'',0,'2026-09-10')", (word, tier, prompt))
    conn.commit()


# ------------------------------------------------ ① 取景不消耗
def test_pick_does_not_consume(env):
    """取景（=用户看一眼）绝不递增 used_count。"""
    sc, db = env
    conn = db.get_conn()
    _ins(conn, "apple", "small")
    _ins(conn, "apple", "small")
    for _ in range(10):
        sc.pick("apple", tier="small")
    n = conn.execute("SELECT COUNT(*) n FROM word_scenarios"
                     " WHERE word='apple' AND used_count>0").fetchone()["n"]
    assert n == 0, "取景不应该消耗情景"


# ------------------------------------------------ ② 轮换靠 exclude_id
def test_pick_rotates_by_exclude_id(env):
    """连点 🔁 应能轮出所有情景，而不是反复给同一条。"""
    sc, db = env
    conn = db.get_conn()
    for _ in range(3):
        _ins(conn, "apple", "small")
    seen, cur = set(), 0
    for _ in range(6):
        r = sc.pick("apple", exclude_id=cur, tier="small")
        assert r, "不该取不到"
        seen.add(r["id"])
        cur = r["id"]
    assert len(seen) == 3, "应轮出全部 3 条，实际 %d 条" % len(seen)


def test_pick_single_row_never_blank(env):
    """只有 1 条时，🔁 也要把它给出去（不开天窗）。"""
    sc, db = env
    conn = db.get_conn()
    _ins(conn, "solo", "large")
    r1 = sc.pick("solo", tier="large")
    assert r1
    r2 = sc.pick("solo", exclude_id=r1["id"], tier="large")
    assert r2 and r2["id"] == r1["id"]


def test_pick_first_view_is_stable(env):
    """不传 exclude_id（首次进页面）时固定取第一条，不来回抖。"""
    sc, db = env
    conn = db.get_conn()
    for _ in range(3):
        _ins(conn, "apple", "small")
    a = sc.pick("apple", tier="small")
    b = sc.pick("apple", tier="small")
    assert a["id"] == b["id"]


# ------------------------------------------------ ③ 按 tier 判断库存
def test_count_of_is_tier_aware(env):
    """count_of 要能只看某一层 —— 这是 large 补不上的根因修复。"""
    sc, db = env
    conn = db.get_conn()
    for _ in range(3):
        _ins(conn, "apple", "small")
    assert sc.count_of("apple", "small") == 3
    assert sc.count_of("apple", "large") == 0, "该词没有 large，必须能查出来"
    assert sc.count_of("apple") == 3


def test_tier_target_excludes_medium(env):
    """medium 是删掉升级句后的死层，不该再被补。"""
    sc, _ = env
    assert "medium" not in sc.TIER_TARGET
    assert set(sc.TIER_TARGET) == {"small", "large"}


# ------------------------------------------------ ④ 生成配额只含 small/large
def test_generate_prompt_excludes_medium(env, monkeypatch):
    """提示词里只要求 small / large，明确不要 medium。"""
    sc, db = env
    captured = {}

    def fake_ark(sys_p, user_p, **kw):
        captured["user"] = user_p
        return {"scenarios": [{"tier": "small", "prompt": "这是一个小情景的正文"}]}, None

    monkeypatch.setattr(sc.ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(sc.ai_correct, "ark_json", fake_ark)
    monkeypatch.setattr(sc, "_lookup_word", lambda w: ("苹果", "n"))
    sc.generate_for_word("apple", n=4)
    u = captured.get("user", "")
    assert "small" in u and "large" in u
    assert "不要 medium" in u, "应明确排除 medium"


def test_generate_respects_need_tier(env, monkeypatch):
    """need_tier='large' 时，AI 若返回 small 就不入库（认准要补的层）。"""
    sc, db = env

    def fake_ark(sys_p, user_p, **kw):
        return {"scenarios": [
            {"tier": "small", "prompt": "错误层级的正文内容"},
            {"tier": "large", "prompt": "正确层级的大情景正文"},
        ]}, None

    monkeypatch.setattr(sc.ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(sc.ai_correct, "ark_json", fake_ark)
    monkeypatch.setattr(sc, "_lookup_word", lambda w: ("", ""))
    sc.generate_for_word("apple", need_tier="large")
    assert sc.count_of("apple", "large") == 1
    assert sc.count_of("apple", "small") == 0, "错层的货不能入进来"


# ------------------------------------------------ ⑤ 按天补齐
def _seed_days(conn, n_days=3, per=2):
    for day in range(1, n_days + 1):
        for i in range(per):
            conn.execute(
                "INSERT INTO day_items (stage,week,day,kind,ref_key,payload_json,"
                "mastered,created_at) VALUES (1,1,?, 'vocab',?,'{}',0,'2026-09-10')",
                (day, "w%da%d" % (day, i)))
    conn.commit()


def test_bf_days_ordered(env):
    """_bf_days 必须按 week/day 升序，且带上每天的词。"""
    sc, db = env
    conn = db.get_conn()
    _seed_days(conn)
    days = sc._bf_days()
    assert [d["day"] for d in days] == [1, 2, 3]
    assert days[0]["words"] and len(days[0]["words"]) == 2


def test_backfill_status_reports_gaps(env):
    """status 要如实报出每天缺多少（每词缺 small+large = 2 项）。"""
    sc, db = env
    conn = db.get_conn()
    _seed_days(conn, n_days=2, per=2)
    st = sc.backfill_status()
    assert st["total_days"] == 2
    assert st["all_done"] is False
    for d in st["days"]:
        assert d["missing"] == 4, "2 个词 × 2 层 = 4 项缺口"


def test_backfill_all_done_when_full(env):
    """全部齐了就该报 all_done=True。"""
    sc, db = env
    conn = db.get_conn()
    _seed_days(conn, n_days=1, per=1)
    for t in ("small", "large"):
        for _ in range(3):
            _ins(conn, "w1a0", t)
    st = sc.backfill_status()
    assert st["all_done"] is True


def test_backfill_step_safe_without_key(env):
    """没配 AI Key 时应安全返回 0，不抛异常。"""
    sc, _ = env
    assert sc.backfill_step(force=True) == 0


def test_backfill_step_empty_db(env):
    """空库（没有任何 day_items）也不能崩。"""
    sc, _ = env
    assert sc.backfill_step(force=True) == 0
    assert sc.backfill_status()["total_days"] == 0


def test_backfill_step_fills_first_gap(env, monkeypatch):
    """补齐应从第一天起补，补完该有的条数后缺口归零。"""
    sc, db = env
    conn = db.get_conn()
    _seed_days(conn, n_days=3, per=1)

    def fake_ark(sys_p, user_p, **kw):
        # 一次返回满足 TIER_TARGET 的条数（3 条），模拟"一次补齐"
        if '"large"' in user_p:
            return {"scenarios": [{"tier": "large", "prompt": "大情景内容第 %d 条的正文" % i}
                                  for i in range(3)]}, None
        return {"scenarios": [{"tier": "small", "prompt": "小情景内容第 %d 条的正文" % i}
                              for i in range(3)]}, None

    monkeypatch.setattr(sc.ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(sc.ai_correct, "ark_json", fake_ark)
    monkeypatch.setattr(sc, "_lookup_word", lambda w: ("", ""))

    before = sc.backfill_status()
    assert before["days"][0]["missing"] > 0
    n = sc.backfill_step(force=True, max_words=1)
    assert n > 0, "应该补上了东西"
    after = sc.backfill_status()
    assert after["days"][0]["missing"] < before["days"][0]["missing"], \
        "第一天的缺口应该变小（补齐优先从第一天开始）"


def test_backfill_cursor_advances(env, monkeypatch):
    """某天齐了之后，游标应该往后走，而不是卡死在第一天。"""
    sc, db = env
    conn = db.get_conn()
    _seed_days(conn, n_days=3, per=1)

    def fake_ark(sys_p, user_p, **kw):
        if '"large"' in user_p:
            return {"scenarios": [{"tier": "large", "prompt": "大情景内容第 %d 条的正文" % i}
                                  for i in range(3)]}, None
        return {"scenarios": [{"tier": "small", "prompt": "小情景内容第 %d 条的正文" % i}
                              for i in range(3)]}, None

    monkeypatch.setattr(sc.ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(sc.ai_correct, "ark_json", fake_ark)
    monkeypatch.setattr(sc, "_lookup_word", lambda w: ("", ""))

    for _ in range(20):
        if sc.backfill_step(force=True, max_words=1) == 0:
            break
    st = sc.backfill_status()
    assert st["all_done"] is True, "多轮之后应该全部补齐"


def test_backfill_prioritizes_today(env, monkeypatch):
    """今天要学的那天必须最先补 —— 用户马上要用，不能排在后面。"""
    sc, db = env
    conn = db.get_conn()
    _seed_days(conn, n_days=4, per=1)
    # 把学习位置切到 Day3
    conn.execute("UPDATE progress SET stage=1, week=1, day=3 WHERE id=1")
    conn.commit()

    calls = []

    def fake_ark(sys_p, user_p, **kw):
        calls.append(user_p)
        if '"large"' in user_p:
            return {"scenarios": [{"tier": "large", "prompt": "大情景内容第 %d 条的正文" % i}
                                  for i in range(3)]}, None
        return {"scenarios": [{"tier": "small", "prompt": "小情景内容第 %d 条的正文" % i}
                              for i in range(3)]}, None

    monkeypatch.setattr(sc.ai_correct, "ai_enabled", lambda: True)
    monkeypatch.setattr(sc.ai_correct, "ark_json", fake_ark)
    monkeypatch.setattr(sc, "_lookup_word", lambda w: ("", ""))

    # Day3 的词在 test 里叫 w3a0
    sc.backfill_step(force=True, max_words=1)
    st = sc.backfill_status()
    d3 = [d for d in st["days"] if d["day"] == 3][0]
    d1 = [d for d in st["days"] if d["day"] == 1][0]
    assert d3["missing"] < 2, "今天的 Day3 应该被优先补上"
    assert d1["missing"] == 2, "没轮到的时候，Day1 不该被动过"
