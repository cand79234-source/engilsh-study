"""句子本地规则批改服务（纯本地，无 AI / 无 LLM / 无第三方 API）。

本系统明确不接入任何 AI/LLM。用户造句后，用本地规则引擎找出并纠正常见错误，
返回：是否正确 / 分数 / 哪里错了 / 为什么错 / 正确表达 / 可优化的表达。

批改结果同时写入：
  - sentences：每次作答都追加一行（重新作答不会覆盖上一次）
  - errors   ：只有「真错」才写入（正确句最多给优化建议，绝不进错题本）

评分规则（>=85 基本掌握，<85 需要改进）：
  - 没错：90 分起，写得够丰富最多加到 100 —— 简单但正确的句子**绝不低分**
  - 有错：90 - 各错误扣分（重度 30 / 中度 20 / 轻微 12），下限 20
  这样「有任何错误」一定 <85，「没错」一定 >=85，「优化建议」完全不影响分数。
"""
import json
import re
from datetime import datetime, timedelta

from db import get_conn, ts, insert_get_id
from srs import schedule_review

ERROR_TYPES = [
    "冠词", "介词", "时态", "主谓一致", "单复数", "词序",
    "固定搭配", "词性", "拼写", "句型", "其他",
]

PASS_LINE = 85          # >=85 基本掌握
BASE_SCORE = 90         # 无错基准分
PENALTY = {"heavy": 30, "medium": 20, "light": 12}
MIN_SCORE = 20

# ---- 错题分级：单错 ≠ 薄弱项 ----
LEVEL_BLOCK = "🔴"    # 阻塞项：必须调整学习安排（由诊断侧标记，不在此自动升级）
LEVEL_WEAK = "🟡"     # 薄弱项：近30天同一个错出现 ≥2 次
LEVEL_MEMORY = "🔵"   # 记忆项：偶发/单次，交给闪卡解决，不改课程


def norm_error_text(text):
    """归一化错误片段：压缩空白 + 转小写。

    只用于「这是不是同一个错」的合并判定，入库仍保留学习者原样写法。
    例："I  like" 与 "i like" 视作同一个错，只累加次数不重复建条目。
    """
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def days_ago_str(n):
    """n 天前的 ISO 串，可直接与 created_at 做字符串比较
    （created_at 为 ISO 格式，字典序即时间序，SQLite 与 PG 通用）。"""
    return (datetime.now() - timedelta(days=n)).isoformat(timespec="seconds")


def recent_error_count(conn, word, etype, norm, since):
    """近 since 之后，这个 (词 + 错误类型 + 归一化片段) 一共出现过几次。

    数据取自 sentences.errors_json：每次作答一行，能真实反映「近30天犯了几次」；
    errors 表已按 (词+类型+归一化) 合并成一行，从它自身看不出时间分布。
    取不到数据时保守返回 1（不晋级）。
    """
    n = 0
    try:
        rows = conn.execute(
            "SELECT errors_json FROM sentences WHERE word=? AND created_at>=?",
            (word, since)).fetchall()
    except Exception:
        return 1
    for r in rows:
        try:
            items = json.loads(r["errors_json"] or "[]")
        except Exception:
            continue
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if (it.get("type") == etype
                    and norm_error_text(it.get("where", "")) == norm):
                n += 1
                break
    return n

# 中文检测（中英混杂 → 需要复核，绝不 PASS）
_CJK = re.compile(r"[\u4e00-\u9fff]+")

# 频率副词（题目要求「频率副词」时，句子必须包含其中之一）
_FREQ_ADV = {
    "always", "usually", "often", "sometimes", "rarely", "seldom",
    "never", "normally", "generally", "frequently", "occasionally",
    "daily", "weekly", "monthly", "yearly",
}
_FREQ_RE = re.compile(
    r"\b(every\s+(?:day|morning|evening|week|month|year|night)"
    r"|each\s+(?:day|week|month|year)|most\s+days)\b")

# 任务要求关键词：从题目/语法文本里解析，不硬编码到某个单词
_REQ_FREQ = ("频率副词", "频率", "frequency adverb", "frequency")
_REQ_PRESENT = ("一般现在时", "present tense", "simple present", "一般现在")
_REQ_PAST = ("一般过去时", "past tense", "过去时", "一般过去")

# 高频功能词 / 结构词：永远不会是内容词的拼写错误，近似检查里跳过它们
# （避免把 to 误判成 go、把 he 误判成 be 等）。
_COMMON_WORDS = {
    "the", "a", "an", "and", "or", "but", "if", "when", "because", "so", "to",
    "of", "in", "on", "at", "for", "with", "by", "from", "as", "is", "are",
    "was", "were", "be", "been", "being", "do", "does", "did", "done",
    "have", "has", "had", "he", "she", "it", "we", "they", "you", "i", "my",
    "your", "his", "her", "its", "our", "their", "this", "that", "these",
    "those", "me", "him", "us", "them", "not", "no", "yes", "very", "too",
    "also", "just", "now", "then", "there", "here", "what", "who", "how",
    "why", "which", "can", "will", "would", "should", "may", "might", "must",
    "about", "into", "over", "under", "out", "up", "down", "off", "all",
    "any", "some", "each", "every", "both", "one", "two", "three", "first",
    "last",
}


def _edit_dist(a, b):
    """受限编辑距离：长度差 >1 直接返回 99（避免误伤正常词）。"""
    if abs(len(a) - len(b)) > 1:
        return 99
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


def _span_of(raw, low, token):
    """在 raw/low 里定位 token 的 [start,end)（_safe_lower 长度不变，坐标通用）。"""
    m = re.search(r"(?:(?<=^)|(?<=\s))" + re.escape(token) + r"(?=\s|$|[^a-z'])", low)
    if not m:
        m = re.search(re.escape(token), low)
    if not m:
        return (0, len(raw))
    return m.start(), m.end()


# =====================================================================
# 词形工具
# =====================================================================

# 不规则动词：base -> (三单, 过去式)
_IRREGULAR = {
    "go": ("goes", "went"), "do": ("does", "did"), "have": ("has", "had"),
    "be": ("is", "was"), "get": ("gets", "got"), "make": ("makes", "made"),
    "say": ("says", "said"), "see": ("sees", "saw"), "take": ("takes", "took"),
    "come": ("comes", "came"), "know": ("knows", "knew"), "give": ("gives", "gave"),
    "find": ("finds", "found"), "tell": ("tells", "told"),
    "become": ("becomes", "became"), "leave": ("leaves", "left"),
    "feel": ("feels", "felt"), "bring": ("brings", "brought"),
    "begin": ("begins", "began"), "keep": ("keeps", "kept"),
    "hold": ("holds", "held"), "write": ("writes", "wrote"),
    "stand": ("stands", "stood"), "hear": ("hears", "heard"),
    "let": ("lets", "let"), "mean": ("means", "meant"), "set": ("sets", "set"),
    "meet": ("meets", "met"), "run": ("runs", "ran"), "pay": ("pays", "paid"),
    "sit": ("sits", "sat"), "speak": ("speaks", "spoke"), "lead": ("leads", "led"),
    "read": ("reads", "read"), "grow": ("grows", "grew"), "lose": ("loses", "lost"),
    "fall": ("falls", "fell"), "send": ("sends", "sent"),
    "build": ("builds", "built"),
    "understand": ("understands", "understood"), "draw": ("draws", "drew"),
    "break": ("breaks", "broke"), "spend": ("spends", "spent"),
    "cut": ("cuts", "cut"), "drive": ("drives", "drove"), "buy": ("buys", "bought"),
    "wear": ("wears", "wore"), "choose": ("chooses", "chose"),
    "eat": ("eats", "ate"), "drink": ("drinks", "drank"), "swim": ("swims", "swam"),
    "sing": ("sings", "sang"), "sleep": ("sleeps", "slept"),
    "teach": ("teaches", "taught"), "think": ("thinks", "thought"),
    "catch": ("catches", "caught"), "put": ("puts", "put"), "win": ("wins", "won"),
    "forget": ("forgets", "forgot"), "fight": ("fights", "fought"),
    "sell": ("sells", "sold"), "throw": ("throws", "threw"),
    "wake": ("wakes", "woke"), "ride": ("rides", "rode"), "hide": ("hides", "hid"),
    "steal": ("steals", "stole"), "strike": ("strikes", "struck"),
    "stick": ("sticks", "stuck"), "swing": ("swings", "swung"),
    "freeze": ("freezes", "froze"), "forgive": ("forgives", "forgave"),
    "flee": ("flees", "fled"), "feed": ("feeds", "fed"), "bleed": ("bleeds", "bled"),
    "shoot": ("shoots", "shot"), "shake": ("shakes", "shook"),
    "sink": ("sinks", "sank"), "lie": ("lies", "lay"), "lay": ("lays", "laid"),
    "rise": ("rises", "rose"), "seek": ("seeks", "sought"),
    "tear": ("tears", "tore"), "swear": ("swears", "swore"),
    "bear": ("bears", "bore"), "bind": ("binds", "bound"),
    "deal": ("deals", "dealt"), "dig": ("digs", "dug"), "spin": ("spins", "spun"),
    "split": ("splits", "split"), "spread": ("spreads", "spread"),
    "spring": ("springs", "sprang"), "sting": ("stings", "stung"),
}

