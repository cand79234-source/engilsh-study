"""数据库层 - SQLite（本地默认）或 PostgreSQL（Neon，设 DATABASE_URL 时启用）。

所有 SQL 统一用 `?` 占位符；Postgres 模式下由适配层自动转成 `%s`，
并把 `INSERT OR IGNORE` 转成 `ON CONFLICT ... DO NOTHING`，因此业务代码无需改动。
"""
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

DB_PATH = os.environ.get("EOS_DB", os.path.join(os.path.dirname(__file__), "..", "data", "english_os.db"))

# ---- Postgres 适配（DATABASE_URL 存在时启用，Neon 等托管库）----
DATABASE_URL = os.environ.get("DATABASE_URL")


def _using_pg():
    """运行时判断是否使用 Postgres：每次连接重新读取环境变量，避免导入期一次性决定后无法纠正。
    生产环境若漏配 DATABASE_URL，由下方 FATAL 校验拦截，绝不静默回落 SQLite。"""
    return bool(os.environ.get("DATABASE_URL"))


# 生产环境防护：运行在 Render 却没配 DATABASE_URL → 直接拒绝启动。
# 否则会静默写入临时 SQLite（Render 重启/重部署即丢数据），且日志毫无提示。
if os.environ.get("RENDER") and not os.environ.get("DATABASE_URL"):
    raise SystemExit(
        "[db] FATAL: 检测到运行环境为 Render，但未设置 DATABASE_URL。\n"
        "为避免数据写入临时 SQLite（重启即丢），已拒绝启动。\n"
        "请在 Render 控制台 Environment 中手动粘贴 Neon 连接串(postgresql://...)，然后 Redeploy。"
    )


def _mask_url(u):
    """隐藏连接串里的密码，便于安全打印日志。"""
    if not u:
        return ""
    import re as _re
    return _re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", u)


# 启动时明确打印当前连的是哪个库。没有这行的话，一旦 Render 上漏配 DATABASE_URL，
# 代码会静默回落到本地 SQLite（重启即丢数据），而日志里完全看不出来。
if _using_pg():
    print("[db] 数据库 = PostgreSQL (Neon): %s" % _mask_url(DATABASE_URL))
else:
    print("[db] ⚠️ 未检测到 DATABASE_URL，当前使用本地 SQLite 文件: %s" % DB_PATH)
    print("[db]    若这是部署环境（Render），数据会在重启/重新部署后丢失，请检查环境变量配置。")

# INSERT OR IGNORE 的冲突列（= 各表 UNIQUE 约束列）
_IGNORE_CONFLICT = {
    "dictionary": "word",
    "example_sentences": "word, sentence",
    "collocations": "word, phrase",
}


def _tr(sql):
    """把 sqlite 风格 SQL 转成 Postgres 风格。sqlite 模式不会调用本函数。"""
    def repl(m):
        tbl, cols, vals = m.group(1), m.group(2), m.group(3)
        conflict = _IGNORE_CONFLICT.get(tbl, cols.split(",")[0].strip())
        return f"INSERT INTO {tbl} ({cols}) VALUES ({vals}) ON CONFLICT ({conflict}) DO NOTHING"
    sql = re.sub(
        r"INSERT OR IGNORE INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]*)\)",
        repl, sql, flags=re.IGNORECASE)
    sql = sql.replace("?", "%s")
    sql = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
    return sql


def _norm(params):
    if params is None:
        return ()
    if isinstance(params, (list, tuple)):
        return tuple(params)
    return (params,)


class _PGCursor:
    """包装 psycopg2 原生 cursor：自动翻译占位符，行以 DictRow 返回。"""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=None):
        self._raw.execute(_tr(sql), _norm(params))
        return self

    def executescript(self, sql):
        self._raw.execute(_tr(sql))
        return self

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    @property
    def lastrowid(self):
        return self._raw.lastrowid

    def __iter__(self):
        return iter(self._raw)


