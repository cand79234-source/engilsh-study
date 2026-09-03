# -*- coding: utf-8 -*-
"""全量 ECDICT 词典种子（约 7 万词，含 word/phonetic/中文释义/词性/考试分级/词频）。

设计目标（不破坏任何现有功能）：
- 仅向 dictionary 表「补充」缺失词；UNIQUE(word) 保证不覆盖现有 150 个精选词。
- 与 SQLite / PostgreSQL(Neon) 都兼容：统一用 executemany + "INSERT OR IGNORE"
  （PG 适配层自动转 ON CONFLICT (word) DO NOTHING）。
- 只读、幂等：重复运行无副作用；种子文件缺失则跳过，不影响其它功能。

种子文件 ecdict_seed.db 由 build_seed() 从 ECDICT 的 ecdict.csv 预处理生成
（仅含 dictionary 表），随仓库发布，部署时由 db.init_db 调用本模块 import_into_db 合并入主库。
"""
import os
import re
import csv
import sqlite3

SEED_DB = os.path.join(os.path.dirname(__file__), "ecdict_seed.db")

_POS_MAP = {
    "n": "名词", "ns": "名词复数", "nv": "动词", "v": "动词", "vi": "不及物动词",
    "vt": "及物动词", "vg": "动词", "a": "形容词", "adj": "形容词", "ad": "副词",
    "adv": "副词", "prep": "介词", "conj": "连词", "pron": "代词", "num": "数词",
    "int": "感叹词", "art": "冠词", "abbr": "缩写", "u": "不可数名词",
    "c": "可数名词", "pl": "复数", "aux": "助动词",
}
_POS_PREFIX = ("n.", "v.", "vi.", "vt.", "a.", "adj.", "ad.", "adv.", "prep.",
               "conj.", "pron.", "num.", "int.", "art.", "abbr.", "aux.")
_SPLIT_RE = re.compile(r"\r?\n|\\n")


def _clean_meaning(translation):
    """取中文释义第一段，去掉行首英文词性标记（如 'n. '）。

    注意：ECDICT 的 CSV 里换行是**字面两字符 `\\n`**（不是真实换行符），
    必须一并切分，否则释义会拖着 `\\nvt. 陪伴` 这样的尾巴（影响约 5 万词）。
    """
    if not translation:
        return ""
    seg = _SPLIT_RE.split(translation)[0].split(";")[0].strip()
    for p in _POS_PREFIX:
        if seg.startswith(p):
            seg = seg[len(p):].strip()
            break
    return seg


_EXTRACT_POS = ("n", "v", "vt", "vi", "adj", "adv", "ad", "a", "prep",
                "conj", "pron", "num", "int", "art", "abbr", "aux", "pl")


def _pos_from_translation(translation):
    """ECDICT 的 pos 列几乎全空，但中文释义里常带 'n. ' / 'vt. ' / 'adj. ' 标记，
    从这里兜底提取词性，显著提高词性标签覆盖率。"""
    if not translation:
        return ""
    for line in _SPLIT_RE.split(translation):
        for seg in line.split(";"):
            seg = seg.strip().lower()
            for p in _EXTRACT_POS:
                if seg.startswith(p + "."):
                    return _map_pos(p)
    return ""


_PHON_MAP = {"ә": "ə", "ӕ": "æ", "ɑ́": "ɑ", "е": "e"}


def _clean_phonetic(ph):
    """ECDICT 音标用了部分非标准字符（如西里尔 schwa ә），清洗为标准 IPA。"""
    if not ph:
        return ""
    for k, v in _PHON_MAP.items():
        ph = ph.replace(k, v)
    return ph


def _map_pos(pos):
    if not pos:
        return ""
    pos = pos.strip().lower().rstrip(".")
    for k, v in _POS_MAP.items():
        if k == pos:
            return v
    first = pos.split(",")[0].split("/")[0].strip()
    for k, v in _POS_MAP.items():
        if k == first:
            return v
    return ""


def build_seed(csv_path, out_db=None):
    """从 ecdict.csv 生成 / 覆盖种子 SQLite（仅 dictionary 表）。供本地预处理用。"""
    out_db = out_db or SEED_DB
    if os.path.exists(out_db):
        os.remove(out_db)
    out = sqlite3.connect(out_db)
    out.execute("""CREATE TABLE dictionary (
        word TEXT PRIMARY KEY, phonetic TEXT DEFAULT '',
        meaning TEXT DEFAULT '', pos TEXT DEFAULT '', tag TEXT DEFAULT '', bnc INTEGER DEFAULT 0)""")
    rows = []
    with open(csv_path, encoding="utf-8", errors="ignore") as f:
        r = csv.DictReader(f)
        for row in r:
            word = (row.get("word") or "").strip().lower()
            if not word or len(word) > 60:
                continue
            translation = (row.get("translation") or "").strip()
            meaning = _clean_meaning(translation)
            # 仅收录带中文释义的词，避免灌入无意义的英文碎片（中文学习场景无用）
            if not meaning:
                continue
            phonetic = _clean_phonetic(row.get("phonetic") or "")
            pos = _map_pos(row.get("pos") or "") or _pos_from_translation(translation)
            tag = (row.get("tag") or "").strip()
            try:
                bnc = int(row.get("bnc") or 0) or 0
            except (ValueError, TypeError):
                bnc = 0
            rows.append((word, phonetic, meaning, pos, tag, bnc))
    out.executemany("INSERT OR IGNORE INTO dictionary VALUES (?,?,?,?,?,?)", rows)
    out.commit()
    n = out.execute("SELECT COUNT(*) FROM dictionary").fetchone()[0]
    out.close()
    return n