# 常用规则动词（原形）。主谓一致 / 时态判定只认这里收录的词，
# 以此避免把名词误判成动词（"He water" 才改，"He dog" 不动）。
_REGULAR = """
accept achieve add admit adopt advise afford agree allow answer apologise apologize
appear apply argue arrange arrive ask attack attend avoid bake balance ban bargain
bark base bathe bear beat become beg believe belong borrow bounce bow brush burn
call cancel care carry celebrate change charge chat cheat check cheer chew clap
clean clear climb close collect comb compare compete complain complete concern
confirm connect consider consist contain continue cook copy correct cough count
cover crash create cry cycle damage dance dare deal decide declare decorate
defeat defend delay deliver depend describe design destroy develop die dig disagree
disappear discover discuss dislike divide double doubt drag dream dress drink
drop dry earn edit educate elect empty encourage end enjoy enter escape examine
exist expand expect explain explore express extend fade fail fasten favour favor
fear fetch fill film finish fish fix flash float flow focus fold follow forbid
force forget form found free gain gather glow grab greet grin guess guard hammer
hand handle hang happen harm hate head heal help hire hug hunt hurry identify
ignore imagine impress improve include increase indicate influence inform insist
install introduce invent invest invite involve iron join joke judge jump kick kiss
knit knock label last laugh launch learn lick lift light like limit link list
listen live load lock long look lose love maintain manage mark marry match matter
mean measure meet melt memorize mend mention mind miss mix move murmur name need
note notice number obey object observe obtain occur offer open operate order
organise organize owe own pack paint park pass pause perform pick pin place plan
plant play please plug point polish pop possess post pour practise practice praise
pray preach prefer prepare present preserve press pretend prevent print produce
promise pronounce protect prove provide publish pull punch purchase push put
question quit race raise reach realize realise receive recognize recommend record
recover reduce refer reflect refuse regret reject relax release rely remain
remember remind remove repair repeat replace reply report represent request
require rescue research resemble resist respect respond rest retire return reveal
review ride ring rise roll rub ruin rule rush sail save scratch scream search
seat select send separate serve set settle shake share shave shed shine shiver
shock shoot shop shout show shrug shut sign sing sink sit skate skip sleep slide
slip slow smash smell smile smoke sneeze sniff snore soak solve sort sound spare
spell spend spill spin spoil spray spread spring sprout squeeze stain stamp stand
stare start state stay steal steer step stir stitch stop store stretch strike
strip study suck suffer suggest supply support suppose surprise surround survive
swallow swear sweep swell swim swing switch talk tame tap taste tease telephone
tell tempt tend terrify test thank threaten tick tickle tie time tip tire touch
tour tow trace track trade train transfer translate transport trap travel treat
trip trust try turn twist type use vanish visit wait wake walk wander want warm
warn wash waste watch wave wear weigh welcome whisper whistle wink wipe wish
withdraw wobble work worry wrap wreck write yell yawn
"""

VERBS = set(_REGULAR.split()) | set(_IRREGULAR.keys())

# 以 s 结尾但**不是**动词三单的词。_r_s3_with_i 抓「I/we/you/they + 动词s」
# 时靠这份白名单排掉副词、名词复数和代词，避免把
# "I always ..." / "I have two books" 判成主谓一致错误。
_S_WORDS_NOT_VERBS = {
    # 副词 / 连词 / 限定词
    "always", "sometimes", "often", "usually", "perhaps", "maybe", "thus",
    "yes", "as", "is", "was", "has", "does", "less", "plus", "versus",
    # 代词 / 指示词
    "this", "his", "its", "whose", "hers", "ours", "yours", "theirs",
    "something", "anything", "nothing", "everything",
    # 常见名词复数（I/we/you/they 后面直接跟名词的场景）
    "books", "friends", "days", "times", "things", "words", "students",
    "teachers", "apples", "cars", "games", "songs", "movies", "photos",
    "hours", "minutes", "weeks", "months", "years", "people", "children",
}

# 明确的「活动类动词」：用于 like/enjoy/finish + doing 判定。
# 刻意不含 work / study 这类常作名词的词，避免 "I finish work at 5" 被误判。
_ACTIVITY = {
    "swim", "read", "cook", "play", "watch", "run", "walk", "dance", "sing",
    "draw", "travel", "shop", "eat", "drive", "write", "paint", "fish",
    "camp", "hike", "jog", "ski", "skate", "cycle", "climb", "chat", "talk",
    "speak", "smoke", "drink", "sleep", "wait", "exercise", "clean", "type",
    "surf", "bake", "garden", "text", "call", "learn", "teach", "practice",
    "practise", "relax", "explore", "photograph", "go", "come", "stay",
    "visit", "see", "meet", "leave", "start", "stop", "try", "help",
}

# -ing 结尾但其实是名词的词：want/need + 这些词不是「该改成 to do」
_ING_NOUNS = {
    "something", "anything", "everything", "nothing", "thing", "morning",
    "evening", "meeting", "training", "building", "painting", "drawing",
    "writing", "reading", "shopping", "parking", "swimming", "beginning",
    "wedding", "clothing", "interesting", "boring", "exciting", "ceiling",
    "feeling", "saving", "earning", "meaning", "opening", "warning",
}

# 不该被当成「动词原形」处理的词（be / 助动词 / 情态动词 / 常见副词）
_AUX_BE = {"am", "is", "are", "was", "were", "be", "been", "being"}
_MODALS = {"will", "would", "can", "could", "shall", "should", "may", "might",
           "must", "ought", "do", "does", "did", "done", "doing",
           "have", "has", "had", "having", "not", "no", "never", "too", "also",
           "just", "still", "even", "now", "then", "there", "here", "always",
           "often", "sometimes", "usually", "really", "very", "quite", "so",
           "get", "gets", "getting", "to", "and", "or", "but", "if", "when"}

_PAST_MARKER = re.compile(r"\b(yesterday|ago|last\s+\w+)\b")

# 第三人称单数主语（内置一层捕获组：group 1 = 主语）
_S3 = (
    r"(he|she|it|tom|mary|john|amy|lisa|david|peter|mike|sarah|lucy|jack|"
    r"my\s+(?:mother|father|mom|dad|sister|brother|friend|teacher|boss|wife|"
    r"husband|son|daughter|manager|colleague|team|cat|dog)|"
    r"his\s+(?:mother|father|sister|brother|friend|wife|husband|son|daughter)|"
    r"her\s+(?:mother|father|sister|brother|friend|wife|husband|son|daughter)|"
    r"the\s+(?:boy|girl|man|woman|teacher|student|doctor|nurse|driver|"
    r"dog|cat|bird|car|bus|train|book|movie|film|company|team|manager|boss|"
    r"meeting|report|weather|food|price|problem))"
)
_S3_RE = re.compile(r"\b" + _S3 + r"\b")
# 主语与动词之间允许出现的副词（不含 very —— very 由专门规则处理）
_ADV_WORD = (r"usually|always|often|sometimes|never|rarely|seldom|normally|"
             r"generally|frequently|also|just|really|still|even|already|simply|"
             r"actually|definitely|probably|quite|truly|deeply|only")
_S3_ADV = r"(?:" + _ADV_WORD + r")\s+"
# 注意：`((?:...)\s+)*` 里的 * 只会作用在 \s+ 上，会触发 multiple repeat；
# 必须再包一层非捕获组：((?:(?:...)\s+)*)
_S3_ADVS = r"(?:(?:" + _ADV_WORD + r")\s+)*"


def _third(v):
    if v in _IRREGULAR:
        return _IRREGULAR[v][0]
    if v.endswith(("s", "x", "z", "ch", "sh", "o")):
        return v + "es"
    if v.endswith("y") and len(v) > 1 and v[-2] not in "aeiou":
        return v[:-1] + "ies"
    return v + "s"


def _past(v):
    if v in _IRREGULAR:
        return _IRREGULAR[v][1]
    if v.endswith("e"):
        return v + "d"
    if v.endswith("y") and len(v) > 1 and v[-2] not in "aeiou":
        return v[:-1] + "ied"
    _DOUBLE = {"stop", "plan", "shop", "drop", "prefer", "travel", "cancel",
               "admit", "begin", "control", "occur", "refer", "regret", "chat",
               "fit", "grab", "hug", "jog", "nod", "pat", "pin", "rub", "slip",
               "step", "swap", "tip", "trap", "wrap", "quit"}
    if v in _DOUBLE:
        return v + v[-1] + "ed"
    return v + "ed"


