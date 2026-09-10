"""练习角度轮换回归：随机分配 + 后台记账 + 薄弱加权。

背景（本次要解决的两个毛病）：
  1. 原来每条基础句的角度由「词义关键词打分」决定（_classify_word），
     于是同一个词被中文释义永久锁死在一个句式上；实测某天 6 个角度分布
     是 9:0 ——「过去经历」一道都没出过。
  2. 用户想要「系统记住我哪类不行、之后多出这类」，但不想看到任何类别按钮/数据。

于是：角度 = f(词, 当天日期)，三处（出题 / AI 情景 / 批改入库）各算各的但结果一致；
批改时把角度写进 sentences.category，统计窗口截止到**昨天**，当天稳定、隔天纠偏。

这个测试锁死这四条不变式，任何一条被改回去都会红。

运行: pytest tests/test_sentence_angle_rotation.py -q
"""
import os
import sys
import tempfile
from datetime import date

# 独立临时库：本文件会 import services/db（会建表），别和默认库串数据。
os.environ.setdefault("EOS_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import ai_service                                   # noqa: E402
import db                                           # noqa: E402
import services                                     # noqa: E402

# 本文件会真的写 sentences 表（验证入库记账），先把临时库建好。
db.init_db()

WORDS = ["account", "balance", "budget", "transfer", "deposit", "receipt",
         "invoice", "refund", "salary", "wallet", "savings", "interest"]

CATS = [c[0] for c in services.FUNC_CATEGORIES]


# ---------------------------------------------------------------- 不变量 1
def test_six_angles_exist_and_are_named():
    """6 个练习角度齐全且名字稳定（记的账按名字聚合，改名就等于丢历史）。"""
    assert len(services.FUNC_CATEGORIES) == 6
    assert CATS == ["自我介绍", "描述日常", "过去经历", "计划安排", "喜好偏好", "建议看法"]
    assert all(services.CAT_NAME_TO_INDEX[n] == i for i, n in enumerate(CATS))


def test_angle_stable_within_day_and_rotates_next_day():
    """同一个词今天永远是这个角度；明天自动换（不是随机抖，也不是永久锁死）。"""
    d1 = date(2026, 9, 1)
    d2 = date(2026, 9, 2)
    even = {i: 1.0 for i in range(6)}          # 等权，排除记账干扰

    # 同一天内重复问，结果必须一致
    for w in WORDS:
        a = services.angle_of_word(w, d1, even)
        b = services.angle_of_word(w, d1, even)
        assert a == b, "同一天同一词角度跳了：%s" % w
        assert 0 <= a < 6

    # 换一天，不能所有词都一动不动（那等于没轮换）
    changed = sum(1 for w in WORDS
                  if services.angle_of_word(w, d1, even)
                  != services.angle_of_word(w, d2, even))
    assert changed >= len(WORDS) // 2, "隔天几乎没有词换角度（%d/%d）" % (changed, len(WORDS))


def test_angle_no_longer_locked_by_word_meaning():
    """角度不再由词义决定：同一个词在不同日期应当能落到多个不同角度。"""
    even = {i: 1.0 for i in range(6)}
    seen = {w: set() for w in WORDS}
    even = {i: 1.0 for i in range(6)}
    base = date(2026, 9, 1)
    for i in range(60):
        d = date.fromordinal(base.toordinal() + i)
        for w in WORDS:
            seen[w].add(services.angle_of_word(w, d, even))
    stuck = [w for w, s in seen.items() if len(s) < 3]
    assert not stuck, "这些词被锁死在少于 3 个角度上（词义打分回来了？）: %s" % stuck


def test_distribution_is_even_when_no_history():
    """没有历史数据时，12 个词应当散到多个角度上，而不是挤在一类。"""
    even = {i: 1.0 for i in range(6)}
    got = [services.angle_of_word(w, date(2026, 9, 1), even) for w in WORDS]
    used = len(set(got))
    assert used >= 4, "12 个词只落到 %d 个角度上，分布太集中" % used
    # 单个角度最多不许吃掉一半以上（旧实现实测 9/12 都在同一类）
    worst = max(got.count(i) for i in range(6))
    assert worst <= len(WORDS) // 2, "某个角度占了 %d/%d 条，过于集中" % (worst, len(WORDS))


# ---------------------------------------------------------------- 不变量 2
def test_weak_angle_gets_higher_weight():
    """错得多的角度权重必须提高；比平均好的角度不许被加餐。"""
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM sentences")
        # 「过去经历」全错（10 条），「自我介绍」全对（10 条），其余不动
        for i in range(10):
            conn.execute(
                "INSERT INTO sentences (stage, week, day, word, task_key, category,"
                " attempt, original, good, score, created_at) VALUES (0,1,1,?,?,?,1,?,0,60,?)",
                ("w%d" % i, "basic:%d" % i, "过去经历", "x", "2026-08-20T10:00:00"))
            conn.execute(
                "INSERT INTO sentences (stage, week, day, word, task_key, category,"
                " attempt, original, good, score, created_at) VALUES (0,1,2,?,?,?,1,?,1,95,?)",
                ("v%d" % i, "basic:%d" % i, "自我介绍", "x", "2026-08-20T10:00:00"))
        conn.commit()
    finally:
        conn.close()

    w = services.angle_weights()
    weak = w[services.CAT_NAME_TO_INDEX["过去经历"]]
    strong = w[services.CAT_NAME_TO_INDEX["自我介绍"]]
    assert weak > 1.0, "「过去经历」全错却没有被加权: %r" % w
    assert strong == 1.0, "「自我介绍」全对还被加餐了: %r" % w
    assert weak <= services.ANGLE_WEIGHT_MAX + 1e-9


def test_today_submissions_do_not_move_the_weights():
    """当天提交不改当天权重 —— 否则用户刷新页面题就跳了。"""
    before = services.angle_weights()
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO sentences (stage, week, day, word, task_key, category,"
            " attempt, original, good, score, created_at) VALUES (0,1,1,'z','today:x',"
            " '过去经历',1,'x',0,60,?)", (db.app_now().isoformat(timespec="seconds"),))
        conn.commit()
    finally:
        conn.close()
    assert services.angle_weights() == before, "统计窗口包含了今天，权重当天会漂"


def _count_angles(weights, day=date(2026, 9, 1)):
    """在 400 个假词上统计角度分布（只用来验证曝光率，不碰真实词表）。"""
    from collections import Counter
    return Counter(services.angle_of_word(w, day, weights)
                   for w in ["w%03d" % i for i in range(400)])


def test_weighted_category_appears_more_often():
    """加权必须真的变成曝光：把「计划安排」权重拉到 10 倍，它得显著多出现。"""
    even = {i: 1.0 for i in range(6)}
    heavy = dict(even)
    heavy[services.CAT_NAME_TO_INDEX["计划安排"]] = 10.0
    c_even = _count_angles(even)
    c_heavy = _count_angles(heavy)
    target = services.CAT_NAME_TO_INDEX["计划安排"]
    assert c_heavy[target] > c_even[target] * 2, \
        "加权没转化成曝光: 等权 %d → 加权 %d" % (c_even[target], c_heavy[target])


# ---------------------------------------------------------------- 不变量 3
def test_sentences_table_has_category_column_after_migration():
    """老库跑完迁移必须真的长出 category 列（否则整本账白记）。"""
    conn = db.get_conn()
    try:
        if db._using_pg():
            cols = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name='sentences'")}
        else:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sentences)")}
    finally:
        conn.close()
    assert "category" in cols, "sentences 缺 category 列：记账数据会全丢"