class _PGConn:
    """包装 psycopg2 连接，暴露 sqlite3 风格的 .execute / .cursor / .commit。"""
    def __init__(self, raw):
        self._raw = raw
        try:
            from psycopg2.extras import DictCursor
            self._raw.cursor_factory = DictCursor
        except Exception:
            pass

    def execute(self, sql, params=None):
        cur = self._raw.cursor()
        cur.execute(_tr(sql), _norm(params))
        return _PGCursor(cur)

    def executemany(self, sql, params_seq):
        cur = self._raw.cursor()
        cur.executemany(_tr(sql), params_seq)
        return self

    def cursor(self):
        return _PGCursor(self._raw.cursor())

    def executescript(self, sql):
        cur = self._raw.cursor()
        cur.execute(_tr(sql))
        return _PGCursor(cur)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, v):
        pass

ERROR_TYPES = [
    "冠词", "介词", "时态", "主谓一致", "单复数", "词序",
    "固定搭配", "词性", "拼写", "句型", "其他",
]

STAGES = [
    {"stage": 0, "name": "阶段0｜A1+→A2", "weeks": 12, "months": "第0-3月"},
    {"stage": 1, "name": "阶段1｜A2→A2+", "weeks": 12, "months": "第3-6月"},
    {"stage": 2, "name": "阶段2｜A2+→B1", "weeks": 12, "months": "第6-10月"},
    {"stage": 3, "name": "阶段3｜B1→B1+", "weeks": 12, "months": "第10-14月"},
    {"stage": 4, "name": "阶段4｜B1+→B2-", "weeks": 12, "months": "第14-18月"},
    {"stage": 5, "name": "阶段5｜B2-→B2", "weeks": 12, "months": "第18-22月"},
]

# 阶段0 的 12 个主题周（用户可自行编辑覆盖）
SEED_WEEKS = [
    (0, 1, "家庭与人际", "be动词、人称代词、物主代词"),
    (0, 2, "工作与日常", "一般现在时、频率副词"),
    (0, 3, "爱好与休闲", "like / enjoy / hate + doing"),
    (0, 4, "时间与生活", "一般过去时"),
    (0, 5, "交通与出行", "will / be going to"),
    (0, 6, "购物与消费", "可数/不可数、some/any/much/many"),
    (0, 7, "地点与城市", "there be、方位介词"),
    (0, 8, "天气与季节", "形容词/副词、比较级/最高级"),
    (0, 9, "食物与饮食", "can"),
    (0, 10, "健康与身体", "must / should / have to"),
    (0, 11, "综合描述", "that / which / who / where / when"),
    (0, 12, "综合复习", "基础被动语态 + 全语法抽测"),
]

