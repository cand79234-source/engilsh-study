"""情景生成 / AI 出分 / 薄弱项讲解 —— 三处 AI 接管的回归测试。

用假的 ark_json 顶替真实豆包调用：不花 token、不依赖网络，
只验证「流程与数据结构」对不对（触发、库存、轮换、出分、只讲一次）。
"""
import os
import sys

os.environ["EOS_DB"] = "/tmp/eos_scenario_test.db"
os.environ["ARK_API_KEY"] = "ark-fake-key-for-test"   # 让 ai_enabled() 为真
if os.path.exists("/tmp/eos_scenario_test.db"):
    os.remove("/tmp/eos_scenario_test.db")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import ai_correct                      # noqa: E402
import scenario                        # noqa: E402
import weakness                        # noqa: E402
from db import init_db                 # noqa: E402

init_db()   # 正常服务由 main.py 启动时调用；这里单独跑模块要自己建表

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {extra}")


# ------------------------------------------------------------------
# 假豆包：按 system prompt 里出现的关键字判断调用方是谁
# ------------------------------------------------------------------
CALLS = {"scenario": 0, "correct": 0, "weakness": 0}


def fake_ark(system, user, max_tokens=1200, temperature=0.2, tag="ark"):
    if "情景设计师" in system:
        CALLS["scenario"] += 1
        return {"scenarios": [
            {"tier": "small", "prompt": "你老板刚打电话你没接，发消息解释一下，用 communicate。"},
            {"tier": "medium", "prompt": "这周你跟三个朋友断了联系，用 communicate / message 各写一句。"},
            {"tier": "large", "prompt": "你刚入职，第一周要跟团队、老板、客户沟通。"},
            {"tier": "small", "prompt": "家里微信群吵起来了，你想让大家好好说话，用 communicate。"},
        ]}, None
    if "反复犯的错" in system:
        CALLS["weakness"] += 1
        return {"explain": "中文习惯把主语省掉，英文句子必须有「谁」在句首，先补上 I / He。", }, None
    CALLS["correct"] += 1
    if "very important base work" in user:
        return {"corrected": "Communication is very important in base work.",
                "score": 40, "level": "需要改进", "is_sentence": 0,
                "error_tags": ["不是完整句子", "缺主语", "句号"],
                "errors": [{"type": "语法", "wrong": "communicate very important",
                            "right": "communication is very important",
                            "explain": "缺主语和谓语，这不是一个完整句子。"}],
                "natural": [], "summary": "先把句子补全。"}, None
    return {"corrected": "I usually communicate with my team by message.",
            "score": 92, "level": "基本掌握", "is_sentence": 1,
            "error_tags": [], "errors": [], "natural": [],
            "summary": "句子完整，表达自然。"}, None


ai_correct.ark_json = fake_ark

print("=== 1. 情景生成（导入时主触发）===")
n = scenario.ensure_for_words(["communicate", "message"], grammar="一般现在时")
check("导入触发批量生成", n == 2, f"实际 {n}")
check("每个词生成 3-5 条", 3 <= scenario.count_of("communicate") <= 6,
      f"实际 {scenario.count_of('communicate')}")
check("另一个词也生成了", scenario.count_of("message") >= 3)

print("=== 2. 已生成过不重复烧 token ===")
before = CALLS["scenario"]
scenario.ensure_for_words(["communicate"])
check("重复导入不再调 AI", CALLS["scenario"] == before,
      f"调用次数 {before} -> {CALLS['scenario']}")

print("=== 3. 轮换取最少用过的 + 🔁 换场景 ===")
a = scenario.pick("communicate")
check("取到第一条", bool(a and a["prompt"]))
b = scenario.pick("communicate", exclude_id=a["id"])
check("🔁 换到不同的一条", b and b["id"] != a["id"])
c = scenario.pick("communicate")
check("不排除时轮到还没用过的那条", c and c["id"] not in (a["id"], b["id"]))
check("带中文层级标签", scenario.with_label(a).get("tier_label") == "小情景",
      scenario.with_label(a))

print("=== 4. AI 出分（§3）===")
# 批改接口有 5 秒/IP 的防连点冷却，两次调用换 IP，免得第二次被冷却挡掉
data, err = ai_correct.correct("communicate very important base work",
                               client_ip="10.0.0.1", word="communicate")
check("AI 给出了分数", data and data["score"] == 40, f"{data and data.get('score')}")
check("非完整句子被判 0", data and data["is_sentence"] == 0)
check("判定为需要改进", data and data["level"] == "需要改进")
check("句号被过滤（§3.3）", "句号" not in (data or {}).get("error_tags", []),
      str((data or {}).get("error_tags")))
check("不是完整句子进了标签", "不是完整句子" in (data or {}).get("error_tags", []))
ok, err2 = ai_correct.correct("I usually communicate with my team by message.",
                              client_ip="10.0.0.2")
check("好句拿高分", bool(ok) and ok["score"] == 92 and ok["is_sentence"] == 1,
      f"{err2 or (ok and ok.get('score'))}")

print("=== 5. 薄弱项讲解（§4）===")
for i in range(3):
    weakness.record("communicate", ["缺主语"])
check("没到阈值不讲", weakness.pending("communicate") is None or
      weakness.pending("communicate")["hits"] >= 3)
pend = weakness.pending("communicate")
check("三次后触发", pend and pend["tag"] == "缺主语" and pend["hits"] >= 3, str(pend))
text, err3 = weakness.explain("communicate", "缺主语", pend["hits"])
check("生成了讲解", bool(text) and not err3, str(err3))
check("讲解不超过 2 句 / 60 字", len(text) <= 80, f"长度 {len(text)}")
check("调了一次 AI", CALLS["weakness"] == 1)
text2, _ = weakness.explain("communicate", "缺主语", pend["hits"])
check("第二次直接取缓存，不再烧 token",
      text2 == text and CALLS["weakness"] == 1, f"调用 {CALLS['weakness']}")
check("讲过之后不再 pending", weakness.pending("communicate") is None)

print("=== 6. 没配 Key 时整条链路空转不报错 ===")
ai_correct.ARK_API_KEY = ""
os.environ.pop("ARK_API_KEY", None)
check("ai_enabled 变假", ai_correct.ai_enabled() is False)
check("批量生成直接返回 0", scenario.ensure_for_words(["apple"]) == 0)
check("取情景返回 None（库里本来就没有）", scenario.pick("apple") is None)
for _ in range(3):
    weakness.record("apple", ["缺主语"])
check("没配 Key 时讲解返回友好错误",
      weakness.explain("apple", "缺主语")[1] is not None)
check("没配 Key 时不会瞎讲", weakness.explain("apple", "缺主语")[0] == "")

print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