def _ing(v):
    if v.endswith("ie"):
        return v[:-2] + "ying"
    if v.endswith("e") and not v.endswith(("ee", "ye", "oe")):
        return v[:-1] + "ing"
    _DOUBLE_LAST = {"swim", "run", "get", "sit", "shop", "stop", "begin",
                    "plan", "drop", "put", "cut", "win", "dig", "jog", "chat",
                    "let", "set", "fit", "hit", "nod", "rub", "travel",
                    "cancel", "control", "prefer", "refer", "occur"}
    if v in _DOUBLE_LAST:
        return v + v[-1] + "ing"
    return v + "ing"


def _plural_of(noun):
    _IR = {"child": "children", "man": "men", "woman": "women",
           "foot": "feet", "tooth": "teeth", "person": "people"}
    if noun in _IR:
        return _IR[noun]
    if noun.endswith(("s", "x", "z", "ch", "sh")):
        return noun + "es"
    if noun.endswith("y") and len(noun) > 1 and noun[-2] not in "aeiou":
        return noun[:-1] + "ies"
    if noun.endswith("f"):
        return noun[:-1] + "ves"
    return noun + "s"


def _safe_lower(s):
    """逐字符小写，保证 len 不变（str.lower() 对个别 Unicode 字符会改变长度）。"""
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in s)


def _normalize(s):
    """弯引号/撇号统一成 ASCII，避免 I'm 被正则切断（长度 1:1 不变）。"""
    return (s.replace("\u2019", "'").replace("\u2018", "'")
             .replace("\u201c", '"').replace("\u201d", '"'))


def _err(etype, where, correct, expl, span, repl, severity="medium"):
    """一条错误。where/correct 用于**展示**，span/repl 用于**改句**。"""
    return {"type": etype, "where": where, "correct": correct,
            "explanation": expl, "span": span, "repl": repl,
            "severity": severity}


# =====================================================================
# 主语判断（主谓一致用）
# =====================================================================

_CLAUSE_BREAKS = (".", ";", ",", "!", "?", " but ", " because ", " so ",
                  " which ", " who ", " when ", " if ", " although ", " that ")


def _coordinated(seg, subj_start):
    """主语是否与前面的名词并列（Tom and Mary go —— 复数，不该判三单）。"""
    before = seg[:subj_start]
    b = 0
    for br in _CLAUSE_BREAKS:
        idx = before.rfind(br)
        if idx >= 0:
            b = max(b, idx + len(br))
    clause = before[b:]
    if not re.search(r"\b(and|or)\b", clause):
        return False
    return not any(w in VERBS for w in clause.split())


def _find_subject3(low, verb_start):
    """在 verb_start 之前找最近的第三人称单数主语，返回 (Match, 是否并列主语)。"""
    head = low[:verb_start]
    seg = head[-50:] if len(head) > 50 else head
    m = None
    for mm in _S3_RE.finditer(seg):
        m = mm
    if m is None:
        return None, False
    # seg 结束于动词之前。若它本身以 and / or 收尾，说明主语是并列结构里的
    # 第二个（Tom and Mary go → 复数，不该加 -s）；中间若出现过动词则不是并列主语。
    if re.search(r"\b(and|or)\s+$", seg) and not any(w in VERBS for w in seg.split()):
        return m, True
    if _coordinated(seg, m.start()):
        return m, True
    return m, False


def _is_third_person(low, verb_start):
    """动词 verb_start 处的主语是否为第三人称单数（中间只允许副词）。"""
    m, coord = _find_subject3(low, verb_start)
    if m is None or coord:
        return False
    head = low[:verb_start]
    seg = head[-50:] if len(head) > 50 else head
    tail = seg[m.end():]
    return re.fullmatch(r"[,]?\s*(?:(?:" + _ADV_WORD + r"|\w+ly)\s+)*", tail) is not None


# =====================================================================
# 规则集：每条返回错误列表，span 一律基于**原句**坐标
# =====================================================================

_PRON_SUBJ = {"me": "I", "him": "he", "her": "she", "us": "we",
              "them": "they", "you": "you", "it": "it"}


# 常作形容词用的词：be + 这些词完全正确（I am free / It is live），不能要求 -ing
_ADJ_NOT_VERB = {
    "free", "live", "clean", "clear", "close", "open", "dry", "warm", "cold",
    "cool", "empty", "own", "long", "short", "slow", "fast", "present",
    "separate", "light", "dark", "sound", "right", "wrong", "safe", "sure",
    "ready", "busy", "full", "quiet", "calm", "content", "flat", "loose",
    "plain", "prime", "spare", "subject", "worth", "due", "faint", "level",
    "round", "square", "thin", "thick", "sharp", "smooth", "cheap", "dear",
    "firm", "brief", "chief", "vast", "rare", "dull", "fond", "glad", "upset",
}


def _r_be_verb(s, low):
    """be + 动词：I am agree with you / He is go home。"""
    out = []
    for m in re.finditer(r"\b(am|is|are|was|were|be)\s+(agree|disagree)\b", low):
        be, verb = m.group(1), m.group(2)
        new = _past(verb) if be in ("was", "were") else verb
        out.append(_err(
            "句型", f"{be} {verb}", new,
            f"agree（同意）本身就是动词，前面不能再加 be 动词（{be}）。"
            f"直接说「主语 + {new}」就够了，例如 I {new} with you。",
            m.span(), new, "heavy"))
    for m in re.finditer(r"\b(am|is|are|was|were)\s+([a-z]+)\b", low):
        be, verb = m.group(1), m.group(2)
        if (verb in _AUX_BE or verb in _MODALS or verb not in VERBS
                or verb in _ADJ_NOT_VERB):
            continue
        new = _ing(verb)
        out.append(_err(
            "句型", f"{be} {verb}", f"{be} {new}",
            f"be 动词（{be}）后面要接 -ing 构成进行时，不能直接接动词原形 {verb}。"
            f"想说习惯性的动作用一般现在时，想说正在做就用 {be} {new}。",
            (m.start(2), m.end(2)), new, "heavy"))
    return out


def _r_very_verb(s, low):
    """very + 情感动词：I very like you."""
    out = []
    for m in re.finditer(
            r"\bvery\s+(like|love|enjoy|hate|want|need|agree|prefer|miss|mind|"
            r"appreciate|admire|recommend|suggest)\b", low):
        verb = m.group(1)
        third = _is_third_person(low, m.start())
        target = _third(verb) if third else verb
        out.append(_err(
            "词性", f"very {verb}", f"really {target}",
            f"very 不能直接放在动词 {verb} 前面修饰它。very 只修饰形容词和副词"
            f"（very good / very quickly）；修饰「喜欢、想要」这类动词时要用 "
            f"really（放在动词前面）或 very much（放在句末）。"
            f"所以「非常喜欢」要说 really {target}"
            + ("（这里主语是第三人称单数，动词还要加 -s/-es）。" if third else "。"),
            m.span(), f"really {target}", "heavy"))
    return out


def _r_very_much_order(s, low):
    """like very much this movie → like this movie very much（中式语序）。"""
    out = []
    pat = r"\b(like|love|enjoy|hate|miss)\s+very\s+much\s+([a-z']+(?:\s+[a-z']+)*?)([.,;!?]|$)"
    for m in re.finditer(pat, low):
        verb, obj = m.group(1), (m.group(2) or "").strip()
        if not obj:
            continue
        third = _is_third_person(low, m.start())
        target = _third(verb) if third else verb
        new = f"{target} {obj} very much"
        out.append(_err(
            "词序", f"{verb} very much {obj}", new,
            "very much 修饰动词时要放在**宾语后面或句末**，不能夹在动词和宾语中间。"
            "英文的语序是「主语 + 动词 + 宾语 + very much」；中文的「很喜欢这个」"
            "不能逐字翻成 like very much this。",
            (m.start(1), m.end(2)), new, "heavy"))
    return out


def _r_suggest_to(s, low):
    """suggest sb to do → suggest (that) sb do。"""
    out = []
    for m in re.finditer(
            r"\b(suggest|suggests|suggested|recommend|recommends|recommended)\s+"
            r"(me|him|her|us|them|you|it)\s+to\s+([a-z]+)\b", low):
        head, obj, verb = m.group(1), m.group(2), m.group(3)
        subj = _PRON_SUBJ.get(obj, obj)
        new = f"{head} that {subj} {verb}"
        out.append(_err(
            "固定搭配", f"{head} {obj} to {verb}", new,
            f"{head} 后面不能接「人 + to do」。它要么接名词或动名词"
            f"（{head} going），要么接 that 从句（{new}）。"
            f"中文的「建议我去」最容易在这里直译成英文出错。",
            m.span(), new, "heavy"))
    return out


