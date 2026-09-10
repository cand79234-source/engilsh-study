"""数据库层 - SQLite（本地默认）或 PostgreSQL（Neon，设 DATABASE_URL 时启用）。

所有 SQL 统一用 `?` 占位符；Postgres 模式下由适配层自动转成 `%s`，
并把 `INSERT OR IGNORE` 转成 `ON CONFLICT ... DO NOTHING`，因此业务代码无需改动。
"""
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

DB_PATH = os.environ.get("EOS_DB", os.path.join(os.path.dirname(__file__), "..", "data", "english_os.db"))

# ------------------------------------------------------------------
# 应用时区（全项目唯一时间基准）
# ------------------------------------------------------------------
# 所有「今天 / 现在」必须走 app_today() / app_now()，不许再直接调用
# date.today() / datetime.now()：后者取的是服务器本地时区，Render 默认是 UTC，
# 和用户实际所在时区差好几个小时，会让「今天学的算昨天」「SRS 到期日错位」
# 「近7/30天统计跨错天」这类问题在跨零点时随机出现。
#
# 换时区只改环境变量 APP_TZ 即可（例如 Asia/Shanghai、Asia/Seoul），不用动代码。
APP_TZ_NAME = (os.environ.get("APP_TZ") or "Asia/Seoul").strip()
try:
    from zoneinfo import ZoneInfo
    APP_TZ = ZoneInfo(APP_TZ_NAME)
except Exception:      # 运行环境缺 tzdata 时兜底成固定偏移，绝不让启动失败
    APP_TZ = timezone(timedelta(hours=int(os.environ.get("APP_TZ_OFFSET_HOURS") or 9)))
print("[db] 应用时区 = %s" % APP_TZ_NAME)


def app_now():
    """应用时区下的「现在」。

    刻意返回不带 tzinfo 的「墙上时间」：库里所有 created_at / first_at /
    last_at / due 一直都是这种朴素 ISO 串，保持格式不变，新旧数据才能继续
    用字符串直接比较，不需要迁移历史数据。
    """
    return datetime.now(APP_TZ).replace(tzinfo=None)


def app_today():
    """应用时区下的「今天」（date 对象）。"""
    return datetime.now(APP_TZ).date()

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
    # 情景库 / 薄弱项讲解：冲突键是 (word, tag)，不写的话 PG 会按第一列 word 判冲突
    "weak_explanations": "word, tag",
    "weak_hits": "word, tag",
}

# INSERT OR REPLACE 的冲突列（= 各表 UNIQUE 约束列）。
# PostgreSQL 没有 INSERT OR REPLACE 这种语法（SQLite 专有），
# 官方解析器会直接报 syntax error at or near "OR"。
# 之前漏了这条翻译，导致凡是用了 INSERT OR REPLACE 的接口在
# PostgreSQL（Render/Neon）上必定 500，本地 SQLite 却一切正常——
# 所以这类问题在本机测试里永远测不出来。
_REPLACE_CONFLICT = {
    "listening_materials": "stage, week, day",
    "listening_progress": "stage, week, day",
}


