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
            "we", "they", "you", "this", "that", "these", "those", "goal", "problem",
            "group", "week", "day", "stage", "vocabulary", "english", "第", "第组"}


def _is_english_word(tok):
    if not tok:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", tok):
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
# 行首列表标记（-、•、·等；不含 */★，它们是重点词标记）
_LIST_MARK_RE = re.compile(r"^[\s]*[-–—•·▪◦‣▪]+\s+")


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
    # 去掉行首序号 "1." "15." "40)" "2、" 及列表标记 "•" "①" 等
    s = re.sub(r"^\s*(?:[0-9０-９]{1,3}\s*[.、)）]|[•·▪◦‣]|[\u2460-\u2473])\s*", "", s)
    for sep in ("—", "–", "－", "："):
        if sep in s:
            left, right = s.split(sep, 1)
            return _build(left, right)
    m = re.match(r"^([A-Za-z][A-Za-z'\-]*)(?:[\s　]+(.+))?$", s)
    if m and _is_english_word(m.group(1)):
        rest = (m.group(2) or "").strip()
        focus = False
        if "★" in rest or "*" in rest:
            focus = True
            rest = re.sub(r"[★*]", "", rest).strip()
        pos, cn = _split_pos_and_cn(rest) if rest else ("", "")
        if rest and not cn:
            cn = rest
        return {"word": m.group(1), "meaning": cn or "", "pos": pos, "focus": focus}
    return None


def _is_word_header_line(line):
    """单词头通常是"短行"，且不是一句英文例句。用于避免把英文例句首词误当单词。"""
    s = line.strip()
    if not s or len(s) > 40:
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
    # 剥离左侧的 IPA 音标。原正则 ^([A-Za-z][A-Za-z'\-]*)...$ 要求 left
    # 只能是纯单词，"continue /kənˈtɪnjuː/" 拖着音标 → 不匹配 → 返回 None
    # → 整行被丢进 skipped → 一个词都导不进去。
    left = _IPA_SEG_RE.sub(" ", left).strip()
    # ★ 重点词标记：用户在单词行打 ★，表示"这个词要重点升级"
    focus = False
    if "★" in left or "★" in right or "*" in left or "*" in right:
        focus = True
        left = re.sub(r"[★*]", "", left).strip()
        right = re.sub(r"[★*]", "", right).strip()
    m = re.match(r"^([A-Za-z][A-Za-z'\-]*)(?:[\s　]*\(([^)]*)\))?$", left)
    if not m or not _is_english_word(m.group(1)):
        return None
    pos, cn = _split_pos_and_cn(right)
    return {"word": m.group(1), "meaning": cn or "", "pos": pos, "focus": focus}


# ---------------- 周/组信息提取（通用） ----------------
def _read_headers(line, week_ref, title_ref, group_ref):
    """若行为周/组标题则更新并返回 (kind, info)。kind: 'week'|'group'|None。

    支持写法（大小写不敏感）：
      第2周｜工作与日常｜120词   /  Week 2｜工作与日常
      第1组｜职场人物             /  Day 1｜第一天上班   /  第1天｜…
    以句末标点结尾的行视为句子而非标题（防止 "Day 1 was my first day." 误判）。
    """
    s = line.strip()
    if s and s[-1] in ".!?，。！？；;":
        return None
    wm = _WEEK_RE.search(s)
    if wm and len(s) <= 40:
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
    if gm and len(s) <= 60:
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
        # 英文例句 → 新开一条例句（中文随后配对）【须先于单词头判断，
        #   否则 "I started working..." 会被误认成单词 "I"】
        if _is_eng_sentence(line):
            if cur_word is not None:
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
                ensure_group(group_ref[0], group_ref[1])["words"].append(w)
                cur_word = w
                pending_ex = None
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

    def ensure_group(day, name=None):
        g = groups.setdefault(day, {"day": day, "name": "", "words": []})
        if name:
            g["name"] = name
        return g

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
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
        w = _parse_word_header(line)
        if w and _is_english_word(w["word"]):
            w.setdefault("examples", [])
            w.setdefault("collocations", [])
            ensure_group(group_ref[0], group_ref[1])["words"].append(w)
            cur_word = w
            pending_ex = None
            continue
        if cur_word is not None and _is_eng_sentence(line):
            ex = {"sentence": _strip_list_marker(line), "translation": ""}
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


def parse_import(text):
    """主解析入口。返回结构见文件头。"""
    text = _normalize_text(text)
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
    }
