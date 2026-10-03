# -*- coding: utf-8 -*-
"""富文本周词导入解析器（纯本地，无 AI）。

把用户一次性粘贴的大段内容解析成：目标周 + 若干组(day) 的单词表。
设计目标：能识别真实用户的宽松粘贴，不因"单词库里没有"就丢弃或报 bug。

支持两种主要格式（自动识别）：

A) 逐行式（旧）
   第2周｜工作与日常｜120词
   第1组｜职场人物与环境
   colleague — 同事
   colleague的英文例句.
   (下方可跟例句中文，若在逐行式里出现则暂不支持成对)

B) 块状式（推荐，支持"英文例句+中文"成对 + 固定搭配）
   第2周｜工作与日常｜120词

   第1组｜第一天上班：认识新工作

   1. company — 公司

   I started working for a new company this week.      <- 英文例句
   我这周开始在一家新公司工作。                         <- 该句中文

   ...
   固定搭配：「work for a company」为一家公司工作；「start a new job」开始一份新工作

   ---
   2. position — 职位
   ...

输出：
{
  "stage": int, "week": int | None,
  "title": str | "", "grammar": str | "",
  "groups": [ {"day":int, "name":str, "words":[word条目,...]}, ... ],
  "flat": [...],
  "warnings": [ ... ], "skipped": [...], "header_lines": [...]
}
每个 word 条目形如：
  {"word":..., "meaning":..., "pos":...,
   "examples":[{"sentence":..., "translation":...}, ...],
   "collocations":[{"phrase":..., "meaning":...}, ...]}

自动识别周号与组号（一组=一天）。同组内重复词只保留首次。
"""
import re

# ---------- 中文/词性识别 ----------
_POS_PATTERN = re.compile(
    r"(?:〔|\[|\()?\s*(名词|动词|形容词|副词|介词|代词|连词|感叹词|情态动词|数词|助动词|冠词|及物动词|不及物动词)"
    r"(?:/\s*(名词|动词|形容词|副词|介词|代词|连词|感叹词|情态动词|数词|助动词|及物动词|不及物动词|冠词))?\s*(?:\]|\))?\s*")

_CN_PATTERN = re.compile(r"[\u4e00-\u9fff][\u4e00-\u9fff·/、，,（）()0-9a-zA-Z\- ]*")
# 过滤明显不是词的英文占位
_EN_SKIP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for",
            "with", "by", "is", "are", "was", "were", "be", "do", "does", "did",
            "have", "has", "had", "not", "but", "so", "very", "it", "he", "she",
            "we", "they", "you", "this", "that", "these", "those",
            "vocabulary", "english", "第", "第组"}


# 单词名允许的字符：字母开头，后面可跟字母 / 撇号 / 连字符 / 数字。
# 加数字是为了兼容 iPhone12 / word2 / COVID-19 / x-ray 这类真实词形。
# 首字符仍要求是字母，所以 "2026"、"1)" 这类编号不会被误判成单词。
_WORD_CHARS = r"[A-Za-z][A-Za-z0-9'\-]*"


def _is_english_word(tok):
    if not tok:
        return False
    if not re.fullmatch(_WORD_CHARS, tok):
        return False
    if tok.lower() in _EN_SKIP:
        return False
    return True


# 识别"整行是一句英文"(无中文、含句末标点)
_IS_ENG_SENT = re.compile(r"^[A-Za-z0-9 .,!?'\-;:()\"\/\u2019\u2018]+[.!?]$")
# IPA 音标片段，如 /kənˈtɪnjuː/ /ˈkɒlɪɡ/ /ɪmˈpruːv/。
# 词头行里夹在单词与中文释义之间，必须优先剥离，否则会被当成"多个英文词"。
_IPA_SEG_RE = re.compile(r"\s*/\[?[^/]{1,40}/\]?\s*")
# 识别"整行纯中文句子"(用于配对例句中译)
_HAS_CN = re.compile(r"[\u4e00-\u9fff]")
# 固定搭配行：形如  固定搭配：「a」b；「c」d
_COLLOC_LINE = re.compile(r"^\s*(?:固定搭配|搭配|词组|短语)\s*[:：]?\s*(.+)$")
# 情景（场景）标签行：形如  Mini Scenario 1： / Mini Scenario 1: / 场景1： / 情景 2：
# 用户粘贴的整周材料里，每个词后面跟着 3 条英文 mini scenario，格式固定是
#     Mini Scenario 1：
#     You meet your new colleague at the office and say hello.
# 这行是**纯标签**（后面什么都没有），真正的英文在下一行。
# 必须单独识别：不认它的话，标签行被丢掉、下面那句英文会被当成"第 4 条例句"
# 混进 examples —— 例句区凭空多出三句没中译的英文，情景也永远进不了库。
_SCENE_LINE = re.compile(
    r"^\s*(?:mini\s*)?(?:scenario|scene|situation|情景|场景|情境)\s*"
    r"([0-9０-９]{1,2})?\s*[:：.、]?\s*(.*)$", re.I)
# 行首列表标记（-、•、·等；不含 */★，它们是重点词标记）
_LIST_MARK_RE = re.compile(r"^[\s]*[-–—•·▪◦‣▪]+\s+")