def _tr(sql):
    """把 sqlite 风格 SQL 转成 Postgres 风格。sqlite 模式不会调用本函数。"""
    def repl(m):
        tbl, cols, vals = m.group(1), m.group(2), m.group(3)
        conflict = _IGNORE_CONFLICT.get(tbl, cols.split(",")[0].strip())
        return f"INSERT INTO {tbl} ({cols}) VALUES ({vals}) ON CONFLICT ({conflict}) DO NOTHING"

    def repl_replace(m):
        tbl, cols, vals = m.group(1), m.group(2), m.group(3)
        conflict = _REPLACE_CONFLICT.get(tbl, cols.split(",")[0].strip())
        conflict_cols = {c.strip() for c in conflict.split(",")}
        # 冲突列本身不进 SET（它本来就是用来定位那一行的），
        # 其余列全部更新为本次传入的值，等价于 SQLite 的 REPLACE 语义。
        updates = [f"{c.strip()}=excluded.{c.strip()}"
                   for c in cols.split(",") if c.strip() not in conflict_cols]
        if not updates:      # 全是冲突列 → 没有可更新的，退化成 DO NOTHING
            return (f"INSERT INTO {tbl} ({cols}) VALUES ({vals}) "
                    f"ON CONFLICT ({conflict}) DO NOTHING")
        return (f"INSERT INTO {tbl} ({cols}) VALUES ({vals}) "
                f"ON CONFLICT ({conflict}) DO UPDATE SET " + ", ".join(updates))

    sql = re.sub(
        r"INSERT OR IGNORE INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]*)\)",
        repl, sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"INSERT OR REPLACE INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]*)\)",
        repl_replace, sql, flags=re.IGNORECASE)

    def repl_nocols(m):
        # 省略列清单的写法：INSERT OR IGNORE INTO t VALUES (...)
        # 上面两条正则都要求 VALUES 前有 (cols)，这条漏网
        # （seed_ecdict.py 里就有一处）。IGNORE 语义 = 任何约束冲突都跳过，
        # 对应 PG 的裸 ON CONFLICT DO NOTHING（可以不指定冲突目标）。
        # REPLACE 没法这么写 —— DO UPDATE 必须带冲突目标，
        # 只能靠表名映射，映射不到就原样返回，让它在启动时显式报错，
        # 也好过悄悄生成一条错误的 SQL。
        kind, tbl, vals = m.group(1).upper(), m.group(2), m.group(3)
        if kind == "IGNORE":
            return f"INSERT INTO {tbl} VALUES ({vals}) ON CONFLICT DO NOTHING"
        conflict = _REPLACE_CONFLICT.get(tbl)
        if not conflict:
            return m.group(0)
        return f"INSERT INTO {tbl} VALUES ({vals}) ON CONFLICT ({conflict}) DO NOTHING"

    sql = re.sub(
        r"INSERT OR (IGNORE|REPLACE) INTO (\w+)\s*VALUES\s*\(([^)]*)\)",
        repl_nocols, sql, flags=re.IGNORECASE)
    sql = sql.replace("?", "%s")
    sql = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
    # SQLite 的 datetime('now') 默认值在 Postgres 下无效 → 统一翻成 CURRENT_TIMESTAMP
    sql = sql.replace("datetime('now')", "CURRENT_TIMESTAMP")
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

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass
        self._closed = True

    def __del__(self):
        # 兜底：异常路径下忘了 close() 时由 GC 关掉，
        # 避免 Postgres 连接泄漏（Neon 连接数有限，漏多了整个站会 500）。
        try:
            if not getattr(self, "_closed", False):
                self._raw.close()
        except Exception:
            pass

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
    {"stage": 0, "name": "阶段0｜基础重建", "weeks": 12, "months": "第1-3月"},
    {"stage": 1, "name": "阶段1｜旅行生存英语", "weeks": 12, "months": "第4-6月"},
    {"stage": 2, "name": "阶段2｜工作沟通英语", "weeks": 16, "months": "第7-10月"},
    {"stage": 3, "name": "阶段3｜社会与信息输入", "weeks": 16, "months": "第11-14月"},
    {"stage": 4, "name": "阶段4｜雅思输出突破", "weeks": 20, "months": "第15-19月"},
    {"stage": 5, "name": "阶段5｜雅思综合与强化", "weeks": 20, "months": "第20-24月"},
]

# 96 周课程地图（阶段｜周｜主题｜语法）——已固化，后续只允许改词汇内容，不再改这张地图。
# 阶段0 基础重建 12 周 / 阶段1 旅行生存英语 12 周 / 阶段2 工作沟通英语 16 周 /
# 阶段3 社会与信息输入 16 周 / 阶段4 雅思输出突破 20 周 / 阶段5 雅思综合与强化 20 周
SEED_WEEKS = [
    (0, 1, "家庭与人际", "be动词、人称代词、物主代词"),
    (0, 2, "工作与日常", "一般现在时、频率副词"),
    (0, 3, "爱好与休闲", "like / enjoy / hate + doing"),
    (0, 4, "时间与生活", "一般过去时"),
    (0, 5, "交通与出行", "一般将来时 will / be going to"),
    (0, 6, "购物与消费", "可数/不可数名词、some / any / much / many"),
    (0, 7, "地点与城市", "there be、方位介词"),
    (0, 8, "天气与季节", "形容词、副词、比较级、最高级"),
    (0, 9, "食物与饮食", "can / can't、能力与请求"),
    (0, 10, "健康与身体", "must / should / have to"),
    (0, 11, "综合描述", "that / which / who / where / when"),
    (0, 12, "综合复习", "基础被动语态、全语法抽测"),
]

