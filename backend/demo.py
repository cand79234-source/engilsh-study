# -*- coding: utf-8 -*-
"""演示模式（?demo=1）—— 让你在不动真实数据的前提下，看一眼周报/月报/薄弱项长什么样。

设计原则（这三条决定了它不会污染你的数据）：

1. **不写库**。演示模式只返回 backend/demo8.json 里预先算好的快照，
   一个 SQL 都不执行，所以根本不存在「看完要删数据」这回事。
2. **写操作一律拒绝**。POST / PUT / DELETE 在演示模式下直接 403，
   避免你在演示页手滑点了「标记已掌握」就写进真实库。
3. **开关只认网址参数**。不写 session、不写 cookie、不改数据库，
   网址去掉 ?demo=1 就立刻回到真实数据。

数据从哪来：tests/demo8_gen.py 灌 8 周假数据到**临时库**，
再调**真实的**统计代码（report.build_report / link.build_weakness / ...）
算出结果后快照成 JSON。所以演示数据的结构和真实接口 100% 一致，
不会出现「演示能显示、真实就崩」。

怎么撤掉：整个功能集中在
  backend/demo.py、backend/main.py 的几行接线、frontend/index.html 的几行
一个 commit 加进来，git revert 这个 commit 就完全回到原样。
"""
import json
import os

_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo8.json")

# 演示模式下返回快照的接口 → 对应 JSON 里的键。
# 这几个是「周报 / 月报 / 薄弱项 / 复习」要看的数据接口。
READ_ROUTES = {
    "/api/report": "report",
    "/api/progress": "progress",
    "/api/weakness": "weakness",
    "/api/errors": "errors",
    "/api/error-bank": "error_bank",
    "/api/errors/trend": "errors_trend",
    "/api/review/flashcards": "flashcards",
    "/api/review/due": "review_due",
    "/api/today": "today",
    "/api/home": "home",
    "/api/activity": "activity",
    "/api/history": "history",
    "/api/vocab/all": "vocab_all",
    "/api/weak/snapshots": "weak_snapshots",
    "/api/sentence/history": "sentence_history",
    "/api/training/summary": "training_summary",
}

# 带路径参数的接口：先把真实路径归一到模板，再查表。
# 例：/api/week/1/3  →  /api/week/{stage}/{week}
_PARAM_ROUTES = {
    "/api/week/{stage}/{week}": "week",
    "/api/quiz/{stage}/{week}": "quiz",
}


def _normalize(path):
    """把带具体参数的路径折成模板路径，便于查表。"""
    parts = path.strip("/").split("/")
    # /api/week/1/3 → 把第 3、4 段换成占位
    if len(parts) == 4 and parts[0] == "api" and parts[1] in ("week", "quiz"):
        try:
            int(parts[2]), int(parts[3])
            return "/api/%s/{stage}/{week}" % parts[1]
        except ValueError:
            pass
    return path


# 「配置类」接口：课程结构、导入格式说明之类的静态信息，
# 不含任何学习记录。演示模式下直接放行真实数据 ——
# 它们跟「学习数据」无关，拦了只会让页面莫名其妙报错。
PASS_THROUGH = {
    "/api/stages",
    "/api/curriculum",
    "/api/file/formats",
    "/api/dict/count",
    "/api/error-types",
    "/api/test/prompt",
    "/api/demo/status",
    "/api/health",
}

_cache = None


def load():
    """读快照。文件不存在或坏了就返回 None（调用方降级到真实数据）。"""
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _cache = json.load(f)
    except Exception:
        _cache = None
    return _cache


def available():
    return load() is not None


# 这次查询的三种结果
MISS = "miss"          # 没有演示数据 → 调用方 404
PASS = "pass"          # 配置类接口 → 放行真实数据


def get(path):
    """按接口路径取演示数据。

    返回 (状态, 数据)：
      ("hit", 数据)  有演示快照
      ("pass", None) 配置类接口，该走真实数据
      ("miss", None) 没有演示数据
    """
    d = load()
    if not d:
        return MISS, None
    key = READ_ROUTES.get(path)
    if not key:
        key = _PARAM_ROUTES.get(_normalize(path))
    if key:
        val = d.get("data", {}).get(key)
        if val is not None:
            return "hit", val
        return MISS, None
    if path in PASS_THROUGH:
        return PASS, None
    return MISS, None


def meta():
    d = load() or {}
    return d.get("_meta", {})


def reload_data():
    """测试用：强制重新读文件。"""
    global _cache
    _cache = None
    return load()
