"""回归：造句批改 /api/sentence/check 不能因为「词没有星级记录」而 500。

背景：main.py 里原来写的是
    result["output_star"] = srs.word_stars(head_word)["stars"] if head_word else None
而 srs.word_stars 的设计是「未记录返回 None，不编造 0 星」。

两者撞在一起：用户写的句子若一个建议词都没用上，used_words 为空，
head_word 会回退到 candidates[0] —— 但 update_output_star 只对
used_words 里的词执行过，这个回退词从来没记录 → word_stars 返回 None
→ None["stars"] → TypeError → 整个批改接口 500，前端只剩一句
「Unexpected token 'I', "Internal S"... is not valid JSON」。

触发场景很常见：组合题给 2~3 个建议词，用户只用其中一个、或者干脆
另写一句，就会踩到。

修复：没记录就如实回 None（和 head_word 为空时的行为一致），不要崩。
"""
import os
import sys
import tempfile

os.environ.setdefault("EOS_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def _check(sentence, word, task_key="tk"):
    r = client.post("/api/sentence/check",
                    json={"sentence": sentence, "word": word, "task_key": task_key})
    return r.status_code, (r.json() if r.status_code == 200 else r.text[:200])


def test_sentence_not_using_target_word_returns_200():
    """句子不含目标词：原先必 500，现在应 200 且 output_star 为 None。"""
    st, d = _check("I am happy today.", "eossn_unused_word")
    assert st == 200, f"HTTP {st}: {d}"
    assert d["output_star"] is None


def test_empty_word_returns_200():
    """没有建议词（word 为空）时同样不能崩。"""
    st, d = _check("I am happy today.", "")
    assert st == 200, f"HTTP {st}: {d}"
    assert d["output_star"] is None


def test_target_word_used_still_updates_star():
    """正常路径不受影响：句子用到目标词，星级照常记录。"""
    st, d = _check("I like this book very much.", "like", task_key="tk_like")
    assert st == 200, f"HTTP {st}: {d}"
    assert "like" in d["used_words"]
    assert isinstance(d["output_star"], int)


def test_combo_task_updates_only_used_words():
    """组合题只给用到的词加星，没用到的词星级不受影响（原有设计保持不变）。"""
    st, d = _check("She goes to school every day.", "goes school", task_key="tk_combo")
    assert st == 200, f"HTTP {st}: {d}"
    assert "goes" in d["used_words"] or d["used_words"] == []