# 阶段1-5 的 12 个主题周骨架（标题 + 语法重点；词汇由用户自行导入覆盖）
SEED_WEEKS_STAGES15 = {
    1: [
        (1, 1, "日常与习惯", "一般现在时、频率副词扩展"),
        (1, 2, "人与关系", "there be、物主代词、所有格"),
        (1, 3, "过去事件", "一般过去时规则/不规则动词"),
        (1, 4, "计划与意图", "going to / would like to"),
        (1, 5, "能力与请求", "can / could / may 请求与许可"),
        (1, 6, "建议与义务", "should / must / have to"),
        (1, 7, "比较与选择", "比较级 / 最高级"),
        (1, 8, "经历与兴趣", "like / enjoy / hate + doing"),
        (1, 9, "时间与约定", "时间介词 in/on/at"),
        (1, 10, "地点与方向", "方位介词、问路指路"),
        (1, 11, "购物与议价", "How much / How many、量词"),
        (1, 12, "阶段复习", "A2 语法综合抽测"),
    ],
    2: [
        (2, 1, "生活变化", "现在完成时 for/since/ever/never"),
        (2, 2, "经验谈", "现在完成时 vs 一般过去时"),
        (2, 3, "未来场景", "will / be going to 辨析"),
        (2, 4, "条件与假设", "if 条件句 0/1 型"),
        (2, 5, "被动与过程", "一般现在/过去时被动语态"),
        (2, 6, "定语从句入门", "who / which / that"),
        (2, 7, "不定代词", "some/any/no/every + body/one/thing"),
        (2, 8, "情态动词深入", "might / should have / must have"),
        (2, 9, "间接引语", "say/tell 引述、时态后退"),
        (2, 10, "连词与逻辑", "although / because / so / however"),
        (2, 11, "描述感受", "look/sound/feel/taste + 形容词"),
        (2, 12, "阶段复习", "A2+ 语法综合抽测"),
    ],
    3: [
        (3, 1, "完成时态", "现在完成时 / 过去完成时"),
        (3, 2, "虚拟条件", "if 条件句 2/3 型"),
        (3, 3, "被动进阶", "各时态被动语态"),
        (3, 4, "定语从句扩展", "关系副词 where/when/whose"),
        (3, 5, "非谓语动词", "动名词 / 不定式作主宾"),
        (3, 6, "used to & be used to", "过去习惯 vs 习惯于"),
        (3, 7, "报告与传闻", "reported speech 入门"),
        (3, 8, "强调与倒装", "It is ... that / 否定倒装"),
        (3, 9, "语篇连接", "furthermore / nevertheless / therefore"),
        (3, 10, "抽象描述", "so...that / too...to / enough to"),
        (3, 11, "建议与委婉", "I suggest / If I were you"),
        (3, 12, "阶段复习", "B1 语法综合抽测"),
    ],
    4: [
        (4, 1, "完成进行时", "现在完成进行 / 过去完成进行"),
        (4, 2, "情态动词推测", "must/can't/might have done"),
        (4, 3, "分词结构", "现在分词 / 过去分词作状语"),
        (4, 4, "名词性从句", "that / what / whether 从句"),
        (4, 5, "条件与混合", "混合条件句、含蓄条件"),
        (4, 6, "定语从句高级", "非限定性定语从句"),
        (4, 7, "倒装与省略", "not only / never / hardly"),
        (4, 8, "虚拟语气", "wish / if only / as if"),
        (4, 9, "强调结构", "cleft sentences / fronting"),
        (4, 10, "语体与措辞", "正式 vs 非正式、委婉表达"),
        (4, 11, "复杂句整合", "多层从句、主谓一致长句"),
        (4, 12, "阶段复习", "B1+ 语法综合抽测"),
    ],
    5: [
        (5, 1, "高阶时态", "将来完成 / 将来完成进行"),
        (5, 2, "情态与义务", "should have / needn't have"),
        (5, 3, "分词独立主格", "独立主格结构"),
        (5, 4, "名词从句综合", "主语/宾语/表语/同位语从句"),
        (5, 5, "虚拟高级", "错综虚拟、含蓄虚拟"),
        (5, 6, "强调与否定", "强调句变形、部分否定"),
        (5, 7, "学术表达", "论文常用连接与客观表述"),
        (5, 8, "报告与转述", "复杂间接引语、转述动词"),
        (5, 9, "修辞与逻辑", "平行结构、省略与替代"),
        (5, 10, "长难句拆解", "插入语、同位语、分词短语"),
        (5, 11, "职场沟通", "邮件、会议、谈判常用表达"),
        (5, 12, "阶段复习", "B2 语法综合抽测"),
    ],
}

