"""句子本地规则纠错服务（纯本地，无 AI / 无 API）。

本系统明确不接入任何 AI/LLM。用户造句后，用本地规则引擎找出并纠正
常见错误（固定搭配、主谓一致、时态、冠词、介词等），返回结构化结果。
批改结果：错误类型 + 修正句 + 解释 + 是否入错误库 + 是否需复习。
"""
import re
from datetime import date

from db import get_conn, ts
from srs import schedule_review

ERROR_TYPES = [
    "冠词", "介词", "时态", "主谓一致", "单复数", "词序",
    "固定搭配", "词性", "拼写", "句型", "其他",
]

# 常见动词过去式/过去分词
PAST = {
    "go": "went", "eat": "ate", "watch": "watched", "play": "played",
    "like": "liked", "enjoy": "enjoyed", "work": "worked", "have": "had",
    "do": "did", "get": "got", "make": "made", "see": "saw",
    "read": "read", "swim": "swam", "drive": "drove", "buy": "bought",
    "study": "studied", "finish": "finished", "start": "started",
    "travel": "traveled", "cook": "cooked", "love": "loved",
}
_VERB_STEMS = set(PAST.keys()) | {
    "think", "know", "want", "need", "learn", "help", "live", "run",
    "walk", "listen", "talk", "call", "help",
}

# 固定搭配规则：把常见的错误简写纠正。 (正则, 正确写法, 中文说明)
COLLOC_RULES = [
    (r"\bgo\s+work\b", "go to work", "go to work 是固定搭配"),
    (r"\bgo\s+school\b", "go to school", "go to school 是固定搭配"),
    (r"\bgo\s+bed\b", "go to bed", "go to bed 是固定搭配"),
    (r"\blike\s+do\b", "like doing", "like 后接 doing"),
    (r"\benjoy\s+to\s+doing\b", "enjoy doing", "enjoy 后接 doing"),
    (r"\blike\s+swim\b", "like swimming", "like 后接 doing"),
    (r"\benjoy\s+swim\b", "enjoy swimming", "enjoy 后接 doing"),
    (r"\bhate\s+do\b", "hate doing", "hate 后接 doing"),
    (r"\bgo\s+swimming\b", "go swimming", "go swimming 是固定搭配"),
    (r"\bplay\s+basketball\s+game\b", "play basketball", "play 后接运动名"),
    (r"\benjoy\s+work\b", "enjoy working", "enjoy 后接 doing"),
    (r"\bfinish\s+do\b", "finish doing", "finish 后接 doing"),
    (r"\bwant\s+doing\b", "want to do", "want 后接 to do"),
    (r"\bneed\s+doing\b", "need to do", "need 后接 to do"),
    (r"\blearn\s+english\b", "learn English", "English 应大写"),
    (r"\bmake\s+friend\b", "make friends", "make friends 中 friends 用复数"),
]

# 第三人称单数变形
def _stem(v):
    if v.endswith("es"):
        return v[:-2]
    if v.endswith("s") and not v.endswith("ss") and not v.endswith("is"):
        return v[:-1]
    return v

def _third_singular(v):
    base = _stem(v)
    if base.endswith(("s", "x", "z", "ch", "sh", "o")):
        return base + "es"
    return base + "s"

def _is_verb(v):
    return _stem(v) in _VERB_STEMS


