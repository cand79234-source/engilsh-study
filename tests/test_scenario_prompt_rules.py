"""情景 Prompt 规则回归：防止旧规则被加回来。

背景（本次改造要解决的问题）：
  1. AI 曾经会自己生成「语法：xxx」「用一般过去时……」这类要求，和页面统一显示的语法打架；
  2. 中/大情景曾经按句子数定义（medium=2-3 句 / large=约 20 句），导致要么太碎、
     要么为了塞词把十几件小事编号硬拼成一个"场景"；
  3. 大情景曾经要求所有词之间都有强因果，结果为了塞复习词不停新开独立事件
     （"办公 → 突然去买东西 → 又突然去吃饭 → 又发生另一件事……"）。

这个测试把"最终真正生效的 Prompt"拼出来做扫描，任何一条旧规则回来了都会红。

运行: pytest tests/test_scenario_prompt_rules.py -q
"""
import inspect
import os
import re
import sys
import tempfile

# 用独立临时库：本文件会 import scenario/services（会建表），
# 别把默认库带进来，免得和其它测试串数据。
os.environ.setdefault("EOS_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import scenario                    # noqa: E402
import services                    # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _build_user(word="account", meaning="账户；账目", grammar="一般现在时", extra=None):
    """复刻 scenario.generate_for_word 里 user 提示的拼装（与源码保持一致）。"""
    user = "目标词：%s" % word
    if meaning:
        user += "（%s）" % meaning
    if grammar:
        user += ("\n本周语法重点：%s（只是背景参考，让你知道该选什么样的场景；"
                 "**不要写进情景文字**，不要出现「语法：…」「请用…时」这类字样）" % grammar)
    extra = [e for e in (extra or []) if e != word]
    if extra:
        user += ("\n大情景（large）里可以自然带上的其他词：%s"
                 "——能自然嵌进场景就带上，嵌不进去不要新开事件硬塞。" % "、".join(extra[:6]))
    user += "\n请生成 4 条情景（small / medium / large 都要有）。"
    return user


def _full_prompt():
    """最终生效的完整 Prompt = system + 四种 user 分支（带/不带语法、带/不带复习词）。"""
    parts = [scenario._SYS]
    for grammar in ("", "一般现在时"):
        for extra in (None, ["choose", "umbrella", "restaurant"]):
            parts.append(_build_user(grammar=grammar, extra=extra))
    return "\n".join(parts)


# ---------------------------------------------------------------- 旧规则
def test_no_legacy_scale_rules():
    """旧的中/大情景"句子数"定义必须彻底消失（不是被新规则追加上去覆盖）。"""
    p = _full_prompt()
    for bad in ("20 句", "20句", "串 2-3 个相关词", "1-3 句", "写 2-3 句",
                "约 20", "5-8 句"):
        assert bad not in p, "旧场景规模规则还在：%r" % bad


def test_no_grammar_or_tense_instruction():
    """AI 不能自己产出语法要求 / 时态指令（语法由页面统一显示）。"""
    p = _full_prompt()
    for bad in ("（语法：", "语法：xxx）", "顺带给出一个搭配或语法要求",
                "情景里可以顺带要求用上", "用一般过去时", "一般过去时",
                "现在完成时", "请注意时态", "请用……时态"):
        assert bad not in p, "Prompt 里仍有语法/时态指令：%r" % bad
    # 语法点只能作为"背景参考"出现，且必须带"不要写进情景文字"的约束
    assert "只是背景参考" in p
    assert "不要写进情景文字" in p


def test_no_yesterday_template():
    """禁止「昨天 / 上周做过的事」这类固定任务模板（AI 侧 + 本地模板侧）。"""
    p = _full_prompt()
    for bad in ("昨天", "上周", "明天要做的事", "上周做过"):
        assert bad not in p, "Prompt 里仍有固定时间模板：%r" % bad
    # 本地造句模板（services.py）同样不许写死时间与自带时态指令。
    # 只扫"会显示给用户看的指令模板"，不扫知识点标签等数据。
    tmpls = "\n".join(t for _, t, _ in services.FUNC_CATEGORIES)
    tmpls += "\n" + inspect.getsource(services.build_sentence_prompts)
    for bad in ("昨天", "上周", "（一般过去时）", "（一般现在时）"):
        assert bad not in tmpls, "services.py 的指令模板里仍有固定时间/时态：%r" % bad
    # 指令模板里不许夹带括号式语法要求（语法由页面统一追加）
    assert not re.search(r"[（(][^）)]*时态[^）)]*[）)]", tmpls)


def test_no_numbered_event_list():
    """不许为了覆盖多个词，生成 10-20 个编号事件拼成的"场景"。"""
    p = _full_prompt()
    assert "编号事件清单" in p          # 明令禁止
    assert "按 1. 2. 3. 编号" in p
    for bad in ("10-20 个", "10~20 个", "10 到 20 个", "编号事件"):
        assert p.count(bad) <= 1, "疑似在要求编号事件：%r" % bad


# ---------------------------------------------------------------- 新规则
def test_medium_definition():
    """中情景 = 围绕 1 个核心学习词的具体、真实、完整小情境。"""
    p = _full_prompt()
    assert "1 个核心学习词" in p
    assert "不要为了多塞词而制造一堆事件" in p
    assert "account" in p and "水电账" in p        # 示例：周末核对水电账与银行卡扣款


def test_large_definition():
    """大情景 = 更完整丰富的真实情境，自然容纳多个学习词；不是事件堆砌。"""
    p = _full_prompt()
    assert "自然容纳多个学习词" in p
    assert "有因果、有时间线、有连续性" in p
    assert "刚入职公司的第一周" in p
    assert "不是把十几个小事件硬拼在一起" in p


def test_tier_by_capacity_not_sentence_count():
    """层级按场景容量分，不按句子数（句子数只是结果，不是指标）。"""
    p = _full_prompt()
    assert "按**场景容量**分，不按句子数" in p
    assert "写几句只是结果，不是指标" in p


def test_review_word_embedding_rules():
    """复习词：自然嵌入即可，不要求强因果，不许为它新开独立事件。"""
    p = _full_prompt()
    assert "不是**要求每个词之间都有强因果关系" in p
    assert "核心学习词承担场景主线" in p
    assert "生活转场、时间推进、人物对话" in p
    assert "不要为了让某个复习词出现，就新开一个独立事件" in p
    assert "就不要硬编事件，优先保证场景自然度" in p
    # 复习词确实会被传给模型（否则大情景无从嵌入）
    assert "可以自然带上的其他词" in p
    assert "嵌不进去不要新开事件硬塞" in p


def test_sys_keeps_json_contract():
    """输出契约没变：JSON + tier + prompt（改规则不能把接口改坏）。"""
    assert "只输出一个 JSON 对象" in scenario._SYS
    assert '{"scenarios":[{"tier":"small","prompt":"…"}, …]}' in scenario._SYS
    assert "情景设计师" in scenario._SYS          # 其它测试用它识别情景调用


# ---------------------------------------------------------------- 入库清洗
def test_sanitize_strips_grammar_instruction():
    """入库清洗：只删括号式语法要求和句尾时态指令，正常内容不动。"""
    s = scenario.sanitize_prompt
    assert s("周末在家核对水电账和银行卡扣款。（语法：一般过去时）") == \
        "周末在家核对水电账和银行卡扣款。"
    assert s("你和同事刚开完周会，（语法：一般现在时、频率副词）经理让你跟一下这个项目") == \
        "你和同事刚开完周会，经理让你跟一下这个项目"
    assert s("周一早上你把报销单交给财务（一般过去时）。") == "周一早上你把报销单交给财务。"
    assert s("把买错的东西退掉，请用一般过去时。") == "把买错的东西退掉"
    # 句中正常提到时态 / 时间的不许误删
    keep = "下班路上突然下雨，你没带伞，只能先用一般过去时把这件事讲清楚"
    assert s(keep) == keep
    assert s("周末坐在餐桌前核对这个月的水电账。") == "周末坐在餐桌前核对这个月的水电账。"


# ---------------------------------------------------------------- 前端
def _paint_scenario_src():
    html = open(os.path.join(ROOT, "frontend", "index.html"), encoding="utf-8").read()
    m = re.search(r"function paintScenario\([^)]*\)\{.*?\n\}", html, re.S)
    assert m, "找不到 paintScenario"
    return m.group(0)


def test_frontend_shows_grammar_uniformly():
    """语法由页面统一显示；不允许前端去"猜 AI 有没有写语法"。"""
    src = _paint_scenario_src()
    assert "（语法：${esc(grammar)}）" in src
    # 任何针对 sc.prompt 的关键词检测（正则匹配语法/时态/昨天）都算兜底猜测，禁止
    for bad in (".match(", ".test(", ".search(", "indexOf("):
        assert bad not in src, "paintScenario 里出现了对情景文本的关键词检测：%r" % bad
    # 情景文本只允许两处出现：空值判断 + 原样转义展示；不许拿它的内容做任何判断 / 兜底
    assert "if(!sc||!sc.prompt)" in src
    assert "esc(sc.prompt)" in src
    assert src.count("sc.prompt") <= 2, "paintScenario 对情景文本做了额外处理"
    # 除空值判断外，不许对情景文本做任何 if 判断
    rest = src.replace("if(!sc||!sc.prompt)", "")
    assert not re.search(r"if\s*\([^)]*(sc\.prompt|prompt\b)", rest)