def _r_look_forward(s, low):
    """look forward to see → look forward to seeing（这里的 to 是介词）。"""
    out = []
    for m in re.finditer(r"\blook\w*\s+forward\s+to\s+([a-z]+)\b", low):
        verb = m.group(1)
        if verb in VERBS and not verb.endswith("ing"):
            new = _ing(verb)
            out.append(_err(
                "固定搭配", f"look forward to {verb}", f"look forward to {new}",
                "look forward to 里的 to 是**介词**，不是不定式的 to，"
                "所以后面必须接名词或动名词（-ing）。「期待见到你」要说 "
                f"look forward to {new} you。",
                (m.start(1), m.end(1)), new, "heavy"))
    return out


def _r_make_let_to(s, low):
    """make / let / see / hear sb to do → sb do（使役与感官动词不加 to）。"""
    out = []
    for m in re.finditer(
            r"\b(make|makes|made|let|lets|see|sees|saw|hear|hears|heard|"
            r"watch|watches|watched)\s+(me|him|her|us|them|you|it)\s+to\s+([a-z]+)\b",
            low):
        head, obj, verb = m.group(1), m.group(2), m.group(3)
        new = f"{head} {obj} {verb}"
        out.append(_err(
            "固定搭配", f"{head} {obj} to {verb}", new,
            f"{head} 是使役/感官动词，后面接「人 + 动词原形」，中间**不加 to**。"
            f"去掉 to 就行：{new}。",
            m.span(), new, "heavy"))
    return out


_GERUND_HEADS = (r"like|likes|love|loves|loved|enjoy|enjoys|enjoyed|hate|hates|"
                 r"hated|finish|finishes|finished|mind|minds|keep|keeps|kept|"
                 r"practise|practises|practice|practices|practiced|avoid|avoids|"
                 r"miss|misses|missed|consider|considers|suggest|suggests|"
                 r"admit|admits|deny|denies|imagine|imagines")


def _r_verb_pattern(s, low):
    """like/enjoy/finish + 原形 → doing；want/need/decide + doing → to do。"""
    out = []
    for m in re.finditer(rf"\b({_GERUND_HEADS})\s+([a-z]+)\b", low):
        head, verb = m.group(1), m.group(2)
        if verb not in _ACTIVITY or verb.endswith("ing"):
            continue
        new = _ing(verb)
        out.append(_err(
            "固定搭配", f"{head} {verb}", f"{head} {new}",
            f"{head} 后面接动作动词时，要用 doing（动名词）形式，不能接原形 {verb}。"
            f"应写成 {head} {new}。",
            (m.start(2), m.end(2)), new, "medium"))
    for m in re.finditer(
            r"\b(want|wants|wanted|need|needs|needed|decide|decides|decided|"
            r"hope|hopes|hoped|plan|plans|planned|promise|promises|agree|agrees|"
            r"would\s+like)\s+([a-z]+ing)\b", low):
        head, ger = m.group(1), m.group(2)
        if ger in _ING_NOUNS:
            continue
        base = ger[:-3]
        if base not in _ACTIVITY:
            continue
        out.append(_err(
            "固定搭配", f"{head} {ger}", f"{head} to {base}",
            f"{head} 后面要接**带 to 的不定式**（to do），不接 doing。"
            f"应写成 {head} to {base}。",
            (m.start(2), m.end(2)), f"to {base}", "medium"))
    return out


# 冗余/中式搭配：(正则, 替换(None=用最后一组), 类型, 说明, 严重度, 只替换最后一组?)
_REDUNDANT = [
    (r"\bcan\s+able\s+to\b", "can", "句型",
     "can 和 be able to 都表示「能够」，两个不能叠着用。保留 can 就够了"
     "（想强调可以说 am able to）。", "heavy"),
    (r"\bmore\s+(better|worse|easier|harder|bigger|smaller|faster|slower|"
     r"cheaper|older|younger|longer|shorter|higher|lower|safer|smarter)\b",
     None, "词性",
     "比较级重复了：more 已经表示「更」，后面就不能再加 -er 的比较级，二选一即可。",
     "heavy"),
    (r"\bdiscuss\s+about\b", "discuss", "介词",
     "discuss（讨论）是及物动词，后面直接接讨论的内容，不用加 about。",
     "medium"),
    (r"\bmarry\s+with\b", "marry", "介词",
     "marry 是及物动词，「和某人结婚」说 marry sb，不加 with。", "medium"),
    (r"\bcontact\s+with\b", "contact", "介词",
     "contact 是及物动词，「联系某人」说 contact sb，不加 with。", "medium"),
    (r"\breturn\s+back\b", "return", "固定搭配",
     "return 本身已经含有「回」的意思，再加 back 就重复了。", "medium"),
    (r"\brepeat\s+again\b", "repeat", "固定搭配",
     "repeat 本身就是「再说一遍」，再加 again 就重复了。", "medium"),
    (r"\bemphasize\s+on\b", "emphasize", "介词",
     "emphasize 是及物动词，后面直接接宾语，不加 on。", "medium"),
    (r"\baccording\s+to\s+my\s+opinion\b", "in my opinion", "固定搭配",
     "according to 后面接「来源/依据」，不接 my opinion。表达个人看法用 in my opinion。",
     "heavy"),
    (r"\bplay\s+the\s+(basketball|football|soccer|volleyball|baseball|"
     r"badminton|tennis|ping-?pong|chess)\b", None, "冠词",
     "球类运动和棋类前面不加 the。打篮球是 play basketball。", "medium"),
    (r"\b(open|close)\s+the\s+(light|lights|tv|television|radio|fan)\b",
     None, "固定搭配",
     "中文的「开/关灯、开/关电视」在英文里要用 turn on / turn off，不用 open / close。",
     "medium"),
    (r"\bgo\s+to\s+(home|there|here|abroad|downtown|upstairs|downstairs)\b",
     None, "介词",
     "home / there / here / abroad 这类词是副词，前面不加 to。回家是 go home。",
     "medium"),
    (r"\b(listen|listens|listened)\s+music\b", None, "介词",
     "listen 是不及物动词，要先加 to 再接听的内容：listen to music。",
     "medium"),
    (r"\barrive\s+to\b", "arrive at", "介词",
     "arrive 后面接地点用 at（小地方）或 in（大城市、国家），不用 to。",
     "medium"),
]


def _r_redundant(s, low):
    """中式英语里最高频的冗余与搭配错误。"""
    out = []
    for pat, repl, etype, expl, sev in _REDUNDANT:
        for m in re.finditer(pat, low):
            frag = m.group(0)
            if repl:
                new = repl
            elif "play" in pat:
                new = "play " + m.group(m.re.groups)
            elif pat.startswith(r"\bgo\s+to\s+"):
                new = "go " + m.group(m.re.groups)
            elif pat.startswith(r"\b(open|close)"):
                vb = "turn on" if m.group(1).lower() == "open" else "turn off"
                new = f"{vb} the {m.group(2)}"
            elif pat.startswith(r"\b(listen"):
                new = f"{m.group(1)} to music"
            else:
                new = m.group(m.re.groups)
            if new == frag:
                continue
            out.append(_err(etype, frag, new, expl, m.span(), new, sev))
    return out


def _r_modal_to(s, low):
    """情态动词后多加了 to：I can to swim → I can swim。

    中文没有情态动词，所以「我能去游泳」很容易被直译成 I can to swim。
    规则：can / must / should / may / might / will / would / shall 后面
    直接跟动词原形，中间不能有 to。
    """
    out = []
    for m in re.finditer(
            r"\b(can|could|must|should|shall|may|might|will|would)\s+to\s+([a-z]+)\b",
            low):
        modal, verb = m.group(1), m.group(2)
        out.append(_err(
            "句型", f"{modal} to {verb}", f"{modal} {verb}",
            f"{modal} 是情态动词，后面直接跟动词原形，中间不能加 to。"
            f"应写成 {modal} {verb}。",
            (m.start(), m.end()), f"{modal} {verb}", "heavy"))
    return out


def _r_to_gerund(s, low):
    """to 后面接了 doing：like to playing → like to play / like playing。

    不定式 to 后面必须是动词原形。想接 doing 就把 to 去掉
    （like doing），两种说法都对，但不能混成 to doing。
    """
    out = []
    for m in re.finditer(r"\bto\s+([a-z]+ing)\b", low):
        ger = m.group(1)
        base = ger[:-3]
        if base not in _ACTIVITY:
            continue
        # 排除 be used to doing / look forward to doing 这类 to 是介词的固定搭配
        if re.search(r"\b(used|accustomed|forward|object|opposed|committed)\b",
                     low[:m.start()]):
            continue
        out.append(_err(
            "固定搭配", f"to {ger}", f"to {base}",
            f"带 to 的不定式后面要接动词原形，不能接 doing。"
            f"想说「{ger}」就把 to 去掉，写成 {ger}；想保留 to 就改成 to {base}。",
            (m.start(), m.end()), base, "heavy"))
    return out


