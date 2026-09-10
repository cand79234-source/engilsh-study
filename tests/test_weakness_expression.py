# -*- coding: utf-8 -*-
"""薄弱项新增「表达丰富度」维度。

语法批改只判对错，判不出「你三个月来一直在写 I like ... 三词句」。
可这件事一样该进薄弱项 —— 每句都 PASS 却永远长不出从句，
学习者会一直待在舒适区里，以为自己在进步。

两个信号（都要求该词造够 3 句才统计，样本太少不冤枉人）：
  - 短句占比 >= 60%（少于 10 个词）→ 表达过于单一
  - I 开头占比 >= 70%             → 句式单一
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import db as D  # noqa: E402


@pytest.fixture
def env():
    """每个用例一份独立临时库，互不污染。"""
    path = tempfile.mktemp(suffix=".db")
    old = os.environ.get("EOS_DB")
    os.environ["EOS_DB"] = path
    D.reset() if hasattr(D, "reset") else None
    D.init_db()
    # init_db 会灌内置示范数据（里面有 like / company 等词的造句历史），
    # 不清掉的话统计里会混进不是本用例造的句子，比例怎么算都对不上。
    c = D.get_conn()
    c.execute("DELETE FROM sentences")
    c.commit()
    c.close()
    import link
    yield path, link
    os.environ["EOS_DB"] = old if old else ""
    try:
        os.unlink(path)
    except OSError:
        pass


def seed(conn, word, sentences, task="t"):
    now = D.ts()
    for i, s in enumerate(sentences):
        conn.execute(
            "INSERT INTO sentences (stage,week,day,word,task_key,attempt,original,"
            "corrected,error_type,explanation,ai_source,good,score,verdict,"
            "errors_json,opts_json,created_at) "
            "VALUES (0,1,1,?,?,?,?,'','','','rule',1,90,'','[]','[]',?)",
            (word, task, i + 1, s, now))
    conn.commit()


def expr_map(link_mod, conn):
    return {e["word"]: e for e in link_mod._weak_from_expression(conn)}


# ------------------------------------------------------------ 正向：该报的
def test_short_and_i_head(env):
    """又短又全 I 开头 → kind='both'。"""
    path, link = env
    conn = D.get_conn()
    seed(conn, "like", ["I like it.", "I like this.", "I like that.",
                        "I like them.", "I like him."])
    m = expr_map(link, conn)
    conn.close()
    assert "like" in m, m
    assert m["like"]["kind"] == "both", m["like"]
    assert m["like"]["short_rate"] == 100
    assert m["like"]["i_head_rate"] == 100


def test_short_only(env):
    """只短、主语还算多样 → kind='short'。"""
    path, link = env
    conn = D.get_conn()
    seed(conn, "shortonly", ["I like it.", "My team likes it.",
                             "She likes it.", "We like it."])
    m = expr_map(link, conn)
    conn.close()
    assert m["shortonly"]["kind"] == "short", m["shortonly"]
    assert m["shortonly"]["short_rate"] == 100
    assert m["shortonly"]["i_head_rate"] < 70


def test_i_head_only(env):
    """句子够长但清一色 I 开头 → kind='i_head'。"""
    path, link = env
    conn = D.get_conn()
    seed(conn, "iheadonly", [
        "I really like to play basketball with my friends every evening.",
        "I usually review the weekly plan before I start the new work.",
        "I always try to finish the report before the afternoon meeting.",
    ])
    m = expr_map(link, conn)
    conn.close()
    assert m["iheadonly"]["kind"] == "i_head", m["iheadonly"]
    assert m["iheadonly"]["i_head_rate"] == 100
    assert m["iheadonly"]["short_rate"] < 60


# ------------------------------------------------------------ 反向：不该报的
def test_healthy_not_reported(env):
    """长句 + 多样主语 → 不进薄弱项。"""
    path, link = env
    conn = D.get_conn()
    seed(conn, "healthy", [
        "I really like to play basketball with my friends every evening.",
        "My team likes to review the plan before we start the work.",
        "She likes reading books about history in the library.",
    ])
    m = expr_map(link, conn)
    conn.close()
    assert "healthy" not in m, m


def test_insufficient_samples_not_reported(env):
    """只造了 2 句 → 样本不足，不冤枉人。"""
    path, link = env
    conn = D.get_conn()
    seed(conn, "toofew", ["I like it.", "I like this."])
    m = expr_map(link, conn)
    conn.close()
    assert "toofew" not in m, m


def test_boundary_just_below_threshold(env):
    """刚好低于阈值 → 不报。

    4 句里 1 句短（25% < 60%）、1 句 I 开头（25% < 70%）。
    注意数词：下面两句都够 10 个词，不能想当然。
    """
    path, link = env
    conn = D.get_conn()
    seed(conn, "borderline", [
        "I like it.",                                                    # 3 词 · I 开头
        "My team really likes to review the plan every morning.",        # 10 词
        "She likes reading books about history in the city library.",    # 10 词
        "They usually practise the new words after the evening class.",  # 9 词... 见下
    ])
    m = expr_map(link, conn)
    conn.close()
    d = m.get("borderline")
    # 最后一句实际 9 词，所以短句是 2/4=50%，仍低于 60% 阈值
    assert d is None or (d["short_rate"] < 60 and d["i_head_rate"] < 70), m


# ------------------------------------------------------------ 集成：进 recommendations
def test_appears_in_recommendations(env):
    """要真的出现在 /api/weakness 的 recommendations 里，否则前端看不见。"""
    path, link = env
    conn = D.get_conn()
    seed(conn, "like", ["I like it.", "I like this.", "I like that."])
    conn.close()
    r = link.build_weakness()
    expr_recs = [x for x in r["recommendations"] if x["kind"] == "expression"]
    assert expr_recs, r["recommendations"]
    assert "like" in expr_recs[0]["label"], expr_recs[0]
    assert expr_recs[0]["advice"], expr_recs[0]


def test_sources_exposes_expression(env):
    """sources.expression 要带上，供总结页使用。"""
    path, link = env
    conn = D.get_conn()
    seed(conn, "like", ["I like it.", "I like this.", "I like that."])
    conn.close()
    r = link.build_weakness()
    assert "expression" in r["sources"], r["sources"].keys()