SEED_WEEKS_STAGES15 = {
    1: [
        (1, 1, "机场与值机", "现在进行时、一般现在时表示固定安排"),
        (1, 2, "飞机与飞行", "现在进行时、祈使句"),
        (1, 3, "入境与海关", "一般过去时、现在完成时基础"),
        (1, 4, "酒店入住", "would like、want to、need to"),
        (1, 5, "酒店问题与投诉", "there is / are、should / could"),
        (1, 6, "餐厅点餐", "可数/不可数、some / any、would like"),
        (1, 7, "购物与退换货", "比较级、最高级、too / enough"),
        (1, 8, "问路与导航", "祈使句、方位介词、疑问句"),
        (1, 9, "公共交通", "一般现在时、一般将来时、时间从句基础"),
        (1, 10, "景点与门票", "过去时、现在完成时基础"),
        (1, 11, "旅行活动与体验", "动词不定式、动名词基础"),
        (1, 12, "旅行突发情况", "情态动词 can / could / should / must、条件句基础"),
    ],
    2: [
        (2, 1, "公司与岗位", "一般现在时、there be"),
        (2, 2, "同事与团队", "人称代词、物主代词、反身代词"),
        (2, 3, "工作任务与安排", "一般现在时、一般将来时"),
        (2, 4, "工作进度与时间管理", "现在进行时、现在完成时"),
        (2, 5, "邮件与消息", "祈使句、礼貌请求、would / could"),
        (2, 6, "请求与确认", "疑问句、间接疑问句基础"),
        (2, 7, "问题与解决方案", "should / need to / have to"),
        (2, 8, "客户与需求", "who / which / that 定语从句基础"),
        (2, 9, "产品与功能", "被动语态基础"),
        (2, 10, "技术问题与故障", "现在完成时、被动语态"),
        (2, 11, "会议与讨论", "同意/不同意、比较结构、连接词"),
        (2, 12, "汇报与展示", "过去时、现在时、将来时综合"),
        (2, 13, "项目计划与执行", "将来时、时间从句、条件句"),
        (2, 14, "反馈与改进", "比较级、too / enough、动名词"),
        (2, 15, "跨部门沟通", "条件句、情态动词、间接表达"),
        (2, 16, "职场综合沟通", "时态综合、被动语态、从句综合"),
    ],
    3: [
        (3, 1, "教育与学习", "现在完成时、过去时对比"),
        (3, 2, "科技与互联网", "被动语态、定语从句"),
        (3, 3, "人工智能", "现在/将来时、被动语态"),
        (3, 4, "工作与职业发展", "条件句、将来时"),
        (3, 5, "金钱与消费社会", "比较级、数量表达、百分比表达"),
        (3, 6, "城市与住房", "there be、定语从句"),
        (3, 7, "环境与气候", "被动语态、因果连接词"),
        (3, 8, "健康与生活方式", "情态动词、建议表达"),
        (3, 9, "媒体与新闻", "被动语态、过去时、现在完成时"),
        (3, 10, "社交媒体", "现在完成时、进行时"),
        (3, 11, "文化与娱乐", "定语从句、动名词/不定式"),
        (3, 12, "旅行与全球化", "比较结构、因果关系"),
        (3, 13, "家庭与社会关系", "条件句、让步关系"),
        (3, 14, "年轻人与社会", "观点表达、比较结构"),
        (3, 15, "问题与社会变化", "被动语态、现在完成时"),
        (3, 16, "观点与日常讨论", "复合句、连接词、从句综合"),
    ],
    4: [
        (4, 1, "教育制度", "复杂定语从句、被动语态"),
        (4, 2, "学习方式", "比较结构、原因与结果"),
        (4, 3, "科技发展", "被动语态、现在完成时"),
        (4, 4, "人工智能与未来", "将来时、条件句"),
        (4, 5, "网络与信息", "定语从句、被动语态"),
        (4, 6, "工作与就业", "条件句、情态动词"),
        (4, 7, "职业选择", "比较结构、让步从句"),
        (4, 8, "城市发展", "there be、被动语态、定语从句"),
        (4, 9, "住房问题", "比较结构、原因结果"),
        (4, 10, "交通问题", "被动语态、条件句"),
        (4, 11, "环境保护", "被动语态、因果与让步"),
        (4, 12, "气候变化", "现在完成时、被动语态"),
        (4, 13, "健康与医疗", "情态动词、条件句"),
        (4, 14, "饮食与生活方式", "比较结构、因果关系"),
        (4, 15, "政府与公共服务", "被动语态、情态动词"),
        (4, 16, "社会公平", "比较结构、让步从句"),
        (4, 17, "文化与传统", "定语从句、被动语态"),
        (4, 18, "全球化", "因果关系、让步关系"),
        (4, 19, "媒体与广告", "被动语态、比较结构"),
        (4, 20, "娱乐与休闲", "动名词、不定式、定语从句"),
    ],
    5: [
        (5, 1, "教育与社会", "复杂句综合、从句连接"),
        (5, 2, "科技与社会", "被动语态、复杂定语从句"),
        (5, 3, "环境与发展", "条件句、让步从句"),
        (5, 4, "工作与经济", "条件句、比较结构"),
        (5, 5, "城市与人口", "定语从句、数量表达"),
        (5, 6, "健康与公共政策", "情态动词、被动语态"),
        (5, 7, "媒体与信息", "被动语态、间接表达"),
        (5, 8, "文化与全球化", "让步、因果、比较结构"),
        (5, 9, "家庭与代际关系", "条件句、比较结构"),
        (5, 10, "犯罪与社会治理", "被动语态、情态动词"),
        (5, 11, "政府与个人责任", "条件句、情态动词"),
        (5, 12, "消费与生活质量", "比较结构、数量表达"),
        (5, 13, "动物与自然", "被动语态、定语从句"),
        (5, 14, "艺术与文化", "定语从句、比较结构"),
        (5, 15, "科技伦理与未来", "条件句、将来时、情态动词"),
        (5, 16, "社会问题综合讨论", "复杂句、连接词综合"),
        (5, 17, "雅思高频混合主题一", "时态综合、从句综合"),
        (5, 18, "雅思高频混合主题二", "被动语态、条件句综合"),
        (5, 19, "雅思弱项主题强化一", "根据模考错误动态强化"),
        (5, 20, "雅思弱项主题强化二", "根据模考错误动态强化"),
    ],
}