def _r_s3_with_i(s, low):
    """非三单主语接了三单动词：I likes → I like。

    反向的（he go → he goes）已由 _r_subj_verb 覆盖。
    这条补的是 I / we / you / they 后面误加 -s 的情况。
    """
    out = []
    if _PAST_MARKER.search(low):
        return out
    for m in re.finditer(r"\b(i|we|you|they)\s+([a-z]+s)\b", low):
        subj, verb = m.group(1), m.group(2)
        if verb in _AUX_BE or verb in _MODALS:
            continue
        # 排除本身就是 -s 结尾的原形（always / sometimes / his / this 等）
        if verb in _S_WORDS_NOT_VERBS:
            continue
        # 三单还原成原形：likes→like / goes→go / studies→study。
        # 只砍一个 s 是不够的（goes→goe 不在词表里），要按 es / ies 依次试。
        base = None
        for cand in (verb[:-1],
                     verb[:-2] if verb.endswith("es") else None,
                     verb[:-3] + "y" if verb.endswith("ies") else None):
            if cand and cand in VERBS:
                base = cand
                break
        if base is None:
            continue
        # 排除名词复数：I have two books / they are my friends
        if re.search(r"\b(two|three|four|five|many|some|my|his|her|their|our)\s+$",
                     low[:m.start(2)]):
            continue
        out.append(_err(
            "主谓一致", f"{subj} {verb}", f"{subj} {base}",
            f"{subj} 不是第三人称单数，后面的动词用原形，不能加 -s。"
            f"应写成 {subj} {base}。只有 he / she / it 后面才加 -s。",
            (m.start(2), m.end(2)), base, "heavy"))
    return out


def _r_although_but(s, low):
    """Although ..., but ... —— 英文里两者不能同时出现。"""
    out = []
    for m in re.finditer(r"\b(?:although|though)\b[^.!?]{0,80}?,\s*but\b", low):
        pos = m.group(0).lower().rfind("but")
        start = m.start() + pos
        out.append(_err(
            "句型", "but", "（把 but 删掉）",
            "英文里 although（虽然）和 but（但是）**不能用在同一个句子里**，"
            "这和中文的「虽然…但是…」不一样。用了 although，后面就直接说结果。",
            (start, start + 3), "", "medium"))
    return out


def _r_aux_agree(s, low):
    """he don't → he doesn't；I doesn't → I don't。"""
    out = []
    for m in re.finditer(r"\b(he|she|it|tom|mary)\s+(don't|do\s+not)\b", low):
        out.append(_err(
            "主谓一致", f"{m.group(1)} {m.group(2)}", f"{m.group(1)} doesn't",
            f"{m.group(1)} 是第三人称单数，否定要用 doesn't，不是 don't。",
            (m.start(2), m.end(2)), "doesn't", "heavy"))
    for m in re.finditer(r"\b(i|we|you|they)\s+(doesn't|does\s+not)\b", low):
        out.append(_err(
            "主谓一致", f"{m.group(1)} {m.group(2)}", f"{m.group(1)} don't",
            f"{m.group(1)} 不是第三人称单数，否定用 don't，不用 doesn't。",
            (m.start(2), m.end(2)), "don't", "heavy"))
    return out


def _r_subj_verb(s, low):
    """主谓一致：he/she/it + 动词原形 → 加 -s/-es。"""
    out = []
    if _PAST_MARKER.search(low):
        return out  # 有过去时间标志时交给时态规则，避免把 went 又改回三单
    for m in re.finditer(rf"\b{_S3}\s+({_S3_ADVS})([a-z]+)\b", low):
        subj, verb = m.group(1), m.group(3)
        if verb in _AUX_BE or verb in _MODALS or verb not in VERBS:
            continue
        if _find_subject3(low, m.start())[1]:
            continue  # 并列主语（Tom and Mary go）是复数，不该加 -s
        new = _third(verb)
        out.append(_err(
            "主谓一致", f"{subj} {verb}", f"{subj} {new}",
            f"{subj} 是第三人称单数。在一般现在时里，它后面的动词要加 -s / -es，"
            f"所以 {verb} 要写成 {new}。只有当句子是过去时，或者前面有 "
            f"can / will / must 这类情态动词时，才用动词原形。",
            (m.start(3), m.end(3)), new, "heavy"))
    return out


def _r_past_tense(s, low):
    """有过去时间标志却用了动词原形 / 现在时。"""
    out = []
    if not _PAST_MARKER.search(low):
        return out
    for m in re.finditer(
            r"\b(i|you|we|they|he|she|it|tom|mary)\s+(?:(?:" + _ADV_WORD + r")\s+)*([a-z]+)\b",
            low):
        verb = m.group(2)
        if verb in _AUX_BE or verb in _MODALS or verb not in VERBS:
            continue
        new = _past(verb)
        out.append(_err(
            "时态", f"{m.group(1)} {verb}", f"{m.group(1)} {new}",
            f"句子里有 yesterday / ago / last … 这样的过去时间，动词要用**过去式**。"
            f"{verb} 的过去式是 {new}。",
            (m.start(2), m.end(2)), new, "heavy"))
    for m in re.finditer(r"\b(i|he|she|it)\s+(am|is)\b", low):
        out.append(_err(
            "时态", f"{m.group(1)} {m.group(2)}", f"{m.group(1)} was",
            "句子说的是过去的事，be 动词要用过去式：I / he / she / it 都用 was。",
            (m.start(2), m.end(2)), "was", "heavy"))
    for m in re.finditer(r"\b(we|you|they)\s+(are)\b", low):
        out.append(_err(
            "时态", f"{m.group(1)} are", f"{m.group(1)} were",
            "句子说的是过去的事，be 动词要用过去式：we / you / they 都用 were。",
            (m.start(2), m.end(2)), "were", "heavy"))
    return out


# 介词规则：(正则, 替换(None=on + 组2 / "in"=in the + 组1), 说明)
_PREP_RULES = [
    (r"\bat\s+the\s+(morning|afternoon|evening)\b", "in",
     "表示「在早上 / 下午 / 晚上」要用 in，固定说 in the morning / in the afternoon / in the evening。"),
    (r"\bin\s+the\s+floor\b", "on the floor",
     "在「地板上 / 几楼」用 on：on the floor、on the second floor。"),
    (r"\bby\s+the\s+(bus|car|train|bike|plane|subway)\b", None,
     "by + 交通工具中间不加冠词：by bus / by car / by train。"),
    (r"\bgood\s+(in|with)\s+english\b", "good at English",
     "表示「擅长…」用 be good at，介词固定是 at。"),
    (r"\b(in|at)\s+(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b",
     "weekday",
     "星期几前面用 on：on Monday / on Sunday。"),
    (r"\bin\s+the\s+weekend\b", "at the weekend",
     "英式英语说 at the weekend，美式说 on the weekend；不要混成 in the weekend。"),
]


def _r_prep(s, low):
    """高频介词错误。"""
    out = []
    for pat, repl, expl in _PREP_RULES:
        for m in re.finditer(pat, low):
            frag = m.group(0)
            if repl == "in":
                new = "in the " + m.group(1)
            elif repl == "weekday":
                new = "on " + m.group(2)
            elif repl is None:
                new = "by " + m.group(1)
            else:
                new = repl
            if new == frag:
                continue
            out.append(_err("介词", frag, new, expl, m.span(), new, "medium"))
    return out


def _r_colloc(s, low):
    """固定搭配与大小写。"""
    out = []
    for m in re.finditer(r"\bgo\s+(work|school|bed)\b", low):
        new = "to " + m.group(1)
        out.append(_err(
            "固定搭配", m.group(0), f"go {new}",
            "go 后面接目的地要加 to：go to work（去上班）/ go to school（去上学）"
            "/ go to bed（去睡觉）。",
            (m.start(1), m.end(1)), new, "medium"))
    for m in re.finditer(r"\bmake\s+friend\b", low):
        out.append(_err(
            "固定搭配", "make friend", "make friends",
            "「交朋友」是 make friends，friend 要用复数。",
            m.span(), "make friends", "medium"))
    # 单独的英文单词 i 必须大写（只在原句里真的是小写时才报）
    for m in re.finditer(r"\bi\b", low):
        if s[m.start():m.end()] == "i":
            out.append(_err("其他", "i", "I",
                            "英文里表示「我」的 I 永远要大写，不管它在句子的哪个位置。",
                            m.span(), "I", "light"))
            break
    return out