# Week 3 示例词汇（爱好与休闲）—— 供用户编辑页使用
SEED_WEEK3_VOCAB = [
    {"word": "hobby", "meaning": "爱好", "pos": "名词", "collocation": "a hobby / my hobby", "example": "Reading is my hobby."},
    {"word": "relax", "meaning": "放松", "pos": "动词", "collocation": "relax at home / relax after work", "example": "I like to relax on weekends."},
    {"word": "enjoy", "meaning": "享受，喜欢", "pos": "动词", "collocation": "enjoy doing sth", "example": "I enjoy listening to music."},
    {"word": "practice", "meaning": "练习", "pos": "动词/名词", "collocation": "practice the guitar / practice every day", "example": "I practice English every morning."},
    {"word": "exercise", "meaning": "锻炼，练习", "pos": "动词/名词", "collocation": "do exercise / exercise daily", "example": "Exercise keeps me healthy."},
    {"word": "sing", "meaning": "唱歌", "pos": "动词", "collocation": "sing a song / love singing", "example": "She sings very well."},
    {"word": "dance", "meaning": "跳舞", "pos": "动词/名词", "collocation": "dance to music / go dancing", "example": "We dance at the party."},
    {"word": "draw", "meaning": "画画", "pos": "动词", "collocation": "draw a picture", "example": "I draw pictures in my free time."},
    {"word": "travel", "meaning": "旅行", "pos": "动词/名词", "collocation": "travel abroad / go traveling", "example": "I want to travel around the world."},
    {"word": "cook", "meaning": "做饭", "pos": "动词", "collocation": "cook dinner / cooking class", "example": "I cook dinner at 7."},
    {"word": "game", "meaning": "游戏", "pos": "名词", "collocation": "play games / video game", "example": "We play games together."},
    {"word": "free", "meaning": "空闲的，自由的", "pos": "形容词", "collocation": "free time / be free", "example": "I am free this afternoon."},
    {"word": "interesting", "meaning": "有趣的", "pos": "形容词", "collocation": "an interesting book / very interesting", "example": "The movie is interesting."},
    {"word": "fun", "meaning": "有趣的，乐趣", "pos": "名词/形容词", "collocation": "have fun / a fun game", "example": "We had fun yesterday."},
    {"word": "weekend", "meaning": "周末", "pos": "名词", "collocation": "at the weekend / last weekend", "example": "I relax at the weekend."},
    {"word": "together", "meaning": "一起", "pos": "副词", "collocation": "do sth together / work together", "example": "We study together."},
    {"word": "like", "meaning": "喜欢", "pos": "动词", "collocation": "like doing / like to do", "example": "I like swimming."},
    {"word": "hate", "meaning": "讨厌", "pos": "动词", "collocation": "hate doing sth", "example": "I hate getting up early."},
    {"word": "movie", "meaning": "电影", "pos": "名词", "collocation": "watch a movie / go to the movies", "example": "I watch a movie tonight."},
    {"word": "music", "meaning": "音乐", "pos": "名词", "collocation": "listen to music / play music", "example": "I listen to music every day."},
]


def insert_get_id(conn, sql, params=None):
    """插入一行并返回新 id。

    SQLite 用 lastrowid；PostgreSQL 下 psycopg2 的 lastrowid 恒为 0，
    必须走 `INSERT ... RETURNING id`，否则拿到的 id 永远是 0。
    """
    if _using_pg():
        cur = conn._raw.cursor()
        cur.execute(_tr(sql).rstrip().rstrip(";") + " RETURNING id", _norm(params))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    cur = conn.execute(sql, params)
    return cur.lastrowid


