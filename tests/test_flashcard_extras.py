"""闪卡翻面后的「固定搭配 + 例句」回归。

背景：用户反馈「复习单词闪卡和线上版不一样，没有固定搭配和例句」。

前端 renderFlashcard 一直是这么写的（本地版与线上版逐字一致）：

    const _cols = c.collocations || [];
    const _exs  = c.examples     || [];

也就是说前端本来就会渲染，只要后端给数据。真正缺的是后端 ——
srs.flashcard_items() 原先只查了 dictionary 表拿音标/词性/释义，
压根没查 collocations / example_sentences，于是这两个字段从来没有值，
前端自然渲染不出任何东西。

更麻烦的是：那两张表只有内置词库会写（seed_builtin），
用户自己导入的词从来不进这两张表 —— 但导入时例句和搭配是存在
weeks.vocab_json 里的。只放在那里就是数据孤岛：学习页看得到、闪卡看不到。

所以本测试锁两条路径：
  1. 内置词 → 从 collocations / example_sentences 取
  2. 导入词 → 回退到 weeks.vocab_json 里导入时自带的例句与搭配
  3. 两边都没有 → 空数组（前端不渲染该区块，不能 500）
"""
import json
import os
import sys

sys.path.insert(0, "backend")

DB = "/tmp/eos_flashcard_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["EOS_DB"] = DB
os.environ.pop("EOS_TOKEN", None)

import db          # noqa: E402
import srs         # noqa: E402

DICT_WORD = "zbuiltin"        # 模拟「内置词库覆盖到」的词：两张表里有搭配和例句
IMPORT_WORD = "zcompany"      # 用户导入的词，只有 weeks.vocab_json 里有
BARE_WORD = "zbareword"       # 什么都没有


def _add_due_card(word):
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO reviews (kind, ref_key, prompt, answer, stage, week, day,"
            " ease, interval, reps, next_due, last_score, total_correct, total_wrong,"
            " last_reviewed, created_at) "
            "VALUES ('vocab',?,'','',0,1,1,2.5,0,0,date('now'),-1,0,0,NULL,?)",
            (word, db.ts()))
        conn.commit()
    finally:
        conn.close()


def _seed_builtin_extras():
    """模拟 seed_builtin 往两张表里写的数据。"""
    conn = db.get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO dictionary (word, meaning, pos, phonetic, theme) "
                     "VALUES (?,?,?,?,?)",
                     (DICT_WORD, "家庭", "n.", "/ˈfæməli/", ""))
        conn.execute(
            "INSERT INTO collocations (word, phrase, meaning, example, source) "
            "VALUES (?,?,?,?,?)",
            (DICT_WORD, "my family", "我的家庭", "I love my family.", "builtin"))
        conn.execute(
            "INSERT INTO example_sentences (word, sentence, translation, grammar_tags,"
            " difficulty, source, created_at) VALUES (?,?,?,?,?,?,?)",
            (DICT_WORD, "There are four people in my family.",
             "我家有四口人。", "", 0, "builtin", db.ts()))
        conn.commit()
    finally:
        conn.close()


def _seed_imported_word():
    """模拟用户导入：词 + 自带例句/搭配只落到 weeks.vocab_json。"""
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO weeks (stage, week_no, title, grammar, vocab_json, topics) "
            "VALUES (9,1,'工作与日常','',?, '')",
            (json.dumps([{
                "word": IMPORT_WORD,
                "meaning": "公司",
                "pos": "n.",
                "collocations": [{"phrase": "run a company", "meaning": "经营公司"}],
                "examples": [{"sentence": "I work for a big company.",
                              "translation": "我在一家大公司上班。"}],
                "day": 1,
            }], ensure_ascii=False),))
        conn.commit()
    finally:
        conn.close()


def setup_module(module):
    db.init_db()
    _seed_builtin_extras()
    _seed_imported_word()
    for w in (DICT_WORD, IMPORT_WORD, BARE_WORD):
        _add_due_card(w)


def _card(word):
    items = srs.flashcard_items()
    hit = [i for i in items if i["word"] == word]
    assert hit, f"{word} 应出现在到期卡里，实际：{[i['word'] for i in items]}"
    return hit[0]


def test_dict_word_gets_collocations_and_examples():
    """两张表有数据的词：直接从 collocations / example_sentences 取。"""
    c = _card(DICT_WORD)
    assert c["collocations"], "内置词应有固定搭配"
    assert c["collocations"][0]["phrase"] == "my family", c["collocations"]
    assert c["examples"], "内置词应有例句"
    assert c["examples"][0]["sentence"].startswith("There are four"), c["examples"]
    assert c["examples"][0]["translation"] == "我家有四口人。"


def test_imported_word_falls_back_to_week_payload():
    """导入词：两张表没有，回退到导入时存在 weeks.vocab_json 里的例句/搭配。"""
    c = _card(IMPORT_WORD)
    assert c["collocations"], f"导入词的搭配应能从 weeks.vocab_json 兜底取到：{c}"
    assert c["collocations"][0]["phrase"] == "run a company", c["collocations"]
    assert c["examples"], f"导入词的例句应能从 weeks.vocab_json 兜底取到：{c}"
    assert c["examples"][0]["sentence"] == "I work for a big company.", c["examples"]
    assert c["examples"][0]["translation"] == "我在一家大公司上班。"


def test_word_without_extras_returns_empty_lists():
    """两边都没有的词：返回空数组（前端不渲染区块），不能缺字段、不能报错。"""
    c = _card(BARE_WORD)
    assert c["collocations"] == [], c
    assert c["examples"] == [], c


def test_extras_are_capped():
    """卡片不能太长：搭配至多 3 条、例句至多 2 条。"""
    conn = db.get_conn()
    try:
        for i in range(6):
            conn.execute(
                "INSERT INTO collocations (word, phrase, meaning, example, source) "
                "VALUES (?,?,?,?,?)",
                (BARE_WORD, f"phrase {i}", f"释义 {i}", "", "builtin"))
            conn.execute(
                "INSERT INTO example_sentences (word, sentence, translation,"
                " grammar_tags, difficulty, source, created_at) VALUES (?,?,?,?,?,?,?)",
                (BARE_WORD, f"Sentence {i}.", f"句子 {i}", "", 0, "builtin", db.ts()))
        conn.commit()
    finally:
        conn.close()
    c = _card(BARE_WORD)
    assert len(c["collocations"]) == 3, len(c["collocations"])
    assert len(c["examples"]) == 2, len(c["examples"])


def test_batch_lookup_tolerates_bad_rows():
    """坏 JSON 的周不能让整页复习挂掉。"""
    conn = db.get_conn()
    try:
        conn.execute("INSERT INTO weeks (stage, week_no, title, grammar, vocab_json, topics) "
                     "VALUES (8,8,'坏数据','','{ 这不是 JSON','')")
        conn.commit()
    finally:
        conn.close()
    items = srs.flashcard_items()          # 不抛异常即可
    assert items, "坏数据不该导致整页复习返回空"