# 场景块标题（中文格式，与前端 WEEK_SCENE_PROMPT_TPL 输出一致）：
#   场景 1｜<名称>        → 小场景（daily 10 个小场景，2–3 个当天词）
#   小场景 1｜<名称>      → 小场景（同上，AI 输出的 markdown "### 小场景N｜" 也认）
#   大场景 1｜<名称>      → 大场景（综合复习，复习词 + 当天词）
# 前缀「大/小」判定 tier；后面跟数字才认，避免误伤「今日综合复习｜5个大场景」这类分区标题。
# 行首的 markdown "#/##/### " 不影响识别：finditer 会在文本里扫到「小场景N｜」子串。
_SCENE_TITLE_RE = re.compile(r"(大|小)?\s*场景\s*(\d+)\s*[｜|]\s*(.*)")
# 块内字段头（与用户给定格式一致）
_SCENE_DESC_RE = re.compile(r"^场景说明\s*[:：]")          # 场景文本（中文情境）
_USE_WORDS_RE = re.compile(r"^使用单词\s*[:：]")          # 小场景：使用的当天词
_REVIEW_WORDS_RE = re.compile(r"^复习单词\s*[:：]")        # 大场景：复习词
_CURR_WORDS_RE = re.compile(r"^当天单词\s*[:：]")          # 大场景：当天词
_EXPR_RE = re.compile(r"^综合表达\s*[:：]")               # 大场景：英文表达（追加进文本）
_CN_MEAN_RE = re.compile(r"^中文意思\s*[:：]")            # 大场景：中文翻译（忽略，仅辅助）


def _parse_scene_line(s):
    """识别情景标签行，返回 (场景序号或None, 同一行里跟在标签后的内容)。

    「Mini Scenario 1：」这种**行尾没内容**的标签行返回 ("", "")——它不是情景
    本身，只是一个"接下来这句是情景"的牌子，真正内容在下一行。
    同一行就写了内容的（"Mini Scenario 1：You meet your colleague."）也能认，
    此时第 2 个返回值就是那句英文。

    不是情景行 → None。判定从严（必须是整行就是标签），避免把
    "In this scenario, you need to talk to your manager." 这类正常句子误伤。
    """
    s = (s or "").strip()
    if not s or len(s) > 60:
        return None
    m = _SCENE_LINE.match(s)
    if not m:
        return None
    num, rest = m.group(1), (m.group(2) or "").strip()
    # 标签后面若还跟着一整句英文（以 . ! ? 收尾），说明是"标签+内容"同一行
    if rest and not re.search(r"[.!?。！？]$", rest) and _HAS_CN.search(rest):
        # 后面跟的是中文说明（如 "情景1：在办公室打招呼"），也认，当作内容
        pass
    if not rest:
        return (num or ""), ""
    return (num or ""), rest


def _strip_list_marker(s):
    """去掉行首的列表标记（如 '- I like it.' → 'I like it.'）。"""
    s = _LIST_MARK_RE.sub("", s.rstrip())
    # 用户/AI 常用 "* " 做例句列表标记（官方提示词用 "- "，实际粘贴多为 "* "）。
    # "*" 同时是重点词标记，因此只在例句识别这条路径上剥离行首星号。
    s = re.sub(r"^\s*\*\s+", "", s)
    return s


def _is_eng_sentence(s):
    """整行是否像一句英文例句（容忍行首 '- '/'* ' 等列表标记）。"""
    s = _strip_list_marker(s.strip())
    if len(s) < 8 or len(s) > 300:
        return False
    if _HAS_CN.search(s):
        return False
    return bool(_IS_ENG_SENT.match(s))


def _is_cn_only(s):
    """整行是否主要是中文(可作为例句的中译)。"""
    s = s.strip()
    if not s or len(s) > 400:
        return False
    if len(s) < 2:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
    return cjk >= max(1, len(s) * 0.3) and not _is_eng_sentence(s)


def _parse_colloc_line(s):
    """解析 '固定搭配：「a」b；「c」d' → [{"phrase":..,"meaning":..},...]。"""
    m = _COLLOC_LINE.match(s)
    if not m:
        return None
    body = m.group(1)
    items = []
    # 按；或; 切，同时容错没有分号的连续「」
    parts = re.split(r"[；;]", body)
    buf = parts
    # 每个 part 含「phrase」meaning
    for p in buf:
        p = p.strip()
        fm = re.search(r"「([^」]+)」\s*(.*)", p)
        if fm:
            phrase = fm.group(1).strip()
            meaning = fm.group(2).strip()
            items.append({"phrase": phrase, "meaning": meaning})
    if not items:
        return None
    return items


def _split_pos_and_cn(rest):
    pos = ""
    m = _POS_PATTERN.match(rest)
    if m and (m.end() - m.start()) > 0:
        pos = "".join(ch for ch in m.group(0) if '\u4e00' <= ch <= '\u9fff')
        rest = rest[m.end():]
    cm = _CN_PATTERN.match(rest)
    meaning = cm.group(0).strip() if cm else ""
    return pos, meaning


def _strip_md_hash(s):
    """去掉行首 Markdown 标题前缀（#/##/###…），让 '### 1. word'、'## Day 1'、
    '# 第1周｜…' 这类也能被当作普通单词/周/组标题解析。"""
    return re.sub(r"^#{1,6}\s*", "", s or "").strip()


# 识别周/组行（支持中英文写法：第2周 / Week 2 / 第1组 / Day 1 / 第1天）
_WEEK_RE = re.compile(
    r"(?:第\s*([0-9０-９]{1,2})\s*周|(?:week|wk)[\s.]*([0-9０-９]{1,2}))", re.I)
_GROUP_RE = re.compile(
    r"(?:第\s*([0-9０-９]{1,2})\s*组|day[\s.]*([0-9０-９]{1,2})|第\s*([0-9０-９]{1,2})\s*天)",
    re.I)
# 阶段行：阶段1｜… / Stage 1｜…（文本里明确写了阶段就按它来）
_STAGE_RE = re.compile(
    r"(?:阶段\s*([0-9０-９]{1,2})|(?:stage|phase)[\s.]*([0-9０-９]{1,2}))", re.I)