# 全部 96 周 = 阶段0 + 阶段1-5，供初始化与线上老库同步使用
def all_seed_weeks():
    """返回 [(stage, week_no, title, grammar), ...]，共 96 条。"""
    out = list(SEED_WEEKS)
    for st in sorted(SEED_WEEKS_STAGES15.keys()):
        out.extend(SEED_WEEKS_STAGES15[st])
    return out

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
    # 后台线程灌 ECDICT 词典（76.8 万行）会长时间占用写锁，
    # sqlite3 默认忙等超时只有 5s，并发写入会直接抛 "database is locked"。
    # 放宽到 30s：让写请求排队等待锁释放，而不是立刻失败。
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ECDICT 后台合并线程的启动闸：init_db 可能被调用多次，只允许起一个线程。
_ECDICT_BG_STARTED = False


def sync_curriculum_map(conn=None):
    """把 96 周课程地图同步进 weeks 表。

    - 地图里缺的周 → 插入（新建库 / 老库补 13-20 周）
    - 已存在的周 → 只把 title / grammar 对齐到地图（老库存的旧骨架会被纠正）
    - vocab_json 一律不动：用户导入的词汇内容是数据，地图只是骨架
    返回变动条数。
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        c = conn.cursor()
        changed = 0
        for (st, w, title, grammar) in all_seed_weeks():
            row = c.execute(
                "SELECT title, grammar FROM weeks WHERE stage=? AND week_no=?", (st, w)
            ).fetchone()
            if not row:
                vocab = SEED_WEEK3_VOCAB if (st == 0 and w == 3) else []
                c.execute(
                    "INSERT INTO weeks (stage, week_no, title, grammar, vocab_json) VALUES (?,?,?,?,?)",
                    (st, w, title, grammar, json.dumps(vocab, ensure_ascii=False)),
                )
                changed += 1
            else:
                old_t = row[0] if row[0] is not None else ""
                old_g = row[1] if row[1] is not None else ""
                if old_t != title or old_g != grammar:
                    c.execute(
                        "UPDATE weeks SET title=?, grammar=? WHERE stage=? AND week_no=?",
                        (title, grammar, st, w),
                    )
                    changed += 1
        conn.commit()
        if changed:
            print(f"[db] 课程地图已同步：{changed} 处（96 周「阶段｜周｜主题｜语法」）")
        return changed
    except Exception as e:
        print("[db] 课程地图同步失败（不影响启动）:", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


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

    -- ===== 专项训练（补习）=====
    -- 训练项目：用户针对某项能力短板发起的一期补习（ability=能力维度, problem=问题描述, prompt_md=发给外部 AI 的提示词）
    CREATE TABLE IF NOT EXISTS training_projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ability TEXT,
        problem TEXT,
        prompt_md TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    -- 训练回合作答：用户把外部 AI 出的卷贴回系统、回合制训练的每次作答与判分记录
    CREATE TABLE IF NOT EXISTS training_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        round INTEGER,
        user_sentence TEXT,
        score INTEGER,
        ok INTEGER,
        errors_json TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(project_id) REFERENCES training_projects(id)
    );

    -- 训练会话：一次「开始训练 → 结束」的完整过程，Round 制训练的上层容器。
    -- 前端此前把 projects/sessions/rounds/attempts 全存在 localStorage（eos_train_v1），
    -- 服务端一张表都没有，换设备/清缓存即全部蒸发。这里补齐四层结构的第 2 层。
    CREATE TABLE IF NOT EXISTS training_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT UNIQUE,             -- 前端业务 ID（sess_xxx）
        project_key TEXT NOT NULL,          -- 前端 project_id（字符串，如 P1）
        started_at TEXT,
        ended_at TEXT,
        round_count INTEGER DEFAULT 0,
        valid_attempts INTEGER DEFAULT 0,
        correct_count INTEGER DEFAULT 0,
        incorrect_count INTEGER DEFAULT 0,
        hint_count INTEGER DEFAULT 0,
        independent_correct_count INTEGER DEFAULT 0,
        consecutive_independent_correct INTEGER DEFAULT 0,
        final_status TEXT,                  -- PASS / NEEDS_REVIEW / NOT_YET
        next_step TEXT,                     -- STOP / REVIEW / CONTINUE
        created_at TEXT DEFAULT (datetime('now'))
    );

    -- 训练回合：一次会话里的一轮（每轮只抽少量题，够判断就停）
    CREATE TABLE IF NOT EXISTS training_rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id TEXT UNIQUE,               -- 前端业务 ID（r_xxx）
        session_id TEXT,
        project_key TEXT,
        idx INTEGER DEFAULT 0,              -- 第几轮（index 是 SQL 保留字，故用 idx）
        started_at TEXT,
        ended_at TEXT
    );

    -- 听力材料（用户粘贴 AI 生成的 <<<LISTENING v1>>> 文本，后端解析后入库）
    CREATE TABLE IF NOT EXISTS listening_materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage INTEGER NOT NULL,
        week INTEGER NOT NULL,
        day INTEGER NOT NULL,
        title TEXT DEFAULT '',
        dialogue_json TEXT DEFAULT '[]',
        passage TEXT DEFAULT '',
        questions_json TEXT DEFAULT '[]',
        created_at TEXT,
        UNIQUE(stage, week, day)
    );

    -- 听力练习进度（按 stage/week/day 累计各 Part 正确数）
    CREATE TABLE IF NOT EXISTS listening_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage INTEGER NOT NULL,
        week INTEGER NOT NULL,
        day INTEGER NOT NULL,
        listening_done INTEGER DEFAULT 0,
        listening_total INTEGER DEFAULT 0,
        parts_json TEXT DEFAULT '{}',
        created_at TEXT,
        UNIQUE(stage, week, day)
    );

    CREATE TABLE IF NOT EXISTS weak_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        d TEXT NOT NULL,
        item_key TEXT NOT NULL,
        val REAL DEFAULT 0,
        updated_at TEXT,
        UNIQUE(d, item_key)
    );

    -- ── AI 情景库（设计方案 §2）─────────────────────────────────────────
    -- 触发：导入时主生成 + 学习模块点「记住了」兜底生成（同一函数，已生成跳过）。
    -- 练习时按 used_count 升序取「最少用过的」一条；库存 ≤1 时自动补 3 条。
    -- AI 只往这里写 prompt 文字，绝不动 day_items / weeks / progress（不碰导入列表与周次）。
    CREATE TABLE IF NOT EXISTS word_scenarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT NOT NULL,             -- 关联单词（小写）
        tier TEXT DEFAULT 'small',      -- small / medium / large
        prompt TEXT NOT NULL,           -- 给用户的情景提示（含必须用到的词/语法）
        grammar TEXT DEFAULT '',        -- 该词自带的语法约束（沿用现有，不重填）
        used_count INTEGER DEFAULT 0,   -- 已被抽取次数（用于轮换）
        created_at TEXT
    );

    -- ── 薄弱项 AI 讲解（设计方案 §4）────────────────────────────────────
    -- 同一个「词 + error_tag」只讲一次：讲过就落在这里，下次直接命中不再调 AI。
    CREATE TABLE IF NOT EXISTS weak_explanations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT DEFAULT '',
        tag TEXT NOT NULL,
        explain TEXT NOT NULL,
        hits INTEGER DEFAULT 1,         -- 触发时该错误已累计出现几次
        created_at TEXT,
        UNIQUE(word, tag)
    );

    -- 同一「词 + 错误标签」累计命中次数（§4.2 触发判定用：最近反复出现才讲）
    CREATE TABLE IF NOT EXISTS weak_hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        word TEXT DEFAULT '',
        tag TEXT NOT NULL,
        times INTEGER DEFAULT 1,
        last_at TEXT,
        UNIQUE(word, tag)
    );

    """)

    # 初始化进度（仅一条记录）
    if c.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 0:
        now = app_now().isoformat()
        c.execute("INSERT INTO progress (id, stage, week, day, last_activity, updated_at) VALUES (1, 0, 3, 1, 'vocab', ?)", (now,))

    # 96 周课程地图：缺失的周插入，已存在的周把 title/grammar 对齐到地图
    # （不动 vocab_json —— 用户导入的词汇内容必须原样保留）
    sync_curriculum_map(conn)

    # 情景库的取用全部按 word 查，给个索引免得词多了全表扫
    c.execute("CREATE INDEX IF NOT EXISTS ix_word_scenarios_word ON word_scenarios(word)")

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

    # ---- 专项训练四层落库：给既有表补列（老库无这些列时自动补齐，不动已有数据）----
    _ensure_columns(conn, "training_projects", {
        "project_key": "TEXT",              # 前端 project_id（字符串业务 ID，如 P1）
        "priority": "TEXT DEFAULT 'P2'",
        "intervention_level": "TEXT DEFAULT 'SUGGESTED'",
        "training_goal": "TEXT DEFAULT ''",
        "training_boundary": "TEXT DEFAULT ''",
        "forbidden_json": "TEXT DEFAULT '[]'",
        "exit_standard": "TEXT DEFAULT ''",
        "exit_rule_json": "TEXT DEFAULT '{}'",
        "status": "TEXT DEFAULT 'NOT_STARTED'",
        "items_json": "TEXT DEFAULT '[]'",  # 题目列表（随项目一起导入）
        "updated_at": "TEXT",
        "stage": "INTEGER",                 # 关联键：这个项目归属哪个课程周
        "week": "INTEGER",
    })
    _ensure_columns(conn, "training_attempts", {
        "attempt_id": "TEXT",
        "session_id": "TEXT",
        "round_id": "TEXT",
        "question_id": "TEXT",
        "project_key": "TEXT",
        "user_answer": "TEXT DEFAULT ''",
        "is_correct": "INTEGER DEFAULT 0",
        "manual": "INTEGER DEFAULT 0",
        "used_hint": "INTEGER DEFAULT 0",
        "hint_level": "INTEGER DEFAULT 0",
        "is_independent": "INTEGER DEFAULT 0",
        "word": "TEXT DEFAULT ''",          # 关联键：这道题考的是哪个词
    })

    # ---- 数据打通：给「错误本」「五星输出」补上课程周身份证 ----
    # 这两张表原本只有 created_at 时间和 word，没有 stage/week，
    # 导致回答不了「第 3 周我错了什么」，也进不了按周聚合的总结页。
    _ensure_columns(conn, "errors", {
        "stage": "INTEGER",
        "week": "INTEGER",
        "day": "INTEGER",
    })
    _ensure_columns(conn, "word_output", {
        "stage": "INTEGER",
        "week": "INTEGER",
    })

    # ---- 测评留存：历史成绩要能点开回看「原题 + 作答」----
    # 老库的 quizzes 只有分数，回看不了当次卷子，这里补齐留存列。
    # kind 老库已有（'weekly'/'stage'），此处类型带上 monthly 语义由写入侧决定，列存在则跳过。
    _ensure_columns(conn, "quizzes", {
        "paper_json": "TEXT DEFAULT ''",      # 完整 <<<TEST>>> 试卷原文
        "answers_json": "TEXT DEFAULT '{}'",  # 学习者作答 {"1":"A","2":"at"}
        "kind": "TEXT DEFAULT 'weekly'",      # weekly / monthly / stage
    })

    # ---- 错题合并与分级：错误按 (词 + 类型 + 归一化文本) 合并，近30天≥2次才晋级🟡 ----
    _ensure_columns(conn, "errors", {
        "norm_text": "TEXT DEFAULT ''",   # 归一化错误片段（压缩空白 + 转小写），仅用于合并判定
        "level": "TEXT DEFAULT '🔵'",      # 🔴阻塞 / 🟡薄弱（近30天≥2次）/ 🔵记忆（单次）
        "is_demo": "INTEGER DEFAULT 0",   # 1 = 近8周示例数据，报告与统计默认排除，可一键清空
    })
    # 老数据回填：历史错误行的 norm_text 为空会导致合并失效，用 error_text 兜底
    try:
        conn.execute(
            "UPDATE errors SET norm_text = error_text "
            "WHERE (norm_text IS NULL OR norm_text = '') AND error_text IS NOT NULL")
        conn.execute(
            "UPDATE errors SET level = '🔵' WHERE level IS NULL OR level = ''")
    except Exception as e:
        print("[db.init_db] errors 老数据回填跳过(可忽略):", e)

    # 老数据回填：用 word 反查它属于哪一周（只补 stage IS NULL 的行，可重复运行）
    _backfill_week_columns(conn)

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
        # 专项训练四层落库后的查询路径
        # project_key / attempt_id 需要 UNIQUE 才能用 ON CONFLICT 做 upsert。
        # ALTER TABLE ADD COLUMN 不允许带 UNIQUE，故单独建唯一索引：
        # SQLite 与 PostgreSQL 都把 NULL 视为互不相等，老数据 project_key 全为 NULL
        # 可以共存，不会因重复而被拒。
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tr_proj_key_uniq ON training_projects(project_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tr_att_id_uniq ON training_attempts(attempt_id)",
        "CREATE INDEX IF NOT EXISTS idx_tr_sess_proj ON training_sessions(project_key)",
        "CREATE INDEX IF NOT EXISTS idx_tr_round_sess ON training_rounds(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tr_att_sess ON training_attempts(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tr_att_round ON training_attempts(round_id)",
        "CREATE INDEX IF NOT EXISTS idx_tr_att_word ON training_attempts(word)",
        # 数据打通后按课程周聚合错误本 / 五星输出
        "CREATE INDEX IF NOT EXISTS idx_errors_stage_week ON errors(stage, week)",
        "CREATE INDEX IF NOT EXISTS idx_wout_stage_week ON word_output(stage, week)",
        # 薄弱项每日快照：按日期查整份快照（(d, item_key) 建表时已是 UNIQUE）
        "CREATE INDEX IF NOT EXISTS idx_weak_snap_d ON weak_snapshots(d)",
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
    global _ECDICT_BG_STARTED
    try:
        import threading
        from seed_ecdict import import_into_db as _ecdict_import

        def _ecdict_bg():
            try:
                print("[db.init_db] ECDICT 词典合并(后台):", _ecdict_import())
            except Exception as e:
                print("[db.init_db] ECDICT 词典合并失败(可忽略):", e)
            finally:
                # 跑完了就允许再次启动（进程内只该跑一次，但失败重跑不该被永久锁死）
                pass

        # 防重复：init_db 未来可能被多次调用（或热重载），
        # 没有这道闸会起多个线程同时写 76 万行，互相抢写锁还可能灌重复数据。
        if not _ECDICT_BG_STARTED:
            _ECDICT_BG_STARTED = True
            threading.Thread(target=_ecdict_bg, daemon=True).start()
    except Exception as e:
        print("[db.init_db] ECDICT 词典合并启动失败(可忽略):", e)

    # 并发安全用的唯一索引（幂等；库里已有重复数据则跳过，不删任何历史行）
    try:
        ensure_unique_indexes(conn)
    except Exception as e:
        print("[db.init_db] 唯一索引建立失败(可忽略):", e)

    conn.commit()
    conn.close()


# DDL 白名单：凡是拼进 ALTER TABLE / CREATE INDEX 的表名、列名都必须在这里。
# 全部来自本文件内写死的调用点，加白名单是为了让「外部输入拼进 DDL」这条路直接断掉。
_DDL_TABLE_WHITELIST = {
    "reviews", "sentences", "errors", "training_projects", "training_attempts",
    "word_output", "quizzes",
}
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ensure_columns(conn, table, columns):
    """给已存在的表补列。columns: {列名: SQL类型}。"""
    # 表名来自本文件内写死的调用点，仍加一道白名单：
    # 万一将来有人把外部输入传进来，这里直接拒绝，而不是拼进 DDL。
    if table not in _DDL_TABLE_WHITELIST:
        raise ValueError("拒绝为非白名单表改结构: %r" % table)
    if _using_pg():
        existing = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,)).fetchall()}
    else:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, coltype in columns.items():
        if col not in existing:
            if not _SAFE_IDENT.match(col):
                raise ValueError("拒绝非法列名: %r" % col)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