_AN_EXCEPT = {"university", "uniform", "useful", "useless", "user", "unit",
              "union", "unique", "universal", "european", "one", "once",
              "used", "usual", "utility", "euro"}
_AN_REQUIRE = {"hour", "honest", "honor", "honour", "heir", "honorable"}


def _r_article(s, low):
    """a / an 误用。"""
    out = []
    for m in re.finditer(r"\ba\s+([a-z][a-z-]*)\b", low):
        w = m.group(1)
        if w in _AN_EXCEPT:
            continue
        if w[0] in "aeiou" or w in _AN_REQUIRE:
            tip = (f"{w} 的 h 不发音，实际以元音开头，前面要用 an。"
                   if w in _AN_REQUIRE else
                   f"{w} 以元音开头，前面要用 an 不是 a（看读音，不是看字母）。")
            out.append(_err("冠词", f"a {w}", f"an {w}", tip,
                            m.span(), f"an {w}", "medium"))
    for m in re.finditer(r"\ban\s+([a-z][a-z-]*)\b", low):
        w = m.group(1)
        if w in _AN_EXCEPT:
            out.append(_err(
                "冠词", f"an {w}", f"a {w}",
                f"{w} 虽然以字母 u 开头，但读音是 /juː/（辅音开头），前面用 a 不用 an。",
                m.span(), f"a {w}", "medium"))
    return out


_PLURAL_TRIGGER = {
    "friend": ["many", "these", "those", "two", "three", "several", "four", "five"],
    "book": ["many", "these", "those", "two", "three", "several"],
    "apple": ["many", "two", "three", "some", "four"],
    "egg": ["many", "two", "three", "some"],
    "pen": ["many", "two", "three", "some"],
    "student": ["many", "these", "those", "two", "three", "several"],
    "day": ["many", "two", "three", "several", "seven", "five"],
    "hour": ["many", "two", "three", "several"],
    "idea": ["many", "these", "those", "two", "three"],
    "child": ["many", "these", "those", "two", "three"],
}


def _r_plural(s, low):
    """数量词后面的可数名词应当用复数。"""
    out = []
    for bare, triggers in _PLURAL_TRIGGER.items():
        for tg in triggers:
            for m in re.finditer(rf"\b{tg}\s+{bare}\b", low):
                new = f"{tg} {_plural_of(bare)}"
                out.append(_err(
                    "单复数", f"{tg} {bare}", new,
                    f"{tg} 表示「不止一个」，后面的可数名词 {bare} 要用复数 "
                    f"{_plural_of(bare)}。",
                    m.span(), new, "medium"))
    return out


# =====================================================================
# 新增规则：上下文相关（拼写 / 中文 / I-l / 句法完整性 / 冠词）
# 这些规则无法从单条「语法规则」覆盖，需要结合目标词与整句判断。
# 它们产生的都是「硬错误」，会进入错题本（除非调用方显式不写库）。
# =====================================================================

def _r_chinese(raw, low):
    """中英混杂：句子里出现中文字符 → 需要复核，绝不 PASS。"""
    out = []
    for m in re.finditer(r"[\u4e00-\u9fff]+", raw):
        s = m.group(0)
        out.append(_err(
            "其他", s, s,
            f"句子里混入了中文「{s}」。英语造句请保持全英文；"
            f"如果想表达这个意思，请换成英文（例如 city center）。",
            (m.start(), m.end()), s, "heavy"))
    return out


def _r_i_l(raw, low):
    """句首的小写 l 当作 I：l like → I like。"""
    out = []
    m = re.match(r"\s*l\s+(?=[a-z])", low)
    if m:
        out.append(_err(
            "拼写", "l", "I",
            "句首的「l」应该是大写字母「I」（英语里“我”永远写成大写 I）。",
            (m.start(), m.start() + 1), "I", "medium"))
    return out


def _looks_like_verb(tok):
    """这个词看起来像谓语动词吗？原形 / 三单 / 过去式 / ing 都算。

    之前只认原形，导致 "I likes apple." 被判成「缺少谓语动词」
    （因为 likes 不在原形表里），而这个整句级的大区间错误又会在去重时
    把「主谓一致」这类更精确的错误吞掉 —— 真正的问题反而看不见。
    """
    if tok in VERBS or tok in _AUX_BE or tok in _MODALS:
        return True
    if tok.endswith("ing"):
        return tok[:-3] in VERBS or tok in VERBS
    if tok.endswith("ed"):
        return tok[:-2] in VERBS or tok[:-1] in VERBS or tok in _IRREGULAR.values()
    if tok.endswith("es"):
        # studies→study（ies 换回 y）也要认，否则整句会被误判成缺谓语
        return (tok[:-2] in VERBS or tok[:-1] in VERBS
                or (tok[:-3] + "y") in VERBS)
    if tok.endswith("s"):
        return tok[:-1] in VERBS
    return False


def _r_no_verb(raw, low):
    """句法完整性：>=3 个词却没有任何谓语动词 → 不是完整句子。"""
    out = []
    toks = re.findall(r"[a-z']+", low)
    if len(toks) < 3:
        return out
    has_verb = any(_looks_like_verb(t) for t in toks)
    if not has_verb:
        out.append(_err(
            "句型", "（缺少谓语动词）", raw,
            "这个句子缺少谓语动词，不是一个完整的英文句子。"
            "请写成「主语 + 动词 + …」的结构，例如加上 be / do / work 等动词。",
            (0, len(raw)), raw, "medium"))
    return out


def _r_overseas_dept(raw, low):
    """from/in/at/of/to + overseas department 缺少冠词 the。"""
    out = []
    for m in re.finditer(r"\b(from|in|at|of|to)\s+overseas\s+department\b", low):
        out.append(_err(
            "冠词", "overseas department", "the overseas department",
            "「overseas department」前通常要加冠词 the（指“那个海外部门”）。"
            "更自然的说法是 I am from the overseas department. "
            "或 I work in the overseas department.",
            m.span(), "the overseas department", "medium"))
    return out


def _r_target_spelling(raw, low, word):
    """目标词拼写检查：句子里出现与目标词「等长且仅 1 处替换」的近似词 → 拼写错误。

    例：comoany vs company（等长、o↔p 一处替换）→ 判拼写错误。
    不误伤：company 本身（精确命中）、companies（前缀匹配）均不触发。
    """
    out = []
    w = (word or "").strip().lower()
    if not w:
        return out
    toks = re.findall(r"[a-z']+", low)
    if w in toks:
        return out
    for t in set(toks):
        if t in _COMMON_WORDS:
            continue
        if len(t) == len(w) and _edit_dist(t, w) == 1:
            out.append(_err(
                "拼写", t, w,
                f"「{t}」看起来是「{w}」的拼写错误，请确认目标词拼写：{w}。",
                _span_of(raw, low, t), w, "medium"))
            break
    return out


def _task_issues(raw, low, word, task_grammar, task_prompt):
    """任务要求检查（软问题，不进错题本，但会阻止 PASS）。

    与硬错误区分：硬错误是「语言本身错了」；这里是「题目没完成」。
    例如要求频率副词但没用 → 句子可理解，但任务未完成，不能算 PASS。
    """
    issues = []
    req = " ".join([task_grammar or "", task_prompt or ""]).lower()

    # 频率副词
    if any(k in req for k in _REQ_FREQ):
        toks = low.split()
        has_freq = any(f in toks for f in _FREQ_ADV) or bool(_FREQ_RE.search(low))
        if not has_freq:
            issues.append({
                "type": "任务要求", "where": "（频率副词）", "correct": "",
                "explanation": "题目要求使用频率副词（always / usually / often / "
                               "sometimes 等），但你的句子里没有。请加上一个频率副词，"
                               "例如：I usually work for a small company.",
                "severity": "medium"})

    # 一般现在时 vs 过去时间词
    if any(k in req for k in _REQ_PRESENT):
        if _PAST_MARKER.search(low):
            issues.append({
                "type": "任务要求", "where": "（时态）", "correct": "",
                "explanation": "题目是一般现在时，但句子出现了过去时间词"
                               "（yesterday / ago / last …），时态不一致。",
                "severity": "medium"})

    # 目标词是否用到
    w = (word or "").strip().lower()
    if w:
        toks = re.findall(r"[a-z']+", low)
        used = w in toks or any(t.startswith(w) for t in toks)
        if not used:
            issues.append({
                "type": "任务要求", "where": "（目标词）", "correct": "",
                "explanation": f"没有在句子里用到目标词「{w}」。请围绕「{w}」来写。",
                "severity": "medium"})
    return issues