def _to_int(t):
    if t is None:
        return None
    t = t.strip()
    if t.isdigit():
        return int(t)
    try:
        return int(t)
    except Exception:
        return None


def _parse_word_header(line):
    """尝试把一行解析成单词头。支持：
       company — 公司
       1. company — 公司
       15. responsibility — 责任 / 职责
       11. branch — 分公司 / 分部
    """
    s = line.strip()
    # 去掉行首 Markdown 标题前缀（### 1. word → 1. word）
    s = _strip_md_hash(s)
    # 去掉行首序号 "1." "15." "40)" "2、" 及列表标记 "•" "①" 等
    s = re.sub(r"^\s*(?:[0-9０-９]{1,3}\s*[.、)）]|[•·▪◦‣]|[\u2460-\u2473])\s*", "", s)
    for sep in ("—", "–", "－", "："):
        if sep in s:
            left, right = s.split(sep, 1)
            return _build(left, right)
    m = re.match(r"^(" + _WORD_CHARS + r")(?:[\s　]+(.+))?$", s)
    if m and _is_english_word(m.group(1)):
        rest = (m.group(2) or "").strip()
        focus = False
        if "★" in rest or "*" in rest:
            focus = True
            rest = re.sub(r"[★*]", "", rest).strip()
        # 这条路径是「单词 + 空格 + 释义」（无 — 分隔符），音标同样可能紧随单词
        phonetic = ""
        m_ipa = _IPA_SEG_RE.search(rest)
        if m_ipa:
            phonetic = m_ipa.group(0).strip().strip("[]").strip()
            rest = _IPA_SEG_RE.sub(" ", rest).strip()
        pos, cn = _split_pos_and_cn(rest) if rest else ("", "")
        if rest and not cn:
            cn = rest
        return {"word": m.group(1), "meaning": cn or "", "pos": pos,
                "focus": focus, "phonetic": phonetic}
    return None


def _is_word_header_line(line):
    """单词头通常是"短行"，且不是一句英文例句。用于避免把英文例句首词误当单词。"""
    s = _strip_md_hash(line.strip())   # 先去 Markdown '#' 前缀（### 1. word…）再判
    if not s or len(s) > 60:
        return False
    if s[-1] in ".!?":
        return False  # 以句末标点结尾的多半是例句
    if _is_eng_sentence(s):
        return False
    # 词数控制：一行若含多个空格分隔的英文词，多为句子而非词头
    # 先剥掉 IPA 音标再数英文词：音标里的 ASCII 字母（k/ə/n/ˈ/t/ɪ/n/j/u/ː 中的
    # k,n,t,nju…）会被 [A-Za-z]+ 切出碎片，导致 "continue /kənˈtɪnjuː/"
    # 被数成 5 个英文词而误判为"句子不是词头"。
    s_wo_ipa = _IPA_SEG_RE.sub(" ", s)
    eng_tokens = re.findall(r"[A-Za-z]+", s_wo_ipa)
    if len(eng_tokens) > 2:
        return False
    return True


def _build(left, right):
    left = left.strip()
    right = right.strip()
    # 先**取出**左侧的 IPA 音标（AI 生成的词表几乎每行都带），再剥离。
    #   原正则 ^([A-Za-z][A-Za-z'\-]*)...$ 要求 left 只能是纯单词，
    #   "continue /kənˈtɪnjuː/" 拖着音标 → 不匹配 → 返回 None → 一个词都导不进去。
    #   当年的修法是把音标直接丢了，代价是导入的词全都没音标，只能靠
    #   ECDICT 词典事后补 —— 词典里没有的词就永远空着，用户看到的就是
    #   「有些有音标、有些没有」。现在改成提取保留。
    phonetic = ""
    m_ipa = _IPA_SEG_RE.search(left)
    if m_ipa:
        phonetic = m_ipa.group(0).strip()
        # 去掉可能包在外面的方括号，只留 /.../
        phonetic = phonetic.strip("[]").strip()
    left = _IPA_SEG_RE.sub(" ", left).strip()
    # ★ 重点词标记：用户在单词行打 ★，表示"这个词要重点升级"
    focus = False
    if "★" in left or "★" in right or "*" in left or "*" in right:
        focus = True
        left = re.sub(r"[★*]", "", left).strip()
        right = re.sub(r"[★*]", "", right).strip()
    m = re.match(r"^(" + _WORD_CHARS + r")(?:[\s　]*\(([^)]*)\))?$", left)
    if not m or not _is_english_word(m.group(1)):
        return None
    pos, cn = _split_pos_and_cn(right)
    return {"word": m.group(1), "meaning": cn or "", "pos": pos,
            "focus": focus, "phonetic": phonetic}


# ---------------- 周/组信息提取（通用） ----------------
def _read_headers(line, week_ref, title_ref, group_ref):
    """若行为周/组标题则更新并返回 (kind, info)。kind: 'week'|'group'|None。

    支持写法（大小写不敏感）：
      第2周｜工作与日常｜120词   /  Week 2｜工作与日常
      第1组｜职场人物             /  Day 1｜第一天上班   /  第1天｜…
    以句末标点结尾的行视为句子而非标题（防止 "Day 1 was my first day." 误判）。
    """
    s = line.strip()
    # 去掉 Markdown 标题前缀（#/##/###），让 '# 第1周｜…' 也能认
    s = _strip_md_hash(s)
    if not s:
        return None
    if s and s[-1] in ".!?，。！？；;":
        return None
    wm = _WEEK_RE.search(s)
    if wm and len(s) <= 60:
        wk = _to_int(wm.group(1) or wm.group(2))
        if wk is not None:
            week_ref[0] = wk
        t = _WEEK_RE.sub("", s)
        t = re.sub(r"[|｜]", " ", t)
        t = re.sub(r"\d+\s*词", "", t)
        t = re.sub(r"\s+", " ", t).strip(" |｜，,。:：")
        if t and not re.fullmatch(r"[0-9a-zA-Z\s]+", t):
            title_ref[0] = t
        return "week"
    gm = _GROUP_RE.search(s)
    if gm and len(s) <= 80:
        gd = _to_int(gm.group(1) or gm.group(2) or gm.group(3)) or 1
        name = _GROUP_RE.sub("", s)
        name = re.sub(r"[|｜:：\s]+", " ", name).strip("|｜，,。:：-")
        group_ref[0] = gd
        group_ref[1] = name
        return "group"
    return None


