# -*- coding: utf-8 -*-
"""造句批改规则 —— 对齐《造句批改逻辑与代码.md》文档的预期。

锁四件事：
  1. 句首小写 l 不再判错（产品决定，不纠缠大小写）
  2. RULES 严格等于文档的 16 条；不夹带 can to do / to+doing / I likes 三条
  3. 扩写线（<10 词 kind='expand'）已按需求移除，任何句子都不应再出现 expand
  4. 文档里的 16 条规则照常生效（尤其主谓一致 he go / she like）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import ai_service as A  # noqa: E402


def an(sentence, word="", grammar="", prompt=""):
    return A.analyze(sentence, word, grammar, prompt)


def types(r):
    return [e["type"] for e in r["errors"]]


def expands(r):
    return [o for o in r.get("optimizations", []) if o.get("kind") == "expand"]


# ---------------------------------------------------------------- ① I/l 不抓
def test_lowercase_l_not_flagged():
    """句首小写 l 应当是 PASS，不该被当成拼写错误。"""
    r = an("l like music very much", word="like")
    assert r["ok"] is True, r
    assert "拼写" not in types(r), types(r)


def test_lowercase_l_rule_still_disabled():
    """_r_i_l 函数保留但调用已被注释（不恢复 I/l）。"""
    assert callable(A._r_i_l)
    src = open(A.__file__, encoding="utf-8").read()
    assert "# raw_errors.extend(_r_i_l" in src, "应保留被注释掉的调用"


# --------------------------------------------------- ② RULES 严格等于文档 16 条
def test_rules_count_is_sixteen():
    """文档定义 16 条规则，不允许夹带额外规则。"""
    assert len(A.RULES) == 16, [f.__name__ for f in A.RULES]


def test_extra_three_rules_removed_from_rules():
    """can to do / to+doing / I likes 三条不应在 RULES 里。"""
    names = [f.__name__ for f in A.RULES]
    for extra in ("_r_modal_to", "_r_to_gerund", "_r_s3_with_i"):
        assert extra not in names, f"{extra} 不应出现在 RULES 中"


# ----------------------------------------------------- ③ 文档 16 条不含的句式
def test_can_to_do_is_now_correct_per_doc():
    """文档 16 条里没有 can to do 规则 → I can to swim 判正确（不报错）。"""
    r = an("I can to swim.", word="can")
    assert r["ok"] is True, r
    assert not any("to swim" in e["where"] or "can to" in e["where"]
                   for e in r["errors"]), r["errors"]


def test_must_to_do_is_now_correct_per_doc():
    r = an("I must to go now.", word="must")
    assert r["ok"] is True, r


def test_to_gerund_is_now_correct_per_doc():
    """文档 16 条里没有 to+doing 规则 → I like to playing 不报 to playing 错误。"""
    r = an("I like to playing basketball.", word="like")
    assert not any("to playing" in e["where"] for e in r["errors"]), r["errors"]


def test_s3_with_i_is_now_correct_per_doc():
    """文档 16 条里没有「I/we/they + 三单」规则 → 这些判正确。

    用 watch（非 y→ies 动词）避免目标词词干匹配干扰，专测语法规则。
    """
    for s, w in [("I likes apple.", "like"),
                 ("We watches English every day.", "watch"),
                 ("They goes home at night.", "go")]:
        r = an(s, word=w)
        assert r["ok"] is True, (s, r)
        assert "主谓一致" not in types(r), (s, types(r))


# ------------------------------------------------- ④ 扩写线：短正确句给扩写建议
def test_short_correct_sentence_gets_expand():
    """<10 词且无连接词的正确句 → 给 kind='expand' 的扩写建议（带 sample/note）。"""
    r = an("I like it.", word="like")
    assert r["ok"] is True, r
    ex = expands(r)
    assert len(ex) == 1, r.get("optimizations")
    assert ex[0]["sample"], ex[0]
    assert ex[0]["note"], ex[0]
    assert "I like it" in ex[0]["sample"], ex[0]["sample"]


def test_expand_hint_targets_ten_words():
    """提示语里要说明目标是 10 个词。"""
    r = an("I like it.", word="like")
    note = expands(r)[0]["note"]
    assert "10" in note, note


def test_long_sentence_no_expand():
    """>=10 词的句子不该再被催着扩写。"""
    s = "I really like to play basketball with my friends every evening."
    assert len(s.split()) >= 10
    r = an(s, word="like")
    assert expands(r) == [], r.get("optimizations")


def test_wrong_sentence_no_expand():
    """有语法错时不该同时给扩写建议（先改错，再谈丰富）。"""
    r = an("I very like you.", word="like")
    assert r["ok"] is False, r
    assert expands(r) == [], r.get("optimizations")


# ----------------------------------------------- ⑤ 文档 16 条照常生效
def test_subject_verb_agreement_still_works():
    """he go / she like —— 文档规则 _r_subj_verb 应抓主谓一致。"""
    for s, w in [("He go to school.", "go"),
                 ("She like apples.", "like"),
                 ("My friend like music.", "like")]:
        r = an(s, word=w)
        assert r["ok"] is False, (s, r)
        assert "主谓一致" in types(r), (s, types(r))


def test_third_person_singular_still_ok():
    """he likes / she goes 本来就是对的，不能反向误伤。"""
    for s, w in [("He likes apple.", "like"), ("She goes home.", "go")]:
        r = an(s, word=w)
        assert not any("主谓一致" in t for t in types(r)), (s, types(r))


# ----------------------------------------- ⑥ 连带修复：缺谓语误判（保留）
def test_no_verb_recognizes_inflected_forms():
    """_r_no_verb 要认三单/过去式/ing 为谓语，避免把 l likes 误判缺谓语。"""
    for tok in ("likes", "goes", "studies", "watched", "playing"):
        assert A._looks_like_verb(tok), tok


def test_looks_like_verb_rejects_non_verbs():
    for tok in ("company", "apple", "books", "morning"):
        assert not A._looks_like_verb(tok), tok


def test_no_verb_still_catches_real_cases():
    out = A._r_no_verb("Apple pie coffee.", "apple pie coffee.")
    assert len(out) == 1, out
    assert out[0]["type"] == "句型", out