def get_conn():
    if _using_pg():
        import psycopg2
        raw = psycopg2.connect(DATABASE_URL, connect_timeout=15)
        return _PGConn(raw)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS progress (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        stage INTEGER NOT NULL DEFAULT 0,
        week INTEGER NOT NULL DEFAULT 1,
        day INTEGER NOT NULL DEFAULT 1,
        last_activity TEXT DEFAULT 'vocab',
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS weeks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage INTEGER NOT NULL,
        week_no INTEGER NOT NULL,
        title TEXT NOT NULL,
        grammar TEXT DEFAULT '',
        vocab_json TEXT DEFAULT '[]',
        topics TEXT DEFAULT '',
        UNIQUE(stage, week_no)
    );

    -- 每日学习项：某个 Day 下的词/句/语法掌握程度
    CREATE TABLE IF NOT EXISTS day_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage INTEGER NOT NULL,
        week INTEGER NOT NULL,
        day INTEGER NOT NULL,
        kind TEXT NOT NULL,           -- 'vocab' | 'sentence_prompt' | 'grammar'
        ref_key TEXT,                 -- 词文本 / 句子提示
        payload_json TEXT DEFAULT '{}',
        mastered INTEGER DEFAULT 0,   -- 0 未掌握 1 学习中 2 已掌握
        created_at TEXT
    );

    -- 用户造句 + 本地规则批改
    -- 每次作答都追加一行（同一道题可提交多次：attempt 递增，历史永不覆盖）
    CREATE TABLE IF NOT EXISTS sentences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage INTEGER NOT NULL,
        week INTEGER NOT NULL,
        day INTEGER NOT NULL,
        word TEXT DEFAULT '',           -- 本题对应的单词（组合题为多个词，空格分隔）
        task_key TEXT DEFAULT '',       -- 前端题目标识：'basic:0' / 'up:2' / 'combo:3'
        attempt INTEGER DEFAULT 1,      -- 第几次作答（同一 task_key 内递增）
        original TEXT NOT NULL,
        corrected TEXT DEFAULT '',
        error_type TEXT DEFAULT '',
        explanation TEXT DEFAULT '',
        ai_source TEXT DEFAULT '',     -- 恒为 'rule'（纯本地，无 AI）
        good INTEGER DEFAULT 0,        -- 是否完全正确
        score INTEGER DEFAULT 0,       -- 0-100
        verdict TEXT DEFAULT '',       -- '正确' / '有错误'
        errors_json TEXT DEFAULT '[]', -- 结构化错误明细
        opts_json TEXT DEFAULT '[]',   -- 可优化表达
        created_at TEXT
    );

    -- 错误库（长期累积，兼作错题本）
    CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        error_type TEXT NOT NULL,
        original TEXT NOT NULL,        -- 错误片段（如 very like）
        corrected TEXT NOT NULL,       -- 正确片段（如 really like）
        explanation TEXT DEFAULT '',
        source TEXT DEFAULT '',        -- 来自造句 / 复习 / 周测
        created_at TEXT,
        word TEXT DEFAULT '',          -- 出错的单词（错题本按词聚合）
        task_key TEXT DEFAULT '',
        error_text TEXT DEFAULT '',    -- 错误片段（与 original 同，便于精确去重）
        sentence_text TEXT DEFAULT '', -- 出错的完整原句
        times INTEGER DEFAULT 1,       -- 出现次数（同一 word+error_text 累加）
        first_at TEXT,                 -- 第一次错误时间
        last_at TEXT,                  -- 最近一次错误时间
        fixed INTEGER DEFAULT 0,       -- 0 未改正 1 已改正
        fixed_at TEXT DEFAULT ''       -- 改正时间
    );

    -- SRS 复习卡
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,            -- 'vocab'|'collocation'|'sentence'|'grammar'|'error'
        ref_key TEXT,                  -- 内容标识
        prompt TEXT DEFAULT '',        -- 复习时的提示
        answer TEXT DEFAULT '',        -- 参考答案/判定依据
        stage INTEGER NOT NULL,
        week INTEGER NOT NULL,
        day INTEGER NOT NULL,
        ease REAL DEFAULT 2.5,
        interval REAL DEFAULT 0,
        reps INTEGER DEFAULT 0,
        next_due TEXT,
        last_score INTEGER DEFAULT -1, -- -1未复习, 0错, 1对
        total_correct INTEGER DEFAULT 0,
        total_wrong INTEGER DEFAULT 0,
        last_reviewed TEXT,            -- 最近一次复习时间(用于"久未复习"排序)
        created_at TEXT,
        UNIQUE(kind, ref_key, prompt)
    );

    -- 造句五星（主动输出熟练度）
    -- 与 reviews 的 SRS 完全独立：SRS 记「记不记得」，五星记「能不能主动用」。
    -- 同一个词可同时存在：vocab SRS 卡 + 五星记录 + listening 卡，三者互不干扰。
    CREATE TABLE IF NOT EXISTS word_output (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,
        stars INTEGER DEFAULT 0,        -- 0-5 主动输出熟练度
        total_attempts INTEGER DEFAULT 0,
        last_result TEXT DEFAULT '',    -- 'pass'|'needs_review'|'uncertain'
        last_score INTEGER DEFAULT 0,
        first_at TEXT,
        last_at TEXT,
        updated_at TEXT,
        UNIQUE(word)
    );

    -- 周测 / 阶段测试
    CREATE TABLE IF NOT EXISTS quizzes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT DEFAULT 'weekly',    -- 'weekly' | 'stage'
        stage INTEGER NOT NULL,
        week INTEGER NOT NULL,
        score INTEGER DEFAULT 0,
        passed INTEGER DEFAULT 0,
        detail_json TEXT DEFAULT '[]',
        created_at TEXT
    );

    -- 学习历史（追加式）
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        stage INTEGER NOT NULL,
        week INTEGER NOT NULL,
        day INTEGER NOT NULL,
        action TEXT NOT NULL,          -- 'learn_vocab'|'write_sentence'|'review'|'quiz'...
        detail TEXT DEFAULT '',
        created_at TEXT
    );

    -- ===== 本地词库（内置，纯本地无 AI）=====
    CREATE TABLE IF NOT EXISTS dictionary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,
        phonetic TEXT DEFAULT '',
        meaning TEXT DEFAULT '',          -- 中文释义
        pos TEXT DEFAULT '',              -- 词性（中文：名词/动词…）
        tag TEXT DEFAULT '',              -- zk/gk/cet4 等考试分级标签
        bnc INTEGER DEFAULT 0,            -- 词频(越小越常用)
        theme TEXT DEFAULT '',            -- 主题归属(家庭/爱好…)
        UNIQUE(word)
    );

    -- 系统例句库：一个词多条候选句。系统例句用于"输入展示"，
    -- 与用户自己的造句（sentences 表）严格分开保存。
    CREATE TABLE IF NOT EXISTS example_sentences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,
        sentence TEXT NOT NULL,
        translation TEXT DEFAULT '',      -- 中文翻译
        grammar_tags TEXT DEFAULT '',     -- 语法标签 逗号分隔
        difficulty INTEGER DEFAULT 0,     -- 0基础 1简单 2中等
        source TEXT DEFAULT 'builtin',
        created_at TEXT,
        UNIQUE(word, sentence)
    );

    -- 固定搭配库（内置常见搭配）
    CREATE TABLE IF NOT EXISTS collocations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,
        phrase TEXT NOT NULL,
        meaning TEXT DEFAULT '',
        example TEXT DEFAULT '',
        source TEXT DEFAULT 'builtin',
        UNIQUE(word, phrase)
    );

    """)

    # 初始化进度（仅一条记录）
    if c.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 0:
        now = datetime.now().isoformat()
        c.execute("INSERT INTO progress (id, stage, week, day, last_activity, updated_at) VALUES (1, 0, 3, 1, 'vocab', ?)", (now,))

    # 初始化阶段0的12个主题周
    for (st, w, title, grammar) in SEED_WEEKS:
        if not c.execute("SELECT 1 FROM weeks WHERE stage=? AND week_no=?", (st, w)).fetchone():
            vocab = SEED_WEEK3_VOCAB if (st == 0 and w == 3) else []
            c.execute(
                "INSERT INTO weeks (stage, week_no, title, grammar, vocab_json) VALUES (?,?,?,?,?)",
                (st, w, title, grammar, json.dumps(vocab, ensure_ascii=False)),
            )

    # 初始化阶段1-5的12个主题周骨架（标题+语法，词汇由用户导入覆盖）
    # 结构：{stage: [(stage, week_no, title, grammar), ...]}（stage 与 dict key 一致）
    for st, weeks in SEED_WEEKS_STAGES15.items():
        for (_st, w, title, grammar) in weeks:
            if not c.execute("SELECT 1 FROM weeks WHERE stage=? AND week_no=?", (st, w)).fetchone():
                c.execute(
                    "INSERT INTO weeks (stage, week_no, title, grammar, vocab_json) VALUES (?,?,?,?,'[]')",
                    (st, w, title, grammar),
                )

    # 轻量迁移：给旧库补新列（已存在则跳过）
    # 造句号：多次作答、评分、错题本所需的列，都是后来加的，线上 Neon 老库靠这里补齐
    _ensure_columns(conn, "reviews", {"last_reviewed": "TEXT"})
    _ensure_columns(conn, "sentences", {
        "word": "TEXT DEFAULT ''",
        "task_key": "TEXT DEFAULT ''",
        "attempt": "INTEGER DEFAULT 1",
        "score": "INTEGER DEFAULT 0",
        "verdict": "TEXT DEFAULT ''",
        "errors_json": "TEXT DEFAULT '[]'",
        "opts_json": "TEXT DEFAULT '[]'",
    })
    _ensure_columns(conn, "errors", {
        "word": "TEXT DEFAULT ''",
        "task_key": "TEXT DEFAULT ''",
        "error_text": "TEXT DEFAULT ''",
        "sentence_text": "TEXT DEFAULT ''",
        "times": "INTEGER DEFAULT 1",
        "first_at": "TEXT",
        "last_at": "TEXT",
        "fixed": "INTEGER DEFAULT 0",
        "fixed_at": "TEXT DEFAULT ''",
    })

    # 建索引（必须在 _ensure_columns 补列之后：旧库缺 word/task_key 时，先建索引会报
    # column "word" does not exist，导致启动崩溃、Render 部署失败 update_failed）
    for _idx in (
        "CREATE INDEX IF NOT EXISTS idx_reviews_due ON reviews(next_due)",
        "CREATE INDEX IF NOT EXISTS idx_reviews_kind_due ON reviews(kind, next_due)",
        "CREATE INDEX IF NOT EXISTS idx_word_output_word ON word_output(word)",
        "CREATE INDEX IF NOT EXISTS idx_errors_type ON errors(error_type)",
        "CREATE INDEX IF NOT EXISTS idx_errors_word ON errors(word)",
        "CREATE INDEX IF NOT EXISTS idx_sentences_day ON sentences(stage, week, day)",
        "CREATE INDEX IF NOT EXISTS idx_sentences_task ON sentences(task_key)",
        "CREATE INDEX IF NOT EXISTS idx_dict_word ON dictionary(word)",
        "CREATE INDEX IF NOT EXISTS idx_ex_word ON example_sentences(word)",
        "CREATE INDEX IF NOT EXISTS idx_colloc_word ON collocations(word)",
    ):
        try:
            c.execute(_idx)
        except Exception as _e:
            print("[db.init_db] 建索引跳过:", _idx, _e)

    # 内置基础词库导入（幂等；复用当前 conn，运行时 import 避免循环引用）
    try:
        from seed_builtin import import_into_db
        import_into_db(conn)
    except Exception as e:
        print("[db.init_db] 内置词库导入失败:", e)

    # 全量 ECDICT 词典合并（幂等：仅补充缺失词，不覆盖现有精选词；种子缺失则跳过）
    # 放后台线程执行：init_db 在 main.py 导入期被调用，首次部署要灌 76.8 万行，
    # 若同步阻塞会导致端口迟迟不监听、Render 健康检查失败判定部署失败。
    # 后台合并期间应用已可正常服务；词典只用于音标/词性补全，缺失不影响任何既有功能。
    try:
        import threading
        from seed_ecdict import import_into_db as _ecdict_import

        def _ecdict_bg():
            try:
                print("[db.init_db] ECDICT 词典合并(后台):", _ecdict_import())
            except Exception as e:
                print("[db.init_db] ECDICT 词典合并失败(可忽略):", e)

        threading.Thread(target=_ecdict_bg, daemon=True).start()
    except Exception as e:
        print("[db.init_db] ECDICT 词典合并启动失败(可忽略):", e)

    conn.commit()
    conn.close()


def _ensure_columns(conn, table, columns):
    """给已存在的表补列。columns: {列名: SQL类型}。"""
    if _using_pg():
        existing = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
    else:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, coltype in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def today_str():
    return date.today().isoformat()


def ts():
    return datetime.now().isoformat(timespec="seconds")