# ---------------- 块状解析 ----------------
def _looks_like_block(text):
    """检测是否块状格式：出现 '固定搭配：' 或 '---' 分块符。"""
    return ("固定搭配" in text or "固定搭配" in text) or ("---" in text)


def _parse_block(text):
    week_ref = [None]
    title_ref = [""]
    group_ref = [1, ""]        # [day, name]
    stage_ref = [None]         # 文本里明确写的阶段号（None=没写）
    groups = {}
    skipped = []
    header_lines = []

    def ensure_group(day, name=None):
        g = groups.setdefault(day, {"day": day, "name": "", "words": []})
        if name:
            g["name"] = name
        return g

    cur_word = None
    # 记录当前词"上一个未配对中文"所对应的例句
    raw_lines = text.splitlines()
    i = 0
    n = len(raw_lines)
    # 把连续行按空行/--- 分段，但中文翻译独立于空行。用逐行状态机：
    pending_ex = None  # 最近一个未配中译的例句
    expect_scene = None  # 刚读到「Mini Scenario N：」牌子，下一行英文是情景

    for i, raw in enumerate(raw_lines):
        line = raw.strip()
        if not line:
            continue
        # 分块线
        if re.fullmatch(r"[-–—=_]{2,}", line):
            continue
        # 阶段行：阶段2｜…（短行才认，避免误伤正文）
        sm = _STAGE_RE.search(line)
        if sm and len(line) <= 30 and not re.search(r"[。！？.!?]$", line):
            st = _to_int(sm.group(1) or sm.group(2))
            if st is not None:
                stage_ref[0] = st
                header_lines.append(raw)
                continue
        # 周/组标题
        kind = _read_headers(line, week_ref, title_ref, group_ref)
        if kind == "week":
            header_lines.append(raw)
            cur_word = None
            continue
        if kind == "group":
            ensure_group(group_ref[0], group_ref[1])
            header_lines.append(raw)
            cur_word = None
            continue
        # 固定搭配行 → 挂到当前词
        colloc = _parse_colloc_line(line)
        if colloc is not None:
            if cur_word is not None:
                cur_word.setdefault("collocations", []).extend(colloc)
            continue
        # 情景（场景）行 → 挂到当前词，**必须在"英文例句"判断之前**。
        #   否则 "Mini Scenario 1：" 之下的三句英文会被当成第 4/5/6 条例句
        #   混进 examples（例句区凭空多三句没中译的英文，场景也进不了库）。
        scene = _parse_scene_line(line)
        if scene is not None:
            if cur_word is not None:
                snum, sbody = scene
                if sbody:
                    cur_word.setdefault("scenes", []).append(
                        {"n": snum, "text": _strip_list_marker(sbody)})
                    expect_scene = None
                else:
                    # 只有牌子、没内容 → 下一行英文才是情景
                    expect_scene = snum
            continue
        # 英文例句 → 新开一条例句（中文随后配对）【须先于单词头判断，
        #   否则 "I started working..." 会被误认成单词 "I"】
        if _is_eng_sentence(line):
            if cur_word is not None:
                if expect_scene is not None:
                    # 上一条是「Mini Scenario N：」牌子 → 这句是情景，不是例句
                    cur_word.setdefault("scenes", []).append(
                        {"n": expect_scene, "text": _strip_list_marker(line)})
                    expect_scene = None
                else:
                    ex = {"sentence": _strip_list_marker(line), "translation": ""}
                    cur_word.setdefault("examples", []).append(ex)
                    pending_ex = ex
            continue
        # 中文行 → 若前一句例句缺中译则配对，否则当作说明文字
        if _is_cn_only(line) and cur_word is not None:
            if pending_ex is not None and not pending_ex["translation"]:
                pending_ex["translation"] = line
                pending_ex = None
            else:
                skipped.append(raw)
            continue
        # 单词头（仅在行较短且非例句时）
        if _is_word_header_line(line):
            w = _parse_word_header(line)
            if w and _is_english_word(w["word"]):
                w.setdefault("examples", [])
                w.setdefault("collocations", [])
                w.setdefault("scenes", [])
                ensure_group(group_ref[0], group_ref[1])["words"].append(w)
                cur_word = w
                pending_ex = None
                expect_scene = None
                continue
        # 其它 → 注释/说明
        skipped.append(raw)

    return {
        "stage": stage_ref[0], "stage_from_text": stage_ref[0],
        "week": week_ref[0], "title": title_ref[0], "grammar": "",
        "groups": groups, "flat": None, "skipped": skipped, "header_lines": header_lines,
    }