# ------------------------------------------------------------------
# 并发安全：唯一索引
# ------------------------------------------------------------------
# 下面两条索引是「长期运行不该出现重复数据」的硬保证：
#   * sentences：同一道题（stage/week/day/task_key）的 attempt 不许撞号
#   * errors    ：同一个归并键（source+word+error_type+norm_text）只许一条
# 有了它们，并发提交就由数据库兜底，而不是靠「先查再插」这种会被并发击穿的写法。
#
# errors 那条必须是**部分索引**（只管 source='sentence'）：
# link.py 写周测错题时不设 norm_text，而该列默认是空串 ''，
# 同一张卷子里两道同 tag 的错题归并键完全一样 —— 若建成全表唯一索引，
# 周测错题同步会撞键、被它自己的 try/except 吞掉，整段同步静默失效。
# 限死在造句错题上，既覆盖本次要修的并发点，又不碰周测那条链路。
# SQLite 与 PostgreSQL 的部分索引语法一致，无需分支。
_UNIQUE_INDEXES = [
    ("sentences", "ux_sentences_attempt", "stage, week, day, task_key, attempt", ""),
    ("errors", "ux_errors_merge", "source, word, error_type, norm_text",
     " WHERE source = 'sentence'"),
]


def ensure_unique_indexes(conn=None):
    """建立上面的唯一索引（幂等）。

    库里若已存在重复数据，索引会建不起来 —— 这种情况只记日志跳过，
    **绝不删历史数据**；并发保护自动退化为「事务内取值 + 冲突重试」。
    """
    own = conn is None
    if own:
        conn = get_conn()
    created = []
    for tbl, name, cols, where in _UNIQUE_INDEXES:
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS %s ON %s (%s)%s"
                % (name, tbl, cols, where))
            conn.commit()          # 每条单独提交：PG 事务报错后必须 rollback 才能继续
            created.append(name)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print("[db.ensure_unique_indexes] 跳过 %s（库里可能已有重复数据，"
                  "并发保护降级）: %s" % (name, e))
    if own:
        conn.close()
    return created