# 规则按优先级排列：越靠前越具体，重叠区间里先命中的胜出
RULES = [
    _r_be_verb,          # I am agree / He is go
    _r_very_verb,        # I very like
    _r_very_much_order,  # like very much this movie
    _r_suggest_to,       # suggested me to go
    _r_look_forward,     # look forward to see
    _r_make_let_to,      # made me to cry
    _r_redundant,        # can able to / discuss about / ...
    _r_although_but,     # Although ..., but ...
    _r_aux_agree,        # he don't
    _r_subj_verb,        # he go -> he goes
    _r_past_tense,       # yesterday + go -> went
    _r_verb_pattern,     # like swim -> like swimming
    _r_prep,             # at the morning
    _r_colloc,           # go work -> go to work
    _r_article,          # a apple -> an apple
    _r_plural,           # two friend -> two friends
]


def _dedupe(errors):
    """去掉区间重叠的错误；越靠前（越具体）的规则胜出，同位置取更长的区间。"""
    accepted = []
    for e in sorted(errors, key=lambda x: (x["span"][0], -(x["span"][1] - x["span"][0]))):
        a, b = e["span"]
        if any(a < y["span"][1] and y["span"][0] < b for y in accepted):
            continue
        accepted.append(e)
    return sorted(accepted, key=lambda x: x["span"][0])


def _apply(s, errors):
    """按区间从右往左把修正写回原句。"""
    out = s
    for e in sorted(errors, key=lambda x: -x["span"][0]):
        a, b = e["span"]
        repl = e["repl"]
        if a == 0 and out[:1].isupper() and repl[:1].islower():
            repl = repl[0].upper() + repl[1:]
        out = out[:a] + repl + out[b:]
    return re.sub(r"\s{2,}", " ", out).replace(" ,", ",").replace(" .", ".").strip()


# =====================================================================
# 优化建议（只在**没有错误**时给出，绝不判错、绝不扣分）
# =====================================================================

_DEGREE = ("really", "very much", "a lot", "so much", "deeply", "truly",
           "quite", "absolutely", "definitely", "extremely", "particularly",
           "especially", "greatly", "totally", "pretty")
_CONNECT = ("because", "so", "and", "but", "although", "when", "if", "which",
            "that", "while", "after", "before", "since", "however", "though")


def _expand_sample(s, low):
    """给过短的句子生成一条「扩写」示范。

    只做拼接、不臆造内容：把该补的那一节留成占位（...），
    让学习者自己填，避免给出一句跟他真实想法无关的假句子。
    """
    base = s.rstrip().rstrip(".!?")
    has_time = bool(re.search(
        r"\b(every\s*day|every\s*morning|every\s*evening|usually|often|"
        r"sometimes|always|never|in\s+the\s+\w+|at\s+\w+|on\s+\w+days?|"
        r"after\s+\w+|before\s+\w+)\b", low))
    has_place = bool(re.search(r"\b(at|in|on|to)\s+(the\s+)?[a-z]+\b", low))
    n = len(re.findall(r"[A-Za-z']+", s))
    goal = f"现在 {n} 个词，补到 10 个词以上就能练到更多结构。"
    # 优先级：先补原因（信息量最大）；已有连接词就补时间；再补地点
    if not any(re.search(r"\b" + c + r"\b", low) for c in _CONNECT):
        return base + " because ...", f"在 because 后面补一句原因，句子立刻多出半句信息。{goal}"
    if not has_time:
        return base + " every day.", (
            f"补一个时间或频率（every day / usually / in the morning），"
            f"让句子更像真实表达。{goal}")
    if not has_place:
        return base + " at ...", (
            f"补上地点（at home / in the park），句子会更完整。{goal}")
    return "", ""


def _optimizations(s, low):
    """正确句的可优化表达（仅供参考，不影响判定与分数）。

    返回两类：
      - 润色类：{where, suggestion, reason} —— 前端按「建议」展示
      - 扩写类：{kind:'expand', sample, note} —— 前端按「扩写：」展示
    两类都只影响展示，不进错误本、不扣分。
    """
    opts = []
    if s and s[-1] not in ".!?":
        opts.append({
            "where": "句末", "suggestion": s + ".",
            "reason": "英文句子末尾要加句号。这是书写习惯，不是语法错误。",
        })
    m = re.search(r"\b(i|you|we|they|he|she)\s+(like|likes|love|loves|enjoy|enjoys)\b",
                  low)
    if m and not any(d in low for d in _DEGREE):
        verb = m.group(2)
        a, b = m.start(2), m.end(2)
        more = "a lot" if verb.endswith("s") else "very much"
        opts.append({
            "where": verb,
            "suggestion": s[:a] + "really " + s[a:b] + s[b:],
            "reason": f"原句完全正确。{verb} 前面加 really 只是让语气更强一点，"
                      f"不代表原句有错；也可以说 {verb} ... {more}。",
        })
    # 扩写线：少于 10 个词就该练长一点。语法没错也要提醒，
    # 否则学习者会一直停在「主谓宾」三词句上，永远长不出从句。
    words = re.findall(r"[A-Za-z']+", s)
    if len(words) < 10 and not any(re.search(r"\b" + c + r"\b", low)
                                   for c in _CONNECT):
        sample, note = _expand_sample(s, low)
        if sample:
            opts.append({
                "kind": "expand",
                "where": "整句",
                "sample": sample,
                "note": note or f"现在 {len(words)} 个词，写到 10 个词以上就能练到更多结构。",
            })
    return opts[:2]


# =====================================================================
# 分析入口（纯函数，不碰数据库，便于测试）
# =====================================================================

def analyze(sentence, word="", task_grammar="", task_prompt=""):
    """纯本地分析，返回结构化批改结果（不写库、无副作用）。

    判定三态：
      PASS          —— 语言基本正确 + 句子完整 + 目标词正确 + 题目要求基本完成
      NEEDS_REVIEW  —— 明确问题：拼写/语法/中英混杂/任务要求未完成/明显表达问题
      UNCERTAIN     —— 句子过短、信息不足，无法可靠判断（不强行 PASS）

    原则：没有检测到错误 + 任务要求满足 + 目标词正确 + 句子基本完整
          → PASS；否则不能 PASS。
    """
    raw = _normalize((sentence or "").strip())
    if not raw:
        return None
    low = _safe_lower(raw)

    raw_errors = []
    for rule in RULES:
        try:
            raw_errors.extend(rule(raw, low) or [])
        except Exception as e:  # 单条规则出错不影响整体批改
            print("[ai_service] 规则 %s 执行失败(已跳过): %s" % (rule.__name__, e))
    # —— 上下文相关的新检查：拼写 / 中文 / 句法完整性 / 冠词 ——
    # 注：_r_i_l（句首小写 l 当 I）已按需求停用。
    # 理由：学习者用手机输入时 l/I 常常只是输入习惯，判成拼写错会让人
    # 把注意力放在大小写上，而不是真正该练的表达。函数保留不删，
    # 将来想恢复只要把下面这行注释放开即可。
    try:
        raw_errors.extend(_r_chinese(raw, low) or [])
        # raw_errors.extend(_r_i_l(raw, low) or [])   # 已停用：不抓 I/l 混淆
        raw_errors.extend(_r_no_verb(raw, low) or [])
        raw_errors.extend(_r_overseas_dept(raw, low) or [])
        raw_errors.extend(_r_target_spelling(raw, low, word) or [])
    except Exception as e:
        print("[ai_service] 新增规则执行失败(已跳过): %s" % e)
    errors = _dedupe(raw_errors)
    # 中文混杂必须保留：它区间小，会被「整句缺谓语」这类大区间错误在
    # 去重时吞掉。这里兜底再确认一次，确保中英混杂永远被识别。
    if _CJK.search(raw) and not any(e["type"] == "其他" for e in errors):
        for m in _CJK.finditer(raw):
            s = m.group(0)
            errors.append(_err(
                "其他", s, s,
                f"句子里混入了中文「{s}」。英语造句请保持全英文；"
                f"如果想表达这个意思，请换成英文（例如 city center）。",
                (m.start(), m.end()), s, "heavy"))
        errors.sort(key=lambda x: x["span"][0])
    corrected = _apply(raw, errors) if errors else raw

    # 任务要求检查（软问题，不进错题本，但阻止 PASS）
    task_issues = _task_issues(raw, low, word, task_grammar, task_prompt)

    # —— 评分与判定：严格区分「语言错误」「任务未完成」「不确定」——
    if errors:
        score = BASE_SCORE - sum(PENALTY.get(e["severity"], 20) for e in errors)
        score = max(MIN_SCORE, min(100, score))
        status = "NEEDS_REVIEW"
        ok = False
    elif task_issues:
        # 任务要求没完成：不判 PASS，但也不当成语法错误重扣
        score = min(84, BASE_SCORE - 6)
        status = "NEEDS_REVIEW"
        ok = False
    else:
        score = BASE_SCORE
        words = re.findall(r"[A-Za-z']+", raw)
        if len(words) >= 8:
            score += 5
        if any(re.search(r"\b" + c + r"\b", low) for c in _CONNECT):
            score += 5
        score = min(100, score)
        # 过短 / 信息不足：不确定，不要强行 PASS
        if len(words) <= 2:
            status = "UNCERTAIN"
            ok = False
        else:
            status = "PASS"
            ok = True

    if errors:
        explanation = "；".join(e["explanation"] for e in errors)
        error_type = errors[0]["type"]
    elif task_issues:
        explanation = "；".join(i["explanation"] for i in task_issues)
        error_type = task_issues[0]["type"]
    else:
        explanation = "没有明显错误。"
        error_type = ""

    return {
        "original": raw,
        "corrected": corrected,
        "ok": ok,
        "score": score,
        "verdict": "正确" if ok else "有错误",
        "level": "基本掌握" if score >= PASS_LINE else "需要改进",
        "status": status,
        "error_type": error_type,
        "explanation": explanation,
        "errors": [{"type": e["type"], "where": e["where"],
                    "correct": e["correct"], "explanation": e["explanation"]}
                   for e in errors],
        "task_issues": [{"type": i["type"], "where": i["where"],
                          "correct": i["correct"], "explanation": i["explanation"]}
                        for i in task_issues],
        "optimizations": _optimizations(raw, low) if ok else [],
    }