def backfill_missing(conn, seed_path):
    """回填：主库里的精选词若缺音标/词性，用种子补齐（只填空，绝不覆盖已有值）。

    **只针对精选词（theme!=''，约 146 个）**。原因：ECDICT 里本来就没音标的 60 万词，
    种子里同样是空的，旧实现每次启动都要把 62.8 万行拉进内存建 76 万项字典，
    白耗 460MB 内存，最后只更新 145 行。收窄范围后内存开销可忽略。
    用户导入的词不受影响：若该词在 ECDICT 中存在，合并时已带音标入库。

    本函数只 UPDATE 空字段，不触碰 meaning / theme / bnc 等任何已有数据。
    """
    targets = conn.execute(
        "SELECT word, phonetic, pos FROM dictionary WHERE theme!='' AND "
        "(phonetic IS NULL OR phonetic='' OR pos IS NULL OR pos='')").fetchall()
    if not targets:
        return 0
    seed = sqlite3.connect(seed_path)
    upd = []
    for r in targets:
        row = seed.execute(
            "SELECT phonetic, pos FROM dictionary WHERE word=?", (r["word"],)).fetchone()
        if not row:
            continue
        ph, po = row[0] or "", row[1] or ""
        if not ph and not po:
            continue
        new_ph = (r["phonetic"] or "") or ph
        new_po = (r["pos"] or "") or po
        if new_ph != (r["phonetic"] or "") or new_po != (r["pos"] or ""):
            upd.append((new_ph, new_po, r["word"]))
    seed.close()
    if not upd:
        return 0
    conn.executemany("UPDATE dictionary SET phonetic=?, pos=? WHERE word=?", upd)
    conn.commit()
    return len(upd)


def import_into_db(conn=None):
    """部署时把种子 SQLite 的 dictionary 合并入主库（幂等，不覆盖现有精选词）。

    支持 .gz 压缩种子：若 ecdict_seed.db 不存在但 ecdict_seed.db.gz 存在，
    先解压到临时文件再合并（压缩后体积更小，便于随仓库发布）。
    """
    seed_path = SEED_DB
    tmp_name = None
    if not os.path.exists(seed_path) and os.path.exists(SEED_DB + ".gz"):
        import gzip
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        with gzip.open(SEED_DB + ".gz", "rb") as gz:
            tmp.write(gz.read())
        tmp.close()
        seed_path = tmp.name
        tmp_name = tmp.name
    if not os.path.exists(seed_path):
        return {"words": 0, "note": "种子库缺失，跳过（不影响现有功能）"}
    own = conn is None
    if own:
        from db import get_conn
        conn = get_conn()
    try:
        # 已合并过就不再重复灌（PG 远程库插入 76 万行会拖慢启动，必须跳过）
        _n = conn.execute("SELECT COUNT(*) FROM dictionary").fetchone()
        existing = _n[0] if _n else 0
        if existing >= 500000:
            try:
                filled = backfill_missing(conn, seed_path)
            except Exception as e:
                filled = -1
                print("[seed_ecdict] 音标回填失败(可忽略):", e)
            finally:
                if tmp_name:
                    os.unlink(tmp_name)
            return {"words": existing, "filled": filled, "skipped": True,
                    "note": "全量词典已在库中，跳过重复合并"}

        seed = sqlite3.connect(seed_path)
        total = seed.execute("SELECT COUNT(*) FROM dictionary").fetchone()[0]
        if not total:
            seed.close()
            if tmp_name:
                os.unlink(tmp_name)
            return {"words": 0, "note": "种子库为空"}

        # 流式分批：游标 fetchmany 逐批取，内存只保留一批（约 3MB），
        # 不再一次性 fetchall 76.8 万行（那样峰值 400MB+，Render 512MB 会 OOM）
        BATCH = 5000
        use_pg = bool(os.environ.get("DATABASE_URL")) and hasattr(conn, "_raw")
        it = seed.execute(
            "SELECT word, phonetic, meaning, pos, tag, bnc FROM dictionary")
        done = 0
        if use_pg:
            from psycopg2.extras import execute_values
            cur = conn._raw.cursor()
            sql = ("INSERT INTO dictionary (word, phonetic, meaning, pos, tag, bnc) "
                   "VALUES %s ON CONFLICT (word) DO NOTHING")
        while True:
            batch = it.fetchmany(BATCH)
            if not batch:
                break
            if use_pg:
                execute_values(cur, sql, batch, page_size=BATCH)
            else:
                conn.executemany(
                    "INSERT OR IGNORE INTO dictionary (word, phonetic, meaning, pos, tag, bnc) "
                    "VALUES (?,?,?,?,?,?)", batch)
            done += len(batch)
        conn.commit()
        try:
            filled = backfill_missing(conn, seed_path)
        except Exception as e:  # 回填失败不影响主流程
            filled = -1
            print("[seed_ecdict] 音标回填失败(可忽略):", e)
        seed.close()
        if tmp_name:
            os.unlink(tmp_name)
        n = conn.execute("SELECT COUNT(*) FROM dictionary").fetchone()[0]
    finally:
        if own:
            conn.close()
    return {"words": n, "filled": filled,
            "note": "ECDICT 全量词典已合并入主库"}