def _rule_correct(sentence):
    """本地规则纠错。返回 {'correct':bool,'corrected':str,'errors':[...]} 或 None（无明确错误）。"""
    s = sentence.strip()
    if not s:
        return None
    lowered = " " + s.lower() + " "
    errors = []

    # 1) 固定搭配/拼写类规则（含大小写修正占位，实际替换时按原句词性处理）
    #    这里先用 lower 匹配，替换时按原句精确替换片段。
    for pat, fix, expl in COLLOC_RULES:
        m = re.search(pat, lowered)
        if m:
            # 在原文找到该片段做替换
            frag = m.group(0).strip()
            # 找到原文对应位置
            idx = s.lower().find(frag)
            if idx >= 0:
                orig_frag = s[idx:idx + len(frag)]
                errors.append({
                    "type": "固定搭配", "original": orig_frag, "correct": fix,
                    "explanation": expl,
                })

    # 1.5) 一般 V+doing 规则：like/enjoy/hate/finish/mind/love + 动作动词(原形) → doing
    for m in re.finditer(r"\b(like|enjoy|hate|finish|mind|love|keep)\s+(swim|read|cook|play|watch|listen|run|walk|dance|sing|draw|travel|shop|eat|drive|study|work|write|learn|make|help)\b", lowered):
        head, vb = m.group(1), m.group(2)
        # 排除已是进行/被动态结构 "like watching" 等（前面动词已是ing则不命中，因为正则要求裸动词）
        errors.append({
            "type": "固定搭配",
            "original": f"{head} {vb}",
            "correct": f"{head} {_ing_form(vb)}",
            "explanation": f"{head} 后接动词要用 doing 形式（{head} {_ing_form(vb)}）。",
        })

    # 2) 主谓一致：he/she/it/人名 + 一般现在时动词（无s）
    #    若有明确过去时间(yesterday/ago)或将来时间(tomorrow/next)，主谓一致的"-s"不是首要问题，
    #    交给后面的时态规则处理，避免把"I went"误改回三单。
    has_past_marker = bool(re.search(r"\b(yesterday|ago|last\s+\w+)\b", lowered))
    has_future_marker = bool(re.search(r"\b(tomorrow|tonight|next\s+\w+)\b", lowered))
    for m in re.finditer(r"\b(he|she|it|tom|mary|my mother|my father|my sister|my brother)\s+([a-z]+)\b", lowered):
        verb = m.group(2)
        stem = _stem(verb)
        if has_past_marker or has_future_marker:
            continue
        if _is_verb(verb) and verb == stem and not verb.endswith("s") and stem in PAST or (stem in _VERB_STEMS and verb not in ("has", "is", "does", "was")):
            if verb in ("am", "is", "are", "has", "does", "was", "were"):
                continue
            errors.append({
                "type": "主谓一致",
                "original": f"{m.group(1)} {verb}",
                "correct": f"{m.group(1)} {_third_singular(verb)}",
                "explanation": f"{m.group(1)} 是第三人称单数，一般现在时动词要加 -s/-es。",
            })

    # 3) 时态：yesterday 用过去式；tomorrow/will 用将来
    if re.search(r"\byesterday\b", lowered):
        for m in re.finditer(r"\b(i|he|she|we|they|tom|mary)\s+(go|eat|watch|play|like|enjoy|work|study|finish|start|travel|cook|buy|see|swim|drive|have|make|do|get|read)\b", lowered):
            verb = m.group(2)
            if verb in PAST:
                errors.append({
                    "type": "时态", "original": f"{m.group(1)} {verb}",
                    "correct": f"{m.group(1)} {PAST[verb]}",
                    "explanation": f"有 yesterday（过去时间），动词 {verb} 应改为过去式 {PAST[verb]}。",
                })

    # 4) 简单冠词：a apple -> an apple 等（基础）
    for m in re.finditer(r"\ba\s+([aeiou][a-z]*)\b", lowered):
        # 排除 a/an 之后确实独立的可数词，仅当后接的是名词性元音词
        if m.group(1) in ("and", "or", "about", "after", "always", "also", "around", "up", "our", "on"):
            continue
        errors.append({
            "type": "冠词", "original": m.group(0).strip(),
            "correct": "an " + m.group(1),
            "explanation": f"{m.group(1)} 是元音开头，前面要用 an 而不是 a。",
        })

    # 5) 单复数：可数单数名词直接跟在数词/ many/ these / those 之后应加复数（常用词集合）
    #    用启发式：名词出现在句首 of 主谓后的表意位置很难可靠判断，只处理明确列表词。
    _PLURALIZE_IF_BARE = {
        # 词 -> (确定性触发词列表：其后可数名词必为复数)
        "friend": ["many", "these", "those", "two", "three", "several"],
        "book": ["many", "these", "those", "two", "three", "several"],
        "apple": ["many", "two", "three", "some"],
        "egg": ["many", "two", "three", "some"],
        "pen": ["many", "two", "three", "some"],
        "student": ["many", "these", "those", "two", "three", "several"],
        "day": ["many", "two", "three", "several", "seven"],
        "child": [], "man": [], "woman": [],  # 不规则变形走固定处理
    }
    for bare, triggers in _PLURALIZE_IF_BARE.items():
        for tg in triggers:
            for m in re.finditer(rf"\b{tg}\s+{bare}\b", lowered):
                errors.append({
                    "type": "单复数", "original": m.group(0).strip(),
                    "correct": f"{tg} {_plural_of(bare)}",
                    "explanation": f"{tg} 表示多个，{bare} 要用复数 {_plural_of(bare)}。",
                })

    # 6) 介词：基础搭配纠正（at the morning->in the morning 等）
    #    (正则, 修正短语, 中文说明)。fix 需是完整可替换短语。
    PREP_RULES = [
        (r"\bat\s+the\s+morning\b", "in the morning", "morning 前用 in the（in the morning）"),
        (r"\bat\s+the\s+afternoon\b", "in the afternoon", "afternoon 前用 in the（in the afternoon）"),
        (r"\bat\s+the\s+evening\b", "in the evening", "evening 前用 in the（in the evening）"),
        (r"\b(in|on)\s+beijing\b", "in Beijing", "城市名 Beijing 前用 in"),
        (r"\blisten\s+music\b", "listen to music", "listen 后接 to（listen to music）"),
        (r"\bgood\s+in\s+english\b", "good at English", "擅长英语用 be good at English"),
        (r"\barrive\s+to\b", "arrive at", "到达地点用 arrive at/in（arrive to 是错的）"),
        (r"\bgo\s+to\s+home\b", "go home", "go home 不用 to"),
        (r"\bget\s+to\s+home\b", "get home", "get home 不用 to"),
        (r"\benjoy\s+at\s+doing\b", "enjoy doing", "enjoy 后直接接 doing，不加 at"),
        (r"\bgood\s+with\s+english\b", "good at English", "擅长英语用 good at English"),
    ]
    for pat, fix, expl in PREP_RULES:
        m = re.search(pat, lowered)
        if m:
            frag = m.group(0).strip()
            # fix 中若含 {time}/{n} 占位则替换为原文中的对应数字
            actual = fix
            if "{time}" in actual or "{n}" in actual:
                dig = re.search(r"\d+", frag)
                actual = actual.replace("{time}", dig.group(0)).replace("{n}", dig.group(0)) if dig else actual
            idx = s.lower().find(frag)
            if idx >= 0:
                orig_frag = s[idx:idx + len(frag)]
                errors.append({
                    "type": "介词", "original": orig_frag, "correct": actual,
                    "explanation": expl,
                })

    # 7) 将来时：tomorrow / next 里出现一般现在/过去式裸动词 → 改成 will + do
    if re.search(r"\b(tomorrow|tonight|next\s+\w+)\b", lowered):
        for m in re.finditer(
                r"\b(i|he|she|we|they|tom|mary|you)\s+(go|eat|watch|play|like|enjoy|work|study|finish|start|travel|cook|buy|see|swim|drive|have|make|do|get|read)\b",
                lowered):
            verb = m.group(2)
            if _is_verb(verb):
                errors.append({
                    "type": "时态", "original": f"{m.group(1)} {verb}",
                    "correct": f"{m.group(1)} will {verb}",
                    "explanation": f"有 tomorrow/next（将来时间），可用 will + 动词原形 {verb} 表达计划。",
                })

    if not errors:
        return None
    # 先按 (original, correct) 去重，避免同一条目(如COLLOC与通用规则都命中)被重复替换导致叠加
    seen_pair = set()
    _errors = []
    for e in errors:
        k = (e["original"], e["correct"])
        if k in seen_pair:
            continue
        seen_pair.add(k)
        _errors.append(e)
    errors = _errors
    # 生成修正句：按在原句中的位置从右到左替换，避免前面替换后影响后面片段的定位
    # 先记录每个错误在原句中的位置与要替换的片段
    candidates = []
    for e in errors:
        orig = e["original"]
        if not orig or orig == e["correct"]:
            continue
        idx = s.lower().find(orig.lower())
        if idx < 0:
            continue  # 该片段未被识别到，跳过
        fix = e["correct"]
        # 若被替换片段位于句首且原句此处为大写开头，则修正词首字母也大写
        if idx == 0 and s[:1].isupper() and fix and fix[0].islower():
            fix = fix[0].upper() + fix[1:]
        candidates.append((idx, orig, fix))
    # 去重同位置同长度的重叠替换冲突：按位置升序保留首次，移除被覆盖的同起点重复
    # 从右往左应用，避免索引漂移
    corrected = s
    for idx, orig, fix in sorted(candidates, key=lambda c: -c[0]):
        corrected = corrected[:idx] + fix + corrected[idx + len(orig):]
    # 已成功应用、且 original != correct 的错误作为最终返回（便于前端展示解释）
    applied = [e for e in errors if e["original"] and e["original"] != e["correct"]]
    # 去重错误（按 original 去重，保留首次出现）
    seen = set()
    uniq = []
    for e in applied:
        if e["original"] not in seen:
            seen.add(e["original"])
            uniq.append(e)
    if not uniq:
        return None
    return {"correct": False, "corrected": corrected, "errors": uniq}