# ---------------- 逐行式解析（向后兼容） ----------------
def _parse_line(text):
    week_ref = [None]
    title_ref = [""]
    group_ref = [1, ""]
    stage_ref = [None]
    groups = {}
    skipped = []
    header_lines = []
    cur_word = None
    pending_ex = None
    # ⚠️ 2026-09-11：逐行式也要认 Mini Scenario（和块状式对齐）。
    # 老版本 _parse_line 完全不认场景标签，于是「Mini Scenario 1：」被丢进
    # skipped、它下面的英文句子被当成第 4/5/6 条例句 —— 基础句就没有自带情景了。
    expect_scene = None      # 刚读到场景标签后，等它的英文句子

    def ensure_group(day, name=None):
        g = groups.setdefault(day, {"day": day, "name": "", "words": []})
        if name:
            g["name"] = name
        return g

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 跳过 Markdown 分隔线 / 旧式分块线
        if re.fullmatch(r"[-–—=_]{2,}", line):
            continue
        sm = _STAGE_RE.search(line)
        if sm and len(line) <= 30 and not re.search(r"[。！？.!?]$", line):
            st = _to_int(sm.group(1) or sm.group(2))
            if st is not None:
                stage_ref[0] = st
                header_lines.append(raw)
                continue
        kind = _read_headers(line, week_ref, title_ref, group_ref)
        if kind == "week":
            header_lines.append(raw)
            continue
        if kind == "group":
            ensure_group(group_ref[0], group_ref[1])
            header_lines.append(raw)
            continue
        colloc = _parse_colloc_line(line)
        if colloc is not None and cur_word is not None:
            cur_word.setdefault("collocations", []).extend(colloc)
            continue
        # 场景标签（Mini Scenario 1：/ 场景1：/ 情景一：…）→ 进入"等下一条英文"状态
        if cur_word is not None:
            _sc = _parse_scene_line(line)
            if _sc is not None and (_sc[1] or not _is_eng_sentence(line)):
                expect_scene = _sc[0] or str(len(cur_word.get("scenes") or []) + 1)
                cur_word.setdefault("scenes", [])
                if _sc[1]:      # 标签后面直接跟了正文（同一行）
                    cur_word["scenes"].append({"n": expect_scene, "text": _sc[1]})
                    expect_scene = None
                continue
        w = _parse_word_header(line)
        if w and _is_english_word(w["word"]):
            w.setdefault("examples", [])
            w.setdefault("collocations", [])
            w.setdefault("scenes", [])
            ensure_group(group_ref[0], group_ref[1])["words"].append(w)
            cur_word = w
            pending_ex = None
            expect_scene = None
            continue
        if cur_word is not None and _is_eng_sentence(line):
            _txt = _strip_list_marker(line)
            # 场景标签下面那条英文 → 归到 scenes，不当例句
            if expect_scene is not None:
                cur_word.setdefault("scenes", []).append(
                    {"n": expect_scene, "text": _txt})
                expect_scene = None
                pending_ex = None
                continue
            ex = {"sentence": _txt, "translation": ""}
            cur_word["examples"].append(ex)
            pending_ex = ex
            continue
        if cur_word is not None and _is_cn_only(line):
            if pending_ex is not None and not pending_ex["translation"]:
                pending_ex["translation"] = line
                pending_ex = None
            else:
                skipped.append(raw)
            continue
        skipped.append(raw)
    return {
        "stage": stage_ref[0], "stage_from_text": stage_ref[0],
        "week": week_ref[0], "title": title_ref[0], "grammar": "",
        "groups": groups, "flat": None, "skipped": skipped, "header_lines": header_lines,
    }


def _finalize(parsed):
    """去空组 + 组内去重 + 组名清洗 + 生成 flat。返回结构化的 groups/flat/warnings。"""
    groups = parsed["groups"]
    warnings = []
    for k in list(groups):
        g = groups[k]
        g["name"] = (g.get("name") or "").strip()
        if not g["words"]:
            del groups[k]
            continue
        seen = set()
        kept = []
        for w in g["words"]:
            key = w["word"].lower()
            if key in seen:
                warnings.append(f"第{k}组中「{w['word']}」重复，已保留首次出现。")
                continue
            seen.add(key)
            kept.append(w)
        g["words"] = kept
    flat = []
    for g in sorted(groups.values(), key=lambda x: x["day"]):
        for w in g["words"]:
            flat.append({"day": g["day"], **w})
    groups = [groups[k] for k in sorted(groups)]
    return groups, flat, warnings


def _normalize_text(text):
    """粘贴/文件提取的文本统一清洗：
    去零宽字符（\ufeff/\u200b 等，网页与聊天工具粘贴的典型产物），
    不间断空格 → 普通空格，统一换行符。"""
    if not text:
        return ""
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060\u00ad]", "", text)
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _extract_words_from_line(line):
    """从「使用单词：family, parent, relative」这类行抽出英文词列表。

    支持逗号/顿号/空格分隔；会去掉 （Day1） 这类中文/英文括号备注。
    """
    s = re.sub(r"[（(][^）)]*[)）]", "", line)        # 去掉括号备注
    parts = re.split(r"[,，、\s]+", s.strip())
    out = []
    for p in parts:
        p = p.strip().strip("`").strip()
        if re.match(r"^[a-zA-Z][a-zA-Z'\-]*$", p):
            out.append(p.lower())
    return out


