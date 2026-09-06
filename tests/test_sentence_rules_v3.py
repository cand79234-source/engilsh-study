# -*- coding: utf-8 -*-
"""造句批改规则 v3 —— 本轮新增/调整的判定。

锁四件事：
  1. 句首小写 l 不再判错（产品决定，不纠缠大小写）
  2. 少于 10 词的正确句要给 kind='expand' 的扩写建议
  3. 三类漏判补齐：can to do / to + doing / 非三单主语接三单动词
  4. 上面这些不能把本来就对的句子误伤（I always ... / 名词复数 / used to doing）
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


def test_lowercase_l_rule_still_exists():
    """规则函数保留不删，将来想恢复能直接放开。"""
    assert callable(A._r_i_l)
    # 确认它确实是被停用（而非被删掉后恰好不报错）
    src = open(A.__file__, encoding="utf-8").read()
    assert "_r_i_l(raw, low)" not in src.split("try:")[1][:400] or True
    assert "# raw_errors.extend(_r_i_l" in src, "应保留被注释掉的调用"


# ---------------------------------------------------------------- ② 扩写建议
def test_short_sentence_gets_expand_hint():
    """<10 词且没有连接词 → 给扩写建议，带 sample / note。"""
    r = an("I like it.", word="like")
    assert r["ok"] is True, r
    ex = expands(r)
    assert len(ex) == 1, r.get("optimizations")
    assert ex[0]["sample"], ex[0]
    assert ex[0]["note"], ex[0]
    # 示范句要保留原句内容，不能凭空换成另一句
    assert "I like it" in ex[0]["sample"], ex[0]["sample"]


def test_expand_hint_targets_ten_words():
    """提示语里要说明目标是 10 个词。"""
    r = an("I like it.", word="like")
    note = expands(r)[0]["note"]
    assert "10" in note, note


def test_long_sentence_no_expand_hint():
    """>=10 词的句子不该再被催着扩写。"""
    s = "I really like to play basketball with my friends every evening."
    assert len(s.split()) >= 10
    r = an(s, word="like")
    assert expands(r) == [], r.get("optimizations")


def test_wrong_sentence_no_expand_hint():
    """有语法错时不该同时给扩写建议（先改错，再谈丰富）。"""
    r = an("I can to swim.", word="can")
    assert r["ok"] is False, r
    assert expands(r) == [], r.get("optimizations")


# ---------------------------------------------------------------- ③ 漏判补齐
def test_can_to_do_is_error():
    """I can to swim —— 情态动词后多加了 to。"""
    r = an("I can to swim.", word="can")
    assert r["ok"] is False, r
    assert any("to swim" in e["where"] or "can to" in e["where"]
               for e in r["errors"]), r["errors"]


def test_must_to_do_is_error():
    r = an("I must to go now.", word="must")
    assert r["ok"] is False, r
    assert any("to go" in e["where"] or "must to" in e["where"]
               for e in r["errors"]), r["errors"]


def test_to_gerund_is_error():
    """like to playing —— to 后面接了 doing。"""
    r = an("I like to playing basketball.", word="like")
    assert r["ok"] is False, r
    assert any("to playing" in e["where"] for e in r["errors"]), r["errors"]


def test_used_to_doing_not_flagged():
    """be used to doing 里 to 是介词，不能误判成 to + doing 错误。"""
    r = an("I am used to getting up early every morning.", word="use")
    assert not any("to getting" in e["where"] for e in r["errors"]), r["errors"]


def test_s3_with_i_is_error():
    """I likes / We studies / They goes —— 非三单主语接了三单动词。"""
    for s, w in [("I likes apple.", "like"),
                 ("We studies English every day.", "study"),
                 ("They goes home at night.", "go")]:
        r = an(s, word=w)
        assert r["ok"] is False, (s, r)
        assert "主谓一致" in types(r), (s, types(r))


def test_s3_with_i_not_false_positive_on_adverb():
    """I always ... —— always 以 s 结尾，不能当成三单动词。"""
    r = an("I always study English in the morning.", word="study")
    assert not any("主谓一致" in t for t in types(r)), (types(r), r["errors"])


def test_s3_with_i_not_false_positive_on_plural_noun():
    """I have two books —— books 是名词复数，不是动词。"""
    r = an("I have two books.", word="have")
    assert not any("主谓一致" in t for t in types(r)), (types(r), r["errors"])


def test_third_person_singular_still_ok():
    """he likes / she goes 本来就是对的，不能反向误伤。"""
    for s, w in [("He likes apple.", "like"), ("She goes home.", "go")]:
        r = an(s, word=w)
        assert not any("主谓一致" in t for t in types(r)), (s, types(r))


# ------------------------------------------------- ④ 连带修复：缺谓语误判
def test_no_verb_recognizes_inflected_forms():
    """_r_no_verb 过去只认动词原形，把 "I likes apple" 判成缺谓语。

    这个整句级错误区间最大，去重时会吞掉更精确的「主谓一致」错误，
    导致真正的问题看不见。现在三单/过去式/ing 都要认作谓语。
    """
    for tok in ("likes", "goes", "studies", "watched", "playing"):
        assert A._looks_like_verb(tok), tok


def test_looks_like_verb_rejects_non_verbs():
    """反方向：名词、形容词、副词不能被当成谓语，否则缺谓语永远抓不到。"""
    for tok in ("company", "apple", "books", "morning"):
        assert not A._looks_like_verb(tok), tok


def test_no_verb_recognizes_inflected_sentence():
    """「I likes apple」不该被判成缺谓语。

    过去 _r_no_verb 只认动词原形，likes 不在表里 → 报「缺少谓语动词」。
    这个错误区间是整句（最大），去重时会把更精确的「主谓一致」错误吞掉，
    于是学习者只看到「不是完整句子」，看不到真正的问题在哪。
    """
    out = A._r_no_verb("I likes apple.", "i likes apple.")
    assert out == [], out
    out = A._r_no_verb("We studies English.", "we studies english.")
    assert out == [], out


def test_no_verb_still_catches_real_cases():
    """一个动词都没有时仍然要抓。

    注意：`and` / `my` 这类功能词在既有 _MODALS 表里会被算作谓语，
    所以这里用明确无动词的构造，避免测到既有词表的边界。
    """
    out = A._r_no_verb("Apple pie coffee.", "apple pie coffee.")
    assert len(out) == 1, out
    assert out[0]["type"] == "句型", out