# 简单名词复数（覆盖基础不规则）
_PLURAL_IRREG = {"child": "children", "man": "men", "woman": "women",
                 "foot": "feet", "tooth": "teeth", "person": "people"}


def _plural_of(noun):
    if noun in _PLURAL_IRREG:
        return _PLURAL_IRREG[noun]
    if noun.endswith(("s", "x", "z", "ch", "sh")):
        return noun + "es"
    if noun.endswith("y") and not noun.endswith(("ay", "ey", "oy", "uy")):
        return noun[:-1] + "ies"
    if noun.endswith("f"):
        return noun[:-1] + "ves"
    return noun + "s"


def _ing_form(v):
    """动词转进行时/动名词形式（基础规则 + 常见需双写尾辅音的动词白名单）。"""
    if v.endswith("ie"):
        return v[:-2] + "ying"
    if v.endswith("e") and not v.endswith(("ee", "ye", "oe")):
        return v[:-1] + "ing"
    _DOUBLE_LAST = {"swim", "run", "get", "sit", "shop", "stop", "begin",
                    "plan", "drop", "put", "cut", "win", "dig", "jog", "chat",
                    "let", "set", "fit", "hit", "nod", "rub"}
    if v in _DOUBLE_LAST:
        return v + v[-1] + "ing"
    return v + "ing"