def _parse_large_scene_block(block):
    """解析单个 场景/大场景 块正文，返回 (new_words, review_words, scene_text, tier)。

    小场景结构：
        场景 1｜<名称>        （或 ### 小场景1｜…）
        场景说明：<情境描述>
        使用单词：family, parent, relative
    大场景结构：
        大场景 1｜<名称>      （或 ### 大场景1｜…）
        场景说明：<情境描述>
        复习单词：currently, hometown, lifestyle
        当天单词：family, parent, relative
        综合表达：<英文表达>   （可选，追加进文本）
        中文意思：<中文翻译>   （可选，忽略）
    new_words  = 小场景的「使用单词」+ 大场景的「当天单词」（当天要练的词）；
    review_words = 大场景的「复习单词」（旧词复习，标记 review）。
    旧版只返回 (words, text, tier) 且把 new/review 混在一起，导致大场景的复习词
    和小场景的新词无法区分——这里拆开，供『按天场景计划』把复习词标 🔁。
    """
    new_words = []
    review_words = []
    scene_text = []
    tier = "small"
    mode = None
    for raw in block.splitlines():
        s = _strip_md_hash(raw.strip())      # 去行首 Markdown '#' 前缀（### 小场景1｜…）
        if not s:
            continue
        tm = _SCENE_TITLE_RE.match(s)
        if tm:                                  # 块首标题行：判定 tier
            # 只有前缀是「大」才判大场景；「小」或纯「场景」都归小场景
            tier = "large" if tm.group(1) == "大" else "small"
            continue
        if _SCENE_DESC_RE.match(s):
            mode = "desc"
            rest = _SCENE_DESC_RE.sub("", s).strip()
            if rest:
                scene_text.append(rest)
            continue
        if _USE_WORDS_RE.match(s):
            mode = "use"
            new_words.extend(_extract_words_from_line(_USE_WORDS_RE.sub("", s)))
            continue
        if _REVIEW_WORDS_RE.match(s):
            mode = "review"
            review_words.extend(_extract_words_from_line(_REVIEW_WORDS_RE.sub("", s)))
            continue
        if _CURR_WORDS_RE.match(s):
            mode = "curr"
            new_words.extend(_extract_words_from_line(_CURR_WORDS_RE.sub("", s)))
            continue
        if _EXPR_RE.match(s):                    # 综合表达：英文，追加进场景文本
            mode = "expr"
            rest = _EXPR_RE.sub("", s).strip()
            if rest:
                scene_text.append(rest)
            continue
        if _CN_MEAN_RE.match(s):                 # 中文意思：仅辅助，忽略
            mode = "cn"
            continue
        # 非字段头的普通行：按当前 mode 归属
        if mode == "review":
            review_words.extend(_extract_words_from_line(s))
        elif mode in ("use", "curr"):
            new_words.extend(_extract_words_from_line(s))
        elif mode == "desc":
            scene_text.append(s)
    return new_words, review_words, " ".join(scene_text).strip(), tier


def _extract_large_scenes(text):
    """抽走文本里所有 场景 N｜ / 大场景 N｜ 块，返回 (scenes, cleaned_text)。

    scenes: [{"tier": "large"/"small", "words": [...], "text": "..."}, ...]
    cleaned_text: 去掉这些块后的正文，交给主解析器（不干扰每词 Mini Scenario）。
    找不到块则原样返回。
    """
    spans = [m.start() for m in _SCENE_TITLE_RE.finditer(text)]
    if not spans:
        return [], text
    spans.append(len(text))
    scenes = []
    parts = []
    prev = 0
    for i in range(len(spans) - 1):
        start, end = spans[i], spans[i + 1]
        block = text[start:end]
        new_words, review_words, scene_text, tier = _parse_large_scene_block(block)
        if (new_words or review_words) and scene_text:
            scenes.append({"tier": tier, "words": new_words + review_words, "text": scene_text})
        parts.append(text[prev:start])   # 块之前的文本保留
        prev = end
    parts.append(text[prev:])
    return scenes, "".join(parts)


# ----------------------------------------------------------------------------
# 「按天场景计划」解析（用户新格式：# 第N天｜… + 小场景/大场景 + 使用单词/复习单词/当天单词）
# ----------------------------------------------------------------------------
# 这份格式顶层是「天」不是「周」，且只有单词列表 + 场景说明（没有 word—释义 词形），
# 所以需要专门的解析：把每个「第N天」下的 使用单词/当天单词/复习单词 收成当天词汇表，
# 把每个 小场景/大场景 收成 word_scenarios（tier 由 大/小 前缀判定）。
# 周号缺失 → 调用方用 forced_week（前端传当前周）兜底。
_DAY_RE = re.compile(r"第\s*([0-9０-９]{1,2})\s*天")


def _is_day_scenario_plan(text):
    """判断是否为『按天场景计划』新格式：含 第N天 天头，且含 使用单词/当天单词/复习单词 任一词。

    与旧『场景 N｜使用单词』（带 第N周 周头、块状 word—释义）区分开：旧格式没有
    第N天、且通常没有 使用单词/当天单词/复习单词 这种小场景字段，会走原 parse_import 路径。
    """
    if not text:
        return False
    if not re.search(r"第\s*[0-9０-９]+\s*天", text):
        return False
    if not ("使用单词" in text or "当天单词" in text or "复习单词" in text):
        return False
    return True


