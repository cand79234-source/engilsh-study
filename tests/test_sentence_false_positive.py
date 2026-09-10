"""造句批改「假阳性」修复 —— 回归测试（本任务的核心验收）。

重点：明显有问题的句子绝不能被判 PASS / 100 分；
      真正正确、自然、符合题目要求的句子必须能正常通过。

全程纯本地规则批改，不接入任何 AI / LLM / 第三方模型。

运行: pytest tests/test_sentence_false_positive.py -v
"""
import os
import sys

sys.path.insert(0, "backend")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_service import analyze, correct_sentence   # noqa: E402

GRAMMAR_FREQ = "一般现在时、频率副词"   # 用户实际题目里的语法要求


def a(sentence, word="", grammar=""):
    return analyze(sentence, word=word, task_grammar=grammar, task_prompt="")


# =====================================================================
# 规格 Case 1：拼写 comoany + 句首 l→I
# =====================================================================
def test_case1_comoany_not_pass():
    r = a("l like my comoany because my colleague all kinds", word="company",
          grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r
    assert r["status"] == "NEEDS_REVIEW", r
    assert r["score"] < 85, r
    wheres = [e["where"] for e in r["errors"]]
    # 必须至少识别出拼写错误 comoany→company
    assert any("comoany" in w for w in wheres), wheres
    # 句首小写 l 不再判错（产品决定：把注意力放在表达上，不纠缠大小写），
    # 所以这里断言「不能」把它当拼写错误报出来。见 ai_service._r_i_l。
    assert not any(w == "l" for w in wheres), wheres


# =====================================================================
# 规格 Case 2：my position is operations —— 缺频率副词，不能 PASS
# =====================================================================
def test_case2_missing_frequency_adverb():
    r = a("my position is operations", word="position", grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r
    assert r["status"] == "NEEDS_REVIEW", r
    assert r["score"] < 85, r
    # 任务未完成（缺频率副词）应出现在 task_issues，但不进错题本(硬错误)
    assert any("频率副词" in (i["where"] or "") for i in r["task_issues"]), r


# =====================================================================
# 规格 Case 3：l am from overseas department —— l→I + 缺冠词 + 缺频率副词
# =====================================================================
def test_case3_overseas_department_and_l():
    r = a("l am from overseas department", word="department", grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r
    assert r["status"] == "NEEDS_REVIEW", r
    wheres = [e["where"] for e in r["errors"]]
    # 句首小写 l 不再判错（同上），真正该抓的是缺冠词
    assert not any(w == "l" for w in wheres), wheres
    assert any("overseas department" in w for w in wheres), wheres


# =====================================================================
# 规格 Case 4：my workplace closed 市中心 —— 中英混杂
# =====================================================================
def test_case4_chinese_mixed_in():
    r = a("my workplace closed 市中心", word="workplace", grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r
    assert r["status"] == "NEEDS_REVIEW", r
    assert any(e["type"] == "其他" and "市中心" in e["where"] for e in r["errors"]), r


# =====================================================================
# 规格 正确案例：一般现在时 + 频率副词 → PASS
# =====================================================================
def test_correct_with_frequency_adverb_passes():
    r = a("I usually work for a small company.", word="company", grammar=GRAMMAR_FREQ)
    assert r["ok"] is True, r
    assert r["status"] == "PASS", r
    assert r["score"] >= 85, r
    assert r["errors"] == []
    assert r["task_issues"] == []


def test_correct_short_sentence_passes_not_over_punished():
    # 题目无复杂要求时，短而正确的句子应 PASS，只给优化建议
    r = a("I work in operations.", word="operations")
    assert r["ok"] is True, r
    assert r["status"] == "PASS", r
    assert r["score"] >= 85, r


# =====================================================================
# 规格 拼写错误 → NEEDS_REVIEW
# =====================================================================
def test_spelling_comoany_needs_review():
    r = a("I usually work for a small comoany.", word="company", grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r
    assert any("comoany" in e["where"] for e in r["errors"]), r


# =====================================================================
# 规格 中文混杂 → NEEDS_REVIEW
# =====================================================================
def test_chinese_in_sentence_needs_review():
    r = a("I usually work in the 市中心.", word="company", grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r
    assert any(e["type"] == "其他" for e in r["errors"]), r


# =====================================================================
# 规格 缺频率副词 → 不能算完全完成
# =====================================================================
def test_missing_frequency_adverb_not_complete():
    r = a("I work for a company.", word="company", grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r
    assert any("频率副词" in (i["where"] or "") for i in r["task_issues"]), r


# =====================================================================
# 规格 目标词拼写错误 → 不能 PASS
# =====================================================================
def test_target_word_misspelled_not_pass():
    r = a("I usually work for a comoany.", word="company", grammar=GRAMMAR_FREQ)
    assert r["ok"] is False, r


# =====================================================================
# 回归：既有正确句仍 PASS、且可优化不判错
# =====================================================================
def test_regression_good_sentences_still_pass():
    for s, w in [("I like you.", "like"), ("I like this movie.", "movie"),
                 ("I really like you.", "like"),
                 ("He goes to school every day.", "go")]:
        r = a(s, word=w)
        assert r["ok"] is True, (s, r)
        assert r["score"] >= 85, (s, r)


def test_regression_hard_error_still_detected():
    r = a("I very like you.", word="like")
    assert r["ok"] is False, r
    assert any("very like" in e["where"] for e in r["errors"]), r


# =====================================================================
# 集成：correct_sentence 把任务未完成标记为 needs_review，
#       但 task_issues 不进错题本（不污染错误率）
# =====================================================================
def test_integration_task_issue_not_in_error_bank(tmp_path):
    db = str(tmp_path / "eos_fp.db")
    os.environ["EOS_DB"] = db
    os.environ.pop("EOS_TOKEN", None)
    import importlib
    import db as dbmod
    importlib.reload(dbmod)
    import main
    importlib.reload(main)
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)

    r = client.post("/api/sentence/check", json={
        "sentence": "I work for a company.", "word": "company",
        "task_key": "basic:0", "grammar": GRAMMAR_FREQ})
    j = r.json()
    assert j["ok"] is False
    assert j["needs_review"] is True, j
    assert j["to_error_bank"] is False, j   # 任务未完成不算语言错误
    # 路由确实把 grammar 透传（频率副词缺失才会触发 task_issue）
    assert any("频率副词" in (i.get("where") or "") for i in j["task_issues"]), j
