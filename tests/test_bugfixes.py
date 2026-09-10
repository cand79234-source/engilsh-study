"""pytest 风格回归测试：覆盖用户清单的 4 个硬性 bug 修复。

  - bug1: 导入丢失 stage（阶段1~5会写进阶段0）
  - bug2: update_week 误清空整周词汇
  - bug3: collocation / collocations 字段契约统一
  - bug4: 公网部署缺少鉴权

运行: pytest tests/test_bugfixes.py -v
"""
import os
import sys
import importlib

sys.path.insert(0, "backend")

DB = "/tmp/eos_bugfixes_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["EOS_DB"] = DB
os.environ.pop("EOS_TOKEN", None)  # 默认本地模式（不鉴权）

import main
from fastapi.testclient import TestClient
import services as svc
import weekimport


def fresh_app():
    """重新加载 main 模块，使 ACCESS_TOKEN 重新从当前 env 读取。

    鉴权依赖模块级全局变量，reload 后中间件会反映最新的 EOS_TOKEN。
    """
    return importlib.reload(main).app


# --------------------------------------------------------------------------
# bug1: 导入丢失 stage
# --------------------------------------------------------------------------
def test_bug1_stage_not_lost_to_stage0():
    svc.set_progress(2, 2, 1)  # 当前进度：阶段2 第2周
    before = svc.get_week(0, 2)
    assert before is None or not (before.get("vocab") or []), "前置：阶段0第2周应无词"

    text = """第2周｜阶段测试｜2词
Day 1｜测试组
alpha — 阿尔法
- This is the alpha version.
这是阿尔法版本。
beta — 贝塔
- We are in beta testing.
我们在做贝塔测试。
"""
    # 模拟「旧前端不传 stage」：不带 forced_stage
    r = weekimport.import_rich_week_merge(text)
    assert r["ok"], r
    assert r["stage"] == 2, f"不传stage应落到当前进度阶段2，实际 {r['stage']}"

    w2 = svc.get_week(2, 2)
    assert w2 and len(w2["vocab"]) == 2, "阶段2第2周应有2个词"
    w0 = svc.get_week(0, 2)
    assert (w0 is None) or not (w0.get("vocab") or []), "阶段0不应被污染"


def test_bug1_stage_text_wins_over_forced():
    svc.set_progress(2, 2, 1)
    text = "第2周｜x｜1词\nDay 1｜g\nalpha — 阿尔法\n- This is alpha.\n这是阿尔法。\n"
    # 文本写「阶段3」应优先于当前进度(2)
    r = weekimport.import_rich_week_merge("阶段3\n" + text)
    assert r["stage"] == 3, f"文本写的阶段应优先，实际 {r['stage']}"
    # 无文本时，前端传入的 forced_stage 优先于当前进度
    r2 = weekimport.import_rich_week_merge(text, forced_stage=1)
    assert r2["stage"] == 1, f"forced_stage应优先于当前进度，实际 {r2['stage']}"


# --------------------------------------------------------------------------
# bug2: update_week 误清空整周词汇
# --------------------------------------------------------------------------
def test_bug2_update_week_does_not_wipe_vocab():
    vocab = [{"word": f"w{i}", "meaning": f"词{i}", "day": 1} for i in range(120)]
    svc.update_week(0, 5, title="原标题", grammar="一般现在时", vocab=vocab)
    assert len(svc.get_week(0, 5)["vocab"]) == 120

    # 只改标题：不应清空词汇
    svc.update_week(0, 5, title="只改标题")
    wk = svc.get_week(0, 5)
    assert len(wk["vocab"]) == 120, "只改标题不应清空词汇"
    assert wk["title"] == "只改标题"

    # 只改语法：不应清空词汇
    svc.update_week(0, 5, grammar="现在完成时")
    assert len(svc.get_week(0, 5)["vocab"]) == 120

    # 明确传 [] 才清空（用户主动清空）
    svc.update_week(0, 5, vocab=[])
    assert len(svc.get_week(0, 5)["vocab"]) == 0


# --------------------------------------------------------------------------
# bug3: collocation / collocations 字段契约统一
# --------------------------------------------------------------------------
def test_bug3_collocations_array_contract():
    app = fresh_app()
    client = TestClient(app)

    # 老格式词条：collocation 是字符串（自动填充型）
    svc.update_week(0, 1, title="测试周", vocab=[{
        "word": "apple", "meaning": "苹果", "pos": "名词",
        "collocation": "an apple / eat an apple",
        "example": "I eat an apple.", "translation": "我吃一个苹果。", "day": 1,
    }])
    svc.set_progress(0, 1, 1)

    r = client.get("/api/today").json()
    w = r["words"][0]
    assert w["word"] == "apple"
    assert isinstance(w["collocations"], list) and w["collocations"], \
        "/api/today 必须返回 collocations 数组，不能是缺失的单数字段"

    # 前端按统一契约发送数组
    resp = client.post("/api/word/master", json={
        "word": "apple", "mastered": 2, "meaning": "苹果",
        "collocations": w["collocations"],
    })
    assert resp.status_code == 200, resp.text

    # 兼容历史上发单数字符串 collocation 的调用方
    resp2 = client.post("/api/word/master", json={
        "word": "apple", "mastered": 2, "meaning": "苹果",
        "collocation": "eat an apple",
    })
    assert resp2.status_code == 200

    due = client.get("/api/review/due").json()
    apple_items = [i for i in due["items"] if i["ref_key"] == "apple"]
    assert apple_items, "已掌握的词应进入复习队列"
    assert "eat an apple" in apple_items[0]["answer"], "搭配应进入 SRS 卡片"


# --------------------------------------------------------------------------
# bug4: 公网部署缺少鉴权
# --------------------------------------------------------------------------
def test_bug4_auth_enforced_when_token_set():
    os.environ["EOS_TOKEN"] = "test-token-123"
    app = fresh_app()
    client = TestClient(app)

    # 健康检查/保活永远不鉴权
    assert client.get("/api/health").status_code == 200

    # 读接口无口令 → 401
    assert client.get("/api/today").status_code == 401
    # 错误口令 → 401
    assert client.get("/api/today", headers={"X-Auth-Token": "wrong"}).status_code == 401
    # 正确口令（X-Auth-Token）→ 200
    assert client.get("/api/today", headers={"X-Auth-Token": "test-token-123"}).status_code == 200
    # Bearer 形态也接受
    assert client.get("/api/today", headers={"Authorization": "Bearer test-token-123"}).status_code == 200

    # 写接口无口令 → 401
    assert client.post("/api/progress", json={"stage": 0, "week": 1, "day": 1}).status_code == 401
    # 写接口对口令 → 200
    assert client.post("/api/progress", json={"stage": 0, "week": 1, "day": 1},
                       headers={"X-Auth-Token": "test-token-123"}).status_code == 200

    # 恢复本地模式（避免污染同进程内的其它测试模块）
    os.environ.pop("EOS_TOKEN", None)
    app_local = fresh_app()
    client_local = TestClient(app_local)
    assert client_local.get("/api/today").status_code == 200, "清掉EOS_TOKEN后应恢复开放"