def _parse_day_scenario_plan(text):
    """解析『# 第N天｜…』按天场景计划。返回与 parse_import 兼容的结构。

    每个词 meaning/pos 留空（由 weekimport 查词库补全；词库没有则自动补占位句）。
    大场景的「复习单词」标 review=True（前端组合页显示 🔁），但仍进入当天词汇表。
    返回 None 表示没解析出任何天（调用方回退到普通解析，不直接失败）。
    """
    text = _normalize_text(text)
    groups = {}          # day -> {"day","name","words":[entries]}
    day_seen = {}        # day -> set(小写词) 组内去重
    day_new = {}         # day -> set(小写新词)
    day_review = {}      # day -> set(小写复习词)
    day_name = {}
    large_scenes = []
    skipped = []
    cur_day = None
    cur_block = None     # {"lines":[...], "day":int}

    def ensure_group(day, name=None):
        g = groups.setdefault(day, {"day": day, "name": "", "words": []})
        if name:
            g["name"] = name
        return g

    def close_block():
        nonlocal cur_block, cur_day
        if cur_block is None:
            return
        blk = "\n".join(cur_block["lines"])
        d = cur_block.get("day") or cur_day or 1
        new_words, review_words, scene_text, tier = _parse_large_scene_block(blk)
        if (new_words or review_words) and scene_text:
            ensure_group(d, day_name.get(d))
            # 仅「大场景」进 word_scenarios（tier='large'，供组合表达页取景）；
            # 「小场景」是练当天 20 词用的，说明只挂 vocab.scenes 给基础句显示，不进
            # word_scenarios，避免组合句页混入小场景、与「大场景=综合复习」的设计分层混淆。
            if tier != "small":
                large_scenes.append({"tier": tier, "words": new_words + review_words,
                                     "text": scene_text, "day": d})
            s = day_seen.setdefault(d, set())
            day_new.setdefault(d, set()).update(new_words)
            day_review.setdefault(d, set()).update(review_words)
            # 小场景：把「场景说明」挂到「使用单词」的 vocab.scenes（Mini Scenario），
            # 这样基础句（20 词造句）本地就能显示该小场景，点 🔁 在多小场景间轮换。
            # 大场景是综合复习，说明只留给 word_scenarios（组合句用），不进 vocab.scenes。
            small_word_scenes = {w.lower(): scene_text for w in new_words} if tier == "small" else {}
            for w in (new_words + review_words):
                wl = w.lower()
                if wl in s:
                    # 已存在：小场景则把本场景追加进已有词的 scenes（跨小场景累积）
                    if tier == "small" and wl in small_word_scenes:
                        ex = next((e for e in ensure_group(d)["words"] if e["word"].lower() == wl), None)
                        if ex is not None:
                            ex["scenes"].append({"text": scene_text})
                    continue
                s.add(wl)
                review = (wl in day_review[d]) and (wl not in day_new[d])
                scenes = [{"text": scene_text}] if (tier == "small" and wl in small_word_scenes) else []
                ensure_group(d)["words"].append({
                    "word": w, "meaning": "", "pos": "",
                    "examples": [], "collocations": [], "scenes": scenes,
                    "review": review,
                })
        else:
            skipped.append(blk)
        cur_block = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 天头：# 第N天｜名称（别误伤场景块里的 第N天；长中文正文也不会带 第N天）
        dm = _DAY_RE.search(line)
        if dm and (line.startswith("#") or len(line) <= 30) and not _SCENE_TITLE_RE.search(line):
            close_block()
            cur_day = int(dm.group(1))
            name = line[dm.end():].lstrip("｜|").strip()
            day_name[cur_day] = name
            continue
        # 场景标题：### 小场景N｜… / ### 大场景N｜… / 小场景N｜…
        if _SCENE_TITLE_RE.search(line):
            close_block()
            cur_block = {"lines": [line], "day": cur_day or 1}
            continue
        # 其它 # 标题（## ① 今日10个小场景 / ## ③ 今日综合复习｜5个大场景）→ 收块忽略
        if line.startswith("#"):
            close_block()
            continue
        if cur_block is not None:
            cur_block["lines"].append(line)
            continue
        skipped.append(raw)

    close_block()
    if not groups:
        return None
    first_day = min(groups)
    title = day_name.get(first_day, "")
    parsed = {"stage": None, "week": None, "title": title, "grammar": "",
              "groups": groups, "flat": None, "skipped": skipped, "header_lines": []}
    groups_list, flat, warnings = _finalize(parsed)
    return {
        "stage": None, "week": None, "title": title, "grammar": "",
        "groups": groups_list, "flat": flat, "warnings": warnings,
        "skipped": skipped, "header_lines": [],
        "large_scenes": large_scenes,
    }


# ----------------------------------------------------------------------------
# 「单词框」内联解析（前端导入弹窗左侧框专用）
# 支持一行一词、例句用 ● 分隔、固定搭配用「」：
#   1. currently /ˈkɜːrəntli/ — 目前，现在
#      ●I currently live in Shanghai. 我目前住在上海。
#      ●I currently work for a company. 我目前在一家公司工作。
#      固定搭配：「currently live」目前居住；「currently work」目前工作
# （● 可写在同一行，也可换行；本解析器两种都认）
# ----------------------------------------------------------------------------
def _split_en_cn(seg):
    """把一个『英文. 中文』片段拆成 (英文, 中文)。没有中文就整段当英文。"""
    seg = (seg or "").strip()
    if not seg:
        return "", ""
    m = re.search(r"[\u4e00-\u9fff]", seg)
    if not m:
        return seg, ""
    en = seg[:m.start()].strip()
    cn = seg[m.start():].strip()
    return en, cn


_DOTTED_NUM_RE = re.compile(
    r"^\s*(?:[0-9０-９]{1,3}\s*[.、)）]|[•·▪◦‣]|[\u2460-\u2473])\s*")