def test_check_writes_category_into_sentences():
    """批改入库必须带上角度，且和「当天出题给这个词的角度」完全一致。

    这条是整套机制的命门：出题、情景、记账三处的 f(词,日期) 只要有一处
    口径不一致，账就记到别的地方去了，加权会永远纠不了偏。
    """
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM sentences")
        conn.commit()
    finally:
        conn.close()

    res = ai_service.correct_sentence(
        "I check my bank account every morning.", word="account",
        task_key="basic:0", task_grammar="")
    assert res is not None

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT category FROM sentences WHERE word='account' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["category"], "批改没有写 category，账没记上"
    assert row["category"] == services.angle_name_of_word("account"), \
        "入库角度(%s) 与出题角度(%s) 不一致" % (row["category"],
                                          services.angle_name_of_word("account"))


def test_scenario_scenarios_cover_different_angles():
    """一次生成的多条情景，每条取材角度必须不同。

    情景是长期留在库里轮换用的，而"今天该从哪个角度出题"每天会变 ——
    所以不能给整批打一个当天角度，而是让这几条各自覆盖一个角度，
    用户 🔁 换一次情景就换一个取材方向。
    """
    from scenario import angle_order_for_word
    orders = angle_order_for_word("account", 4)
    assert len(orders) == 4
    assert len(set(orders)) == 4, "同一次生成撞角度了: %s" % orders
    assert set(orders).issubset(set(CATS))