# =====================================================================
# 批改主入口：分析 + 落库 + 错题本
# =====================================================================

def correct_sentence(sentence, stage=0, week=3, day=1, word="", task_key="",
                     task_grammar="", task_prompt=""):
    """纯本地批改主入口。

    - sentences：每次作答追加一行，attempt 递增，**绝不覆盖上一次答案**。
    - errors（错题本）：只有真错才写；同一个 word + 错误片段累加 times；
      该词后来写对了只把 fixed 标成 1，**不删除**历史记录。
    - task_issues（任务未完成，如缺频率副词）：只展示、不进错题本、不污染 SRS。
    全程无任何 AI 参与。
    """
    res = analyze(sentence, word, task_grammar, task_prompt)
    if res is None:
        return None

    word = (word or "").strip()
    task_key = (task_key or "").strip()
    now = ts()

    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(attempt) a FROM sentences WHERE stage=? AND week=? AND day=?"
        " AND task_key=?", (stage, week, day, task_key)).fetchone()
    attempt = int(row["a"] or 0) + 1 if row else 1

    sentence_id = insert_get_id(
        conn,
        "INSERT INTO sentences (stage, week, day, word, task_key, attempt,"
        " original, corrected, error_type, explanation, ai_source, good, score,"
        " verdict, errors_json, opts_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (stage, week, day, word, task_key, attempt, res["original"],
         res["corrected"], res["error_type"], res["explanation"], "rule",
         1 if res["ok"] else 0, res["score"], res["verdict"],
         json.dumps(res["errors"], ensure_ascii=False),
         json.dumps(res["optimizations"], ensure_ascii=False), now))

    # 错题本：只有真错才写入；同一个 (词 + 错误类型 + 归一化错误片段) 合并成一条，只累加次数
    bank_ids = []
    for e in res["errors"]:
        where, etype = e["where"], e["type"]
        norm = norm_error_text(where)
        exist = conn.execute(
            "SELECT id, times FROM errors WHERE source='sentence' AND word=?"
            " AND error_type=? AND norm_text=?", (word, etype, norm)).fetchone()
        if exist:
            conn.execute(
                "UPDATE errors SET times=?, last_at=?, sentence_text=? WHERE id=?",
                (int(exist["times"] or 0) + 1, now, res["original"], exist["id"]))
            bank_id = exist["id"]
        else:
            bank_id = insert_get_id(
                conn,
                "INSERT INTO errors (error_type, original, corrected, explanation,"
                " source, created_at, word, task_key, error_text, norm_text, level,"
                " sentence_text, times, first_at, last_at, fixed)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (etype, where, e["correct"], e["explanation"], "sentence", now,
                 word, task_key, where, norm, LEVEL_MEMORY, res["original"],
                 1, now, now, 0))
        bank_ids.append(bank_id)
        # 晋级判定：近30天同一个错出现 ≥2 次才算「薄弱项🟡」；只错一次只累加次数，仍标「记忆项🔵」。
        # 🔴阻塞项由诊断侧单独标记，这里不自动升级，避免偶发错误被放大成「必须调整学习」。
        lvl = (LEVEL_WEAK if recent_error_count(conn, word, etype, norm,
                                                days_ago_str(30)) >= 2
               else LEVEL_MEMORY)
        try:
            conn.execute(
                "UPDATE errors SET level=? WHERE word=? AND error_type=? AND norm_text=?",
                (lvl, word, etype, norm))
        except Exception:
            pass

    # 写对了 → 把该词此前未改正的错题标记为已改正（不删除）
    fixed_ids = []
    if res["ok"] and word:
        for r in conn.execute(
                "SELECT id FROM errors WHERE source='sentence' AND word=?"
                " AND fixed=0", (word,)).fetchall():
            conn.execute("UPDATE errors SET fixed=1, fixed_at=? WHERE id=?",
                         (now, r["id"]))
            fixed_ids.append(r["id"])

    # 有硬错误 → 安排一张「高频错误」复习卡。
    # 注意：task_issues（如缺频率副词）是任务未完成，不算语言错误，不污染 SRS。
    hard_review = bool(res["errors"])
    if hard_review:
        schedule_review(conn, "error", res["error_type"],
                        f"改正错误：{res['corrected']}",
                        res["corrected"], stage, week, day)

    conn.commit()
    conn.close()

    out = dict(res)
    out.update({
        "sentence_id": sentence_id,
        "attempt": attempt,
        "word": word,
        "task_key": task_key,
        "created_at": now,
        "good": res["ok"],
        "ai_source": "rule",
        "needs_review": hard_review or bool(res["task_issues"]),
        "to_error_bank": hard_review,
        "error_bank_ids": bank_ids,
        "fixed_error_ids": fixed_ids,
        # 润色类用 reason，扩写类用 note，两种字段都要能取到
        "expand_hint": (res["optimizations"][0].get("reason")
                        or res["optimizations"][0].get("note") or "")
                       if res["optimizations"] else "",
    })
    return out


def attempts_of(conn, stage, week, day, task_key):
    """取某道题的全部作答历史（按 attempt 升序）。"""
    rows = conn.execute(
        "SELECT id, attempt, original, corrected, score, verdict, good,"
        " error_type, errors_json, opts_json, created_at"
        " FROM sentences WHERE stage=? AND week=? AND day=? AND task_key=?"
        " ORDER BY attempt, id",
        (stage, week, day, task_key)).fetchall()
    out = []
    for r in rows:
        try:
            errs = json.loads(r["errors_json"] or "[]")
        except Exception:
            errs = []
        try:
            opts = json.loads(r["opts_json"] or "[]")
        except Exception:
            opts = []
        out.append({
            "id": r["id"], "attempt": r["attempt"], "sentence": r["original"],
            "corrected": r["corrected"], "score": r["score"],
            "verdict": r["verdict"] or ("正确" if r["good"] else "有错误"),
            "ok": bool(r["good"]), "error_type": r["error_type"],
            "errors": errs, "optimizations": opts, "created_at": r["created_at"],
        })
    return out


def today_attempts(conn, stage, week, day, since):
    """当天全部作答，按 task_key 分组（用于页面刷新后回填历史）。"""
    rows = conn.execute(
        "SELECT id, word, task_key, attempt, original, corrected, score,"
        " verdict, good, error_type, errors_json, opts_json, created_at"
        " FROM sentences WHERE stage=? AND week=? AND day=? AND created_at>=?"
        " ORDER BY id", (stage, week, day, since)).fetchall()
    groups = {}
    for r in rows:
        tk = r["task_key"] or f"free:{r['id']}"
        try:
            errs = json.loads(r["errors_json"] or "[]")
        except Exception:
            errs = []
        try:
            opts = json.loads(r["opts_json"] or "[]")
        except Exception:
            opts = []
        groups.setdefault(tk, {"task_key": tk, "word": r["word"] or "",
                               "attempts": []})
        groups[tk]["attempts"].append({
            "id": r["id"], "attempt": r["attempt"], "sentence": r["original"],
            "corrected": r["corrected"], "score": r["score"],
            "verdict": r["verdict"] or ("正确" if r["good"] else "有错误"),
            "ok": bool(r["good"]), "error_type": r["error_type"],
            "errors": errs, "optimizations": opts, "created_at": r["created_at"],
        })
    return list(groups.values())
