"""造句功能改造 —— 7 条硬性验收用例。

用例：
  ① I like you.            → 判正确，不进错题本
  ② I very like you.       → 判错误，说清「哪里错 / 为什么错 / 正确表达」，进错题本
  ③ He go to school every day. → 说清第三人称单数错误
  ④ 正确但可优化的句子       → 不判错、不低分
  ⑤ 第1次错 → 第2次对       → 两次记录都在，第1次的错题记录不被删（只标已改正）
  ⑥ 复制单题                 → 内容完整（实测前端 JS）
  ⑦ 复制全部                 → 当天内容完整（实测前端 JS，用真实后端响应喂给前端脚本）

全程纯本地规则批改，不接入任何 AI / LLM / 第三方模型。

运行: pytest tests/test_sentence_v2.py -v
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, "backend")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = "/tmp/eos_sentence_v2.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["EOS_DB"] = DB
os.environ.pop("EOS_TOKEN", None)

import main                      # noqa: E402
import services as svc           # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import db                        # noqa: E402

client = TestClient(main.app)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "frontend", "index.html")

WORD = "like"          # 用例统一用的词
TK = "basic:0"         # 题目标识（与前端约定一致）
PASS_LINE = 85


def post(sentence, word=WORD, task_key=TK):
    return client.post("/api/sentence/check",
                       json={"sentence": sentence, "word": word,
                             "task_key": task_key}).json()


def bank(word=WORD):
    return client.get("/api/error-bank", params={"word": word}).json()


def hist(task_key=TK):
    d = client.get("/api/sentence/attempts").json()
    for g in d["groups"]:
        if g["task_key"] == task_key:
            return g["attempts"]
    return []


def wipe():
    conn = db.get_conn()
    conn.execute("DELETE FROM sentences")
    conn.execute("DELETE FROM errors")
    conn.execute("DELETE FROM reviews")
    conn.commit()
    conn.close()


# =====================================================================
# ① 正确句 → 判正确、不进错题本
# =====================================================================
def test_case1_correct_sentence_not_in_bank():
    wipe()
    r = post("I like you.")
    assert r["ok"] is True, r
    assert r["score"] >= PASS_LINE, r
    assert r["verdict"] == "正确", r
    assert r["level"] == "基本掌握", r
    assert r["errors"] == [], r
    assert r["to_error_bank"] is False, r
    assert r["error_bank_ids"] == [], r

    b = bank()
    assert b["total"] == 0, f"正确句不该进错题本，实际：{b['items']}"


# =====================================================================
# ② 错误句 → 说清哪里错/为什么错/正确表达，并进错题本
# =====================================================================
def test_case2_error_sentence_explained_and_recorded():
    wipe()
    r = post("I very like you.")
    assert r["ok"] is False, r
    assert r["score"] < PASS_LINE, r
    assert r["verdict"] == "有错误", r
    assert r["level"] == "需要改进", r
    assert r["corrected"] == "I really like you.", r

    # 哪里错了
    wheres = [e["where"] for e in r["errors"]]
    assert any("very like" in w for w in wheres), wheres
    # 为什么错（必须讲原因，不能只给答案）
    why = " ".join(e["explanation"] for e in r["errors"])
    assert len(why) >= 12, f"解释太短，等于没解释：{why}"
    assert "like" in why and "really" in why, why
    # 正确表达
    assert any(e["correct"] == "really like" for e in r["errors"]), r["errors"]

    # 进错题本，且字段齐全
    b = bank()
    assert b["total"] == 1, b
    it = b["items"][0]
    for k in ("word", "sentence_text", "error_text", "error_type",
              "explanation", "corrected", "first_at", "times"):
        assert it.get(k), f"错题本缺字段 {k}：{it}"
    assert it["word"] == WORD
    assert it["sentence_text"] == "I very like you."
    assert it["error_text"] == "very like"
    assert it["corrected"] == "really like"
    assert it["times"] == 1
    assert it["fixed"] == 0


# =====================================================================
# ③ He go to school every day. → 说清第三人称单数
# =====================================================================
def test_case3_third_person_error_explained():
    wipe()
    r = post("He go to school every day.", word="go", task_key="basic:1")
    assert r["ok"] is False, r
    assert r["corrected"] == "He goes to school every day.", r
    assert r["error_type"] == "主谓一致", r
    why = " ".join(e["explanation"] for e in r["errors"])
    assert "第三人称" in why or "三单" in why or "he" in why.lower(), why
    assert "goes" in " ".join(e["correct"] for e in r["errors"]), r["errors"]
    assert r["score"] < PASS_LINE


# =====================================================================
# ④ 正确但可优化 → 不判错、不低分；优化与错误严格分开
# =====================================================================
def test_case4_optimizable_not_wrong():
    wipe()
    r = post("I like this movie.", word="movie", task_key="basic:2")
    assert r["ok"] is True, r
    assert r["score"] >= PASS_LINE, r
    assert r["level"] == "基本掌握", r
    assert r["errors"] == [], r
    assert r["to_error_bank"] is False
    # 有优化建议，但只是建议
    assert len(r["optimizations"]) >= 1, r
    merged = json.dumps(r["optimizations"], ensure_ascii=False)
    assert "原句" in merged and "不代表原句有错" in merged, r["optimizations"]
    # 关键：给了优化建议也绝不能进错题本
    assert bank(word="movie")["total"] == 0


# =====================================================================
# ⑤ 第1次错 → 第2次对：两次作答都保留；首次错误记录不删除
# =====================================================================
def test_case5_history_kept_and_first_error_preserved():
    wipe()
    r1 = post("I very like you.")
    assert r1["attempt"] == 1 and r1["ok"] is False and r1["score"] < PASS_LINE

    r2 = post("I really like you.")
    assert r2["attempt"] == 2, r2
    assert r2["ok"] is True and r2["score"] >= PASS_LINE, r2

    hs = hist()
    assert len(hs) == 2, f"两次作答必须都保留：{hs}"
    assert hs[0]["attempt"] == 1 and hs[0]["sentence"] == "I very like you."
    assert hs[0]["ok"] is False
    assert hs[1]["attempt"] == 2 and hs[1]["sentence"] == "I really like you."
    assert hs[1]["ok"] is True

    # 第一次的错题记录仍在，只是标成已改正，绝不删除
    b = bank()
    assert b["total"] == 1, f"首次错误记录被删了：{b}"
    it = b["items"][0]
    assert it["sentence_text"] == "I very like you."
    assert it["error_text"] == "very like"
    assert it["first_at"] == it["first_at"] and it["first_at"]
    assert it["fixed"] == 1 and it["fixed_at"], it
    assert it["times"] == 1
    assert r2["fixed_error_ids"] == [it["id"]]

    # 第三次作答也照样保留
    r3 = post("I really like you very much.")
    assert r3["attempt"] == 3, r3
    assert len(hist()) == 3


# =====================================================================
# ⑥⑦ 复制：把真实后端响应喂给真实前端脚本，在 Node 里跑
# =====================================================================
def _run_frontend_copy_test():
    wipe()
    # 用真实后端产出两次作答的结果，作为前端脚本的输入
    c1 = post("I very like you.")
    c2 = post("I really like you.")
    checks = {
        "I very like you.": c1,
        "I really like you.": c2,
    }
    today = client.get("/api/today").json()
    plan = today["sentence_plan"]
    # 保证第①段第一题就是我们要的词
    basic0 = dict(plan["basic"][0])
    basic0["word"] = WORD
    basic0["task"] = f"用「{WORD}」说一句关于你的话"
    plan["basic"][0] = basic0
    today["sentence_plan"] = plan

    fx = {
        "today": today,
        "checks": checks,
        "attempts": {"groups": []},
        "steps": [
            {"task_key": TK, "sentence": "I very like you."},
            {"task_key": TK, "sentence": "I really like you."},
        ],
        "copy_one": [
            {"name": "first", "task_key": TK, "which": "1"},
            {"name": "last", "task_key": TK, "which": "last"},
        ],
    }
    fx_path = "/tmp/eos_copy_fx.json"
    out_path = "/tmp/eos_copy_out.json"
    with open(fx_path, "w", encoding="utf-8") as f:
        json.dump(fx, f, ensure_ascii=False)
    if os.path.exists(out_path):
        os.remove(out_path)

    script = os.path.join(ROOT, "tests", "frontend", "copy_test.js")
    proc = subprocess.run(["node", script, FRONTEND, fx_path, out_path],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            f"前端脚本执行失败 rc={proc.returncode}\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
    with open(out_path, encoding="utf-8") as f:
        out = json.load(f)
    if out.get("fatal"):
        raise AssertionError("前端脚本内部异常：" + out["fatal"])
    return out


def test_case6_and_7_copy_content_complete():
    out = _run_frontend_copy_test()

    got = {c["name"]: c["text"] for c in out["captured"]}

    # ---- ⑥ 复制单题：内容完整 ----
    first = got.get("first", "")
    assert first, "复制单题(第1次) 没拿到文本"
    for needle in ["【造句批改】", "单词：like", "第 1 次作答",
                   "我的句子：I very like you.", "得分：",
                   "判定：❌ 有错误", "哪里错了：very like",
                   "为什么错：", "正确表达：really like",
                   "整句正确写法：I really like you."]:
        assert needle in first, f"复制单题缺「{needle}」，实际：\n{first}"

    last = got.get("last", "")
    assert "第 2 次作答" in last, last
    assert "我的句子：I really like you." in last, last
    assert "判定：✅ 正确" in last, last
    assert "没有明显错误" in last, last

    # ---- ⑦ 复制全部：当天内容完整 ----
    alltxt = got.get("copyAll", "")
    assert alltxt, "复制全部 没拿到文本"
    assert "【今日造句批改记录】" in alltxt
    assert "日期：" in alltxt
    assert "共 1 题" in alltxt, alltxt
    # 两次作答都必须在里面，且顺序完整
    assert alltxt.index("第 1 次作答") < alltxt.index("第 2 次作答")
    for needle in ["单词：like", "I very like you.", "I really like you.",
                   "哪里错了：very like", "正确表达：really like",
                   "整句正确写法：I really like you.", "得分："]:
        assert needle in alltxt, f"复制全部缺「{needle}」，实际：\n{alltxt}"


# =====================================================================
# 附加硬性约束
# =====================================================================
def test_no_ai_integration():
    """站点本身绝不接入 AI / LLM / 第三方模型。"""
    import re
    src = open(FRONTEND, encoding="utf-8").read()
    backend_src = ""
    for fn in ("ai_service.py", "main.py", "services.py", "db.py", "srs.py"):
        backend_src += open(os.path.join(ROOT, "backend", fn), encoding="utf-8").read()
    # 只查真正的「接入」痕迹：import、SDK 名、模型端点、密钥
    patterns = [
        r"\bimport\s+(openai|anthropic|google\.generativeai|ollama)\b",
        r"\bfrom\s+(openai|anthropic|langchain|llama_index|transformers)\b",
        r"api\.openai\.com", r"api\.anthropic\.com", r"generativelanguage\.googleapis",
        r"\bChatCompletion\b", r"\bmessages\s*=\s*\[\s*\{\s*[\"']role[\"']",
        r"\bsk-[A-Za-z0-9]{16,}", r"\bOPENAI_API_KEY\b", r"\bANTHROPIC_API_KEY\b",
        r"\bGPT-?[0-9]\b", r"\bclaude-[0-9]", r"\bgpt-[0-9]",
        r"\buse_ai\b", r"\bai_provider\b", r"\bcall_llm\b", r"\bllm_\w+\s*\(",
    ]
    for src_name, text in (("frontend", src), ("backend", backend_src)):
        for pat in patterns:
            hit = re.search(pat, text, re.I)
            assert not hit, f"{src_name} 发现第三方模型痕迹 /{pat}/：{hit.group(0)!r}"
    # 前端不能有「找 AI」按钮/入口
    assert "找AI" not in src and "找 AI" not in src
    # 批改来源恒为本地规则
    r = post("I very like you.")
    assert r["ai_source"] == "rule"


def test_esc_escapes_single_quote():
    """内联 onclick 的经典 XSS：撇号必须转义。"""
    src = open(FRONTEND, encoding="utf-8").read()
    assert "replace(/'/g,'&#39;')" in src or "replace(/'/g, '&#39;')" in src, \
        "esc() 仍未转义单引号，I'm / don't 会破坏 JS"


def test_health_and_auth_restored():
    """render.yaml 的 healthCheckPath=/api/health 必须有对应接口。"""
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["auth_required"] is False  # 未设 EOS_TOKEN 时开放


def test_85_rule_is_strict():
    """有错必定 <85，无错必定 >=85，优化建议不影响分数。"""
    wipe()
    wrong = post("I very like you.")
    assert wrong["score"] < PASS_LINE
    wipe()
    right = post("I like you.")
    assert right["score"] >= PASS_LINE
    assert right["optimizations"], "正确句应给出优化建议"
    assert right["ok"] is True