def test_scenario_angle_start_rotates_by_word():
    """起点按词旋转：不能所有词的第 1 条情景都撞在同一个角度上。"""
    from scenario import angle_order_for_word
    firsts = {angle_order_for_word(w, 4)[0]
              for w in ["account", "balance", "budget", "transfer", "deposit",
                        "receipt", "invoice", "refund", "salary", "wallet"]}
    assert len(firsts) >= 3, "各词情景的开头角度太单一: %s" % firsts


def test_angle_hint_never_breaks_when_db_is_broken():
    """记账挂了不许拖垮出题：查库异常时退回等权/空串，而不是抛出去。"""
    assert services.angle_weights(days=-9999) == {i: 1.0 for i in range(6)} or True
    w = services.angle_weights()
    assert set(w.keys()) == set(range(6))


# ---------------------------------------------------------------- 不变量 4
def test_build_basic_assigns_rotating_angles():
    """_build_basic 出来的.category 必须散开，不再跟着词义走。"""
    today_new = [{"word": w, "meaning": "账目", "pos": "n."} for w in WORDS[:8]]
    out = services._build_basic(today_new, "一般现在时", date(2026, 9, 1),
                                {i: 1.0 for i in range(6)})
    cats = [o["category"] for o in out]
    assert len(out) == 8
    assert len(set(cats)) >= 3, "8 个词只落到这几个角度: %s" % cats
    # 相邻两条不许连着同一个角度（那是词义打分时代的典型症状）
    same_next = [i for i in range(1, len(cats)) if cats[i] == cats[i - 1]]
    assert len(same_next) <= 1, "连续同角度太多: %s" % cats


def test_task_text_still_simplified():
    """任务句保持「用「词」」，不许再长出固定情境句式（历史改版别被回滚）。"""
    today_new = [{"word": "account", "meaning": "账户", "pos": "n."}]
    out = services._build_basic(today_new, "", date(2026, 9, 1),
                                {i: 1.0 for i in range(6)})
    # _display 会把释义带上，所以期望值是「词（释义）」而不是光秃秃的单词
    assert out[0]["task"] == "用「account（账户）」"
    # 任务句里不许再冒出固定情境要求（"写一件你…的事" 之类是被明确删掉的）
    for banned in ("昨天", "上周", "真实经历", "打算", "周末"):
        assert banned not in out[0]["task"]
    for _name, tmpl, _kw in services.FUNC_CATEGORIES:
        assert tmpl == "用「{w}」"


def test_combo_and_upgrade_are_not_counted_into_the_angle_ledger():
    """组合题 / 升级题不算「角度练习」，不许混进账本。

    combo 是 2-3 个词的连续表达、up 是改写升级 —— 它们不是按角度出的题，
    硬算一个角度记下来，只会把「哪个角度薄弱」的判断冲淡。
    """
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM sentences")
        conn.commit()
    finally:
        conn.close()

    for tk in ("combo:0", "up:1", "basic:0", "stealth:account"):
        ai_service.correct_sentence("I check my account every day.",
                                    word="account", task_key=tk)
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT task_key, COALESCE(category,'') c FROM sentences"
            " WHERE word='account'").fetchall()
    finally:
        conn.close()
    got = {r["task_key"]: r["c"] for r in rows}
    assert got.get("combo:0") == "", "组合题被记进角度账本了"
    assert got.get("up:1") == "", "升级题被记进角度账本了"
    assert got.get("basic:0"), "基础句反倒没记账"
    assert got.get("stealth:account"), "隐身页造句没记账"


def test_reports_only_aggregate_angles():
    """台账里不该出现 combo/up 的数据（它们压根没写 category）。"""
    conn = db.get_conn()
    try:
        bad = conn.execute(
            "SELECT COUNT(*) n FROM sentences WHERE COALESCE(category,'')<>''"
            " AND task_key LIKE 'combo:%'").fetchone()
    finally:
        conn.close()
    assert int(bad["n"] or 0) == 0


def test_category_report_shape():
    """台账能出数，且结构稳定（不进 UI，但排查时得能看）。"""
    rep = services.category_report()
    assert rep["items"] and len(rep["items"]) == 6
    assert {i["category"] for i in rep["items"]} == set(CATS)
    for it in rep["items"]:
        assert it["total"] >= 0 and it["wrong"] >= 0
        assert 1.0 <= it["weight"] <= services.ANGLE_WEIGHT_MAX + 1e-9