def _backfill_week_columns(conn):
    """给老数据回填「课程周」身份证（stage/week）。

    背景：errors 和 word_output 历史上只有 created_at 和 word，没有 stage/week，
    补列之后老行全是 NULL —— 回答不了「第 3 周我错了什么」，也进不了按周聚合的总结页。

    做法：用 word 去 sentences 反查该词最近一次出现在哪一周。
      * 只处理 stage IS NULL 的行 → 可重复运行，不覆盖已填好的值
      * 用标量子查询而非 UPDATE...FROM → SQLite 与 PostgreSQL 都支持这一写法
      * 反查不到就留 NULL（前端按「未知周」显示），绝不猜一个数字填上去
      * sentences 表为空（新用户）时直接返回，不做无谓的全表扫描
    """
    try:
        has_src = conn.execute("SELECT COUNT(*) FROM sentences").fetchone()[0]
        if not has_src:
            return
    except Exception as e:
        print("[db._backfill_week_columns] 跳过（无法读取 sentences）:", e)
        return

    stmts = (
        # 错误本：按出错的词反查它属于哪一周
        "UPDATE errors SET stage = (SELECT s.stage FROM sentences s "
        "  WHERE s.word <> '' AND s.word = errors.word ORDER BY s.id DESC LIMIT 1), "
        "week = (SELECT s.week FROM sentences s "
        "  WHERE s.word <> '' AND s.word = errors.word ORDER BY s.id DESC LIMIT 1) "
        "WHERE stage IS NULL AND word IS NOT NULL AND word <> ''",
        # 五星输出：按词反查
        "UPDATE word_output SET stage = (SELECT s.stage FROM sentences s "
        "  WHERE s.word <> '' AND s.word = word_output.word ORDER BY s.id DESC LIMIT 1), "
        "week = (SELECT s.week FROM sentences s "
        "  WHERE s.word <> '' AND s.word = word_output.word ORDER BY s.id DESC LIMIT 1) "
        "WHERE stage IS NULL AND word IS NOT NULL AND word <> ''",
    )
    for sql in stmts:
        try:
            conn.execute(sql)
        except Exception as e:
            print("[db._backfill_week_columns] 跳过:", e)


def today_str():
    return app_today().isoformat()


def ts():
    return app_now().isoformat(timespec="seconds")