def _simple_expand(sentence):
    """对过于简单的句子给出扩展示例（不判错、不修改原句，仅作友好提示）。"""
    s = sentence.strip().rstrip(".!?")
    low = s.lower()
    words = low.split()
    if len(words) <= 1:
        return "句子太短了，试着加上你是谁、做什么或喜欢什么。"
    # 模式1：I am / He is / She is / My name is ... 等自我介绍
    m = re.match(r"^(i|he|she|my name|his name|her name)\s+(am|is|are)\s+(.+)$", s, re.I)
    if m:
        name = m.group(3).strip()
        return (
            f"你可以写成：I'm {name}, and I'm a student / I work as a ... "
            "再补一句你来自哪里或喜欢什么，会让句子更丰富。"
        )
    # 模式2：My name is ...
    m = re.match(r"^my name is\s+(.+)$", s, re.I)
    if m:
        name = m.group(1).strip()
        return (
            f"你可以写成：My name is {name}. I'm from ... and I like ... "
            "（加上身份、地点或爱好）"
        )
    # 模式3：主语 + be + 表语（不超过5词且无标点）
    if len(words) <= 5 and re.match(r"^[a-z]+\s+(am|is|are)\s+[a-z]+\s*$", low):
        return "可以补充 because / and / but / usually / every day 等，把一句话拉长成两句。"
    # 模式4：极短陈述句（<=5词且无连词/标点）
    if len(words) <= 5 and not any(p in sentence for p in ",.;:!"):
        return "试试用 and / because / so 把两个短信息连起来，例如 I like apples because they are sweet."
    return None


def correct_sentence(sentence, stage=0, week=3, day=1):
    """纯本地批改主入口：返回统一结构，并把错误写入错误库、安排复习。无任何 AI。"""
    expand_hint = _simple_expand(sentence)
    result = _rule_correct(sentence)
    if result is None:
        result = {"correct": True, "corrected": sentence, "errors": []}
    source = "rule"

    good = bool(result.get("correct"))
    errors = result.get("errors") or []
    primary_type = errors[0]["type"] if errors else ""
    explanation = "；".join(e["explanation"] for e in errors) if errors else ""

    # 持久化到 sentences 表
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO sentences (stage, week, day, original, corrected, error_type,"
        " explanation, ai_source, good, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (stage, week, day, sentence, result["corrected"], primary_type,
         explanation, source, 1 if good else 0, ts()))
    sentence_id = cur.lastrowid

    # 错误写入错误库
    need_review = False
    for e in errors:
        conn.execute(
            "INSERT INTO errors (error_type, original, corrected, explanation,"
            " source, created_at) VALUES (?,?,?,?,?,?)",
            (e["type"], e.get("original", ""), e.get("correct", ""),
             e.get("explanation", ""), "sentence", ts()))
        need_review = True

    # 为错误类型安排一张"高频错误"复习卡
    if need_review:
        schedule_review(conn, "error", primary_type,
                        f"改正错误：{result['corrected']}",
                        result["corrected"], stage, week, day)

    conn.commit()
    conn.close()

    return {
        "sentence_id": sentence_id,
        "original": sentence,
        "corrected": result["corrected"],
        "good": good,
        "error_type": primary_type,
        "explanation": explanation,
        "errors": errors,
        "ai_source": source,
        "needs_review": need_review,
        "to_error_bank": need_review,
        "expand_hint": expand_hint,
    }

