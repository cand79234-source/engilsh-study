"""隐身学习三处 AI 接管的端到端测试（接口层）。

用假 ark_json 顶替真实豆包：不花 token、不联网，只验接口契约对不对。
覆盖：导入触发情景 → /api/stealth/next 带上情景 → 🔁 /api/scenario/next 轮换
      → /api/stealth/submit 返回 AI 分 → /api/weakness/explain 只讲一次
"""
import os
import sys

os.environ["EOS_DB"] = "/tmp/eos_stealth_ai_test.db"
os.environ["ARK_API_KEY"] = "ark-fake-key-for-test"
if os.path.exists("/tmp/eos_stealth_ai_test.db"):
    os.remove("/tmp/eos_stealth_ai_test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import ai_correct                      # noqa: E402

CALLS = {"scenario": 0, "weakness": 0}


def fake_ark(system, user, max_tokens=1200, temperature=0.2, tag="ark"):
    if "情景设计师" in system:
        CALLS["scenario"] += 1
        return {"scenarios": [
            {"tier": "small", "prompt": "老板刚打电话你没接，发消息解释一下，用 %s。" % user.split("：")[1].split("\n")[0]},
            {"tier": "medium", "prompt": "这周你跟三个朋友断了联系，各写一句。"},
            {"tier": "large", "prompt": "你刚入职的第一周，要跟团队、老板、客户沟通。"},
            {"tier": "small", "prompt": "家里微信群吵起来了，你想让大家好好说话。"},
        ]}, None
    if "反复犯的错" in system:
        CALLS["weakness"] += 1
        return {"explain": "中文习惯省略主语，英文必须有「谁」，先补上 I / He。", }, None
    if "very important base work" in user:
        return {"corrected": "Communication is very important in base work.",
                "score": 40, "level": "需要改进", "is_sentence": 0,
                "error_tags": ["不是完整句子", "缺主语"],
                "errors": [{"type": "语法", "wrong": "communicate very important",
                            "right": "communication is very important",
                            "explain": "缺主语和谓语，这不是完整句子。"}],
                "natural": [], "summary": "先把句子补全。"}, None
    return {"corrected": "I usually communicate with my team by message.",
            "score": 92, "level": "基本掌握", "is_sentence": 1,
            "error_tags": [], "errors": [], "natural": [],
            "summary": "句子完整，表达自然。"}, None


ai_correct.ark_json = fake_ark

from fastapi.testclient import TestClient          # noqa: E402
import scenario as _scenario                       # noqa: E402
from main import app                               # noqa: E402

client = TestClient(app)
passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {extra}")


print("=== 1. 隐身学习取词 + 情景 ===")
r = client.get("/api/stealth/next").json()
check("接口可用", r.get("ok") is True or r.get("done") is True, str(r)[:200])
if r.get("done"):
    print("  （今天没有待练词，改用内置词直接测接口）")
    word = "communicate"
else:
    word = (r.get("word") or {}).get("word") or "communicate"

# 模拟「导入时主生成」：给这个词批量生成情景
_scenario.ensure_for_words([word], grammar="一般现在时")
check("导入触发生成了情景", _scenario.count_of(word) >= 3, f"{_scenario.count_of(word)} 条")

r = client.get("/api/stealth/next").json()
if not r.get("done"):
    check("next 带上情景", bool(r.get("scenario") and r["scenario"].get("prompt")),
          str(r.get("scenario")))
    check("情景带中文层级标签", bool(r["scenario"].get("tier_label")))
    first_id = r["scenario"]["id"]
else:
    sc = _scenario.with_label(_scenario.pick(word))
    check("next 带上情景（直连模块）", bool(sc and sc["prompt"]))
    first_id = sc["id"]

print("=== 2. 🔁 换场景（只轮换，不现场生成）===")
before = CALLS["scenario"]
r2 = client.get(f"/api/scenario/next?word={word}&cur={first_id}").json()
check("换场景接口可用", r2.get("ok") is True, str(r2)[:200])
check("换到了不同的一条", r2.get("scenario", {}).get("id") != first_id,
      f"{first_id} -> {r2.get('scenario', {}).get('id')}")
check("没有现场调 AI", CALLS["scenario"] == before)

print("=== 3. 提交造句 → AI 出分 ===")
r3 = client.post("/api/stealth/submit", json={
    "word": word, "sentence": "communicate very important base work",
    "grammar": "一般现在时", "ai": 1}).json()
check("提交成功", r3.get("ok") is True, str(r3)[:200])
ai = r3.get("ai") or {}
check("返回 AI 分数", ai.get("score") == 40, str(ai.get("score")))
check("返回 AI 判定", ai.get("level") == "需要改进", str(ai.get("level")))
check("标了不是完整句子", ai.get("is_sentence") == 0)
check("返回 error_tags", "不是完整句子" in (ai.get("error_tags") or []),
      str(ai.get("error_tags")))
check("本地分仍在（仅作兜底）", "score" in (r3.get("local") or {}))

print("=== 4. 薄弱项讲解只讲一次 ===")
for _ in range(2):
    client.post("/api/stealth/submit", json={
        "word": word, "sentence": "communicate very important base work",
        "grammar": "", "ai": 1})
r4 = client.post("/api/stealth/submit", json={
    "word": word, "sentence": "communicate very important base work",
    "grammar": "", "ai": 1}).json()
wk = r4.get("weakness")
check("反复同错后触发", bool(wk and wk.get("tag")), str(wk))
if wk:
    r5 = client.get(f"/api/weakness/explain?word={word}&tag={wk['tag']}&hits={wk['hits']}").json()
    check("拿到讲解", r5.get("ok") and bool(r5.get("explain")), str(r5)[:200])
    check("只调了一次 AI", CALLS["weakness"] == 1, f"{CALLS['weakness']}")
    r6 = client.get(f"/api/weakness/explain?word={word}&tag={wk['tag']}&hits={wk['hits']}").json()
    check("第二次走缓存", r6.get("explain") == r5.get("explain") and CALLS["weakness"] == 1)

print("=== 5. 关掉 AI（没配 Key）时本地兜底 ===")
ai_correct.ARK_API_KEY = ""
os.environ.pop("ARK_API_KEY", None)
r7 = client.post("/api/stealth/submit", json={
    "word": word, "sentence": "I usually communicate with my team.",
    "grammar": "", "ai": 1}).json()
check("提交仍成功", r7.get("ok") is True, str(r7)[:200])
check("AI 段为空（前端改显示「本地估算」）", not r7.get("ai"))
check("有友好错误提示", bool(r7.get("ai_error")), str(r7.get("ai_error")))
check("本地规则分仍在", (r7.get("local") or {}).get("score") is not None)

print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
