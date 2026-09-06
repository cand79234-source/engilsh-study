# -*- coding: utf-8 -*-
"""导入时保留音标。

背景：importer._build 当年为了让「单词 + 音标」的行能匹配成功，
直接把音标替换成空格丢掉了（否则正则要求 left 是纯单词，一个词都导不进）。
代价：导入的词全都没音标，只能靠 ECDICT 全量词典事后补 ——
词典里没有的词就永远空着，表现就是用户看到的「有些有音标、有些没有」。

现在改成：先取出音标保存，再剥离。词典补全过程仍然保留作兜底。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import importer  # noqa: E402


def parse(line):
    return importer._parse_word_header(line)


# ------------------------------------------------------------ 基本提取
def test_phonetic_with_em_dash():
    """company /ˈkʌmpəni/ — n. 公司"""
    r = parse("company /ˈkʌmpəni/ — n. 公司")
    assert r is not None
    assert r["word"] == "company"
    assert r["phonetic"] == "/ˈkʌmpəni/", r


def test_phonetic_with_ordinal():
    """带行首序号也要能取到。"""
    r = parse("1. responsibility /rɪˌspɒnsəˈbɪləti/ — n. 责任")
    assert r is not None
    assert r["word"] == "responsibility"
    assert r["phonetic"] == "/rɪˌspɒnsəˈbɪləti/", r


def test_phonetic_with_bracket():
    """方括号包住的音标要剥掉括号只留 /.../。"""
    r = parse("branch [/brɑːntʃ/] n. 分公司")
    assert r is not None
    assert r["phonetic"] == "/brɑːntʃ/", r


def test_phonetic_no_separator():
    """无 — 分隔符、用空格隔开释义的写法。"""
    r = parse("continue /kənˈtɪnjuː/ v. 继续")
    assert r is not None
    assert r["word"] == "continue"
    assert r["phonetic"] == "/kənˈtɪnjuː/", r


def test_phonetic_with_focus_mark():
    """★ 重点词标记和音标同时存在。"""
    r = parse("★ important /ɪmˈpɔːtnt/ — adj. 重要的")
    assert r is not None
    assert r["focus"] is True
    assert r["phonetic"] == "/ɪmˈpɔːtnt/", r


# ------------------------------------------------------------ 不能回归
def test_no_phonetic_leaves_empty():
    """本来就没音标的行，phonetic 是空串而不是 None。"""
    r = parse("travel — v. 旅行")
    assert r is not None
    assert r["word"] == "travel"
    assert r["phonetic"] == "", r


def test_word_still_parsed_correctly():
    """核心回归：音标剥离后单词本身必须还能被解析出来。

    这正是当年出问题的地方 —— 只要这里挂了，整份词表就一个词都导不进。
    """
    for line, word in [
        ("company /ˈkʌmpəni/ — n. 公司", "company"),
        ("continue /kənˈtɪnjuː/ — v. 继续", "continue"),
        ("travel — v. 旅行", "travel"),
        ("1. apple /ˈæpl/ — n. 苹果", "apple"),
    ]:
        r = parse(line)
        assert r is not None, (line, "解析失败 —— 整行会被丢进 skipped")
        assert r["word"] == word, (line, r)


def test_phonetic_not_leaking_into_word():
    """音标不能被当成单词的一部分。"""
    r = parse("company /ˈkʌmpəni/ — n. 公司")
    assert "/" not in r["word"], r
    assert "kʌmpəni" not in r["word"], r


def test_phonetic_not_leaking_into_meaning():
    """音标也不能漏进释义。"""
    r = parse("branch /brɑːntʃ/ n. 分公司")
    assert "brɑːntʃ" not in (r["meaning"] or ""), r