def _parse_inline_word_line(line):
    """解析『单词框』里的单行词（含 ● 内联例句 + 固定搭配）。

    返回 word 条目 {"word","meaning","phonetic","examples","collocations"}，
    或 None（不是单词行）。无分隔符的旧格式回退到通用 _parse_word_header。
    """
    s = line.strip()
    if not s or len(s) > 600:
        return None
    # 去掉行首 Markdown 标题前缀（### 1. word → 1. word）
    s = _strip_md_hash(s)
    # 去行首序号 "1." "15)" "2、" "①"
    s = _DOTTED_NUM_RE.sub("", s)
    # 分隔符：优先 — ，其次 – / －
    sep = None
    for sp in ("—", "–", "－"):
        if sp in s:
            sep = sp
            break
    if sep:
        left, right = s.split(sep, 1)
    else:
        # 无分隔符：回退到通用单词头解析（旧格式，不含内联例句）
        return _parse_word_header(s)
    # 抽取音标（在左侧单词与中文释义之间）
    phonetic = ""
    m_ipa = _IPA_SEG_RE.search(left)
    if m_ipa:
        phonetic = m_ipa.group(0).strip().strip("[]").strip()
        left = _IPA_SEG_RE.sub(" ", left).strip()
    mw = re.match(r"^(" + _WORD_CHARS + r")", left)
    if not mw or not _is_english_word(mw.group(1)):
        # 左侧不是干净单词（整行是例句/说明）→ 放弃
        return None
    word = mw.group(1)
    # right: 释义 ●En. 中 ●En. 中 固定搭配：...
    segs = [x.strip() for x in re.split(r"●", right) if x.strip()]
    meaning = segs[0] if segs else ""
    examples = []
    collocations = []
    for seg in segs[1:]:
        # 段内若带「固定搭配：」，其前是例句、其后是搭配
        mfc = re.search(r"(?:固定)?搭配\s*[:：]", seg)
        if mfc:
            before = seg[:mfc.start()].strip()
            after = seg[mfc.end():].strip()
            if before:
                en, cn = _split_en_cn(before)
                examples.append({"sentence": en, "translation": cn})
            if after:
                collocations.extend(_parse_colloc_line("固定搭配：" + after) or [])
        else:
            en, cn = _split_en_cn(seg)
            examples.append({"sentence": en, "translation": cn})
    return {"word": word, "meaning": meaning, "phonetic": phonetic, "pos": "",
            "examples": examples, "collocations": collocations, "scenes": []}


def parse_words_block(text):
    """解析『单词框』内容：每个词一行（支持 ● 内联例句 + 固定搭配）。

    头行支持：第N周｜主题｜N词 / 第N组｜名 / 第N天｜名 / Day N｜名。
    返回与 parse_import 兼容的 {week,title,stage,groups,flat,warnings,skipped}。
    """
    text = _normalize_text(text)
    week_ref = [None]
    title_ref = [""]
    group_ref = [1, ""]
    stage_ref = [None]
    groups = {}
    skipped = []
    cur_word = None

    def ensure_group(day, name=None):
        g = groups.setdefault(day, {"day": day, "name": "", "words": []})
        if name:
            g["name"] = name
        return g

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 跳过 Markdown 分隔线 / 旧式分块线
        if re.fullmatch(r"[-–—=_]{2,}", line):
            continue
        sm = _STAGE_RE.search(line)
        if sm and len(line) <= 30 and not re.search(r"[。！？.!?]$", line):
            st = _to_int(sm.group(1) or sm.group(2))
            if st is not None:
                stage_ref[0] = st
                continue
        kind = _read_headers(line, week_ref, title_ref, group_ref)
        if kind == "week":
            continue
        if kind == "group":
            ensure_group(group_ref[0], group_ref[1])
            cur_word = None
            continue
        w = _parse_inline_word_line(line)
        if w and _is_english_word(w["word"]):
            w.setdefault("examples", [])
            w.setdefault("collocations", [])
            w.setdefault("scenes", [])
            ensure_group(group_ref[0], group_ref[1])["words"].append(w)
            cur_word = w
            continue
        if cur_word is not None and _is_eng_sentence(line):
            cur_word["examples"].append(
                {"sentence": _strip_list_marker(line), "translation": ""})
            continue
        colloc = _parse_colloc_line(line)
        if colloc is not None and cur_word is not None:
            cur_word["collocations"].extend(colloc)
            continue
        if cur_word is not None and _is_cn_only(line):
            for ex in reversed(cur_word["examples"]):
                if not ex["translation"]:
                    ex["translation"] = line
                    break
            else:
                skipped.append(raw)
            continue
        skipped.append(raw)
    groups_list, flat, warnings = _finalize({"groups": groups})
    return {"stage": stage_ref[0], "week": week_ref[0], "title": title_ref[0],
            "grammar": "", "groups": groups_list, "flat": flat,
            "warnings": warnings, "skipped": skipped}


def parse_import(text):
    """主解析入口。返回结构见文件头。"""
    text = _normalize_text(text)
    # 按天场景计划（# 第N天｜… + 小场景/大场景 + 使用单词/复习单词/当天单词）：
    # 这种格式没有 word—释义 词形、没有周号，必须走专属解析，否则会落得
    # "没有识别到任何单词，也没有可导入的场景"。周号由调用方 forced_week 兜底。
    if _is_day_scenario_plan(text):
        res = _parse_day_scenario_plan(text)
        if res is not None:
            return res
    # 先把「Scene N｜tier」大/中/小场景块抽走（这些来自导入，不归每词 Mini Scenario）：
    large_scenes, text = _extract_large_scenes(text)
    if _looks_like_block(text):
        parsed = _parse_block(text)
    else:
        parsed = _parse_line(text)
    groups, flat, warnings = _finalize(parsed)
    return {
        "stage": parsed["stage"], "week": parsed["week"], "title": parsed["title"],
        # stage_from_text: 文本里明确写了"阶段N"才有值（None 表示没写），
        # 调用方据此决定是沿用该值，还是回退到传入值/当前进度。
        "stage_from_text": parsed.get("stage_from_text"),
        "grammar": parsed["grammar"], "groups": groups, "flat": flat,
        "warnings": warnings, "skipped": parsed["skipped"],
        "header_lines": parsed["header_lines"],
        # 导入带来的大/中/小场景（运行时系统不再调 AI 生成，见 scenario.AI_TIERS=()）
        "large_scenes": large_scenes,
    }
