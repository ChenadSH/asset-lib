"""SQLite 访问层：建表、连接、迁移。"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import CFG

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id       INTEGER PRIMARY KEY,
  dev      INTEGER NOT NULL,
  ino      INTEGER NOT NULL,
  path     TEXT    NOT NULL,
  name     TEXT    NOT NULL,
  ext      TEXT,
  kind     TEXT,
  size     INTEGER,
  mtime    INTEGER,
  top      TEXT,
  year     INTEGER,
  status   TEXT DEFAULT 'new',
  thumb    INTEGER DEFAULT 0,
  vec_row  INTEGER,
  phash    INTEGER,
  seen_at  INTEGER,
  -- 视频抽帧：frame_count = 已抽帧数（f0 代表帧 + f1.. 副帧），
  -- frames_done = 已编码向量的帧数（断点续跑指针）
  frame_count INTEGER DEFAULT 0,
  frames_done INTEGER DEFAULT 0,
  -- 筛选用派生字段（迁移自动补列）：
  -- mtype = 用户视角素材类型（PPT/TIFF 从 kind 里拆出来了）
  -- duration 秒（仅视频）；width/height 取自缩略图（长宽比与原文件一致）；
  -- orient = h 横屏 / v 竖屏 / sq 方图
  mtype    TEXT,
  duration REAL,
  width    INTEGER,
  height   INTEGER,
  orient   TEXT,
  UNIQUE(dev, ino)
);
CREATE INDEX IF NOT EXISTS idx_kind   ON files(kind);
CREATE INDEX IF NOT EXISTS idx_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_top    ON files(top);
CREATE INDEX IF NOT EXISTS idx_thumb  ON files(thumb);
CREATE INDEX IF NOT EXISTS idx_vec    ON files(vec_row);
CREATE INDEX IF NOT EXISTS idx_phash  ON files(phash);
CREATE INDEX IF NOT EXISTS idx_year   ON files(year);
-- 关键：让「按时间倒序 + LIMIT」走索引提前终止，LIKE 查询才能从 500ms 降到几十毫秒
CREATE INDEX IF NOT EXISTS idx_status_mtime ON files(status, mtime DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  name, path,
  content='files', content_rowid='id',
  tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS queue (
  id       INTEGER PRIMARY KEY,
  file_id  INTEGER NOT NULL,
  job      TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  err      TEXT,
  created  INTEGER,
  UNIQUE(file_id, job)
);
CREATE INDEX IF NOT EXISTS idx_queue_job ON queue(job, id);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

-- 视频帧的向量映射：一个视频多帧多向量，全部指向同一 file_id。
-- frame_idx 记录这一行向量对应第几帧（0 = 代表帧），检索命中后要能
-- 直接展示「命中的那一帧」，否则所有结果都只能显示封面。
CREATE TABLE IF NOT EXISTS video_vec_map (
  file_id   INTEGER NOT NULL,
  vec_row   INTEGER NOT NULL,
  frame_idx INTEGER DEFAULT 0,
  UNIQUE(file_id, vec_row)
);
CREATE INDEX IF NOT EXISTS idx_vvm_row ON video_vec_map(vec_row);
"""

# FTS 触发器：保持与 files 同步
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
  INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
  INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
END;
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF name, path ON files BEGIN
  INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
  INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
END;
"""


def connect(path: Path | None = None, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    p = path or CFG.db_path
    if readonly and p.exists():
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=timeout)
    else:
        con = sqlite3.connect(str(p), timeout=timeout)
    con.row_factory = sqlite3.Row
    # 不要在这里执行 `PRAGMA journal_mode=WAL`：它是持久化在库文件头里的属性，
    # 建库时设一次就够；而每次连接都执行会让它变成一次写操作，扫描/worker
    # 持写锁时全部接口都卡在这一行直到 timeout（实测 /api/facets 卡死 20s+，
    # 前端表现为「素材类型整行消失」）。WAL 由 init_db 负责设置。
    if not readonly:
        con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")  # 约 200MB
    return con


def init_db(path: Path | None = None) -> None:
    con = connect(path)
    try:
        # WAL 只需在启动时设一次（属性写在库文件里），连接路径上不再重复设置
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as ex:
            log.warning("设置 WAL 失败（继续用现有模式）：%s", ex)
        con.executescript(SCHEMA)
        con.executescript(TRIGGERS)
        # 已有库的列迁移：SQLite 的 ALTER 只支持加列，够用
        cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
        if "frame_count" not in cols:
            con.execute("ALTER TABLE files ADD COLUMN frame_count INTEGER DEFAULT 0")
        if "frames_done" not in cols:
            con.execute("ALTER TABLE files ADD COLUMN frames_done INTEGER DEFAULT 0")
        # 筛选派生列（mtype/duration/width/height/orient）
        for cname, ddl in (("mtype", "TEXT"), ("duration", "REAL"),
                           ("width", "INTEGER"), ("height", "INTEGER"),
                           ("orient", "TEXT")):
            if cname not in cols:
                con.execute(f"ALTER TABLE files ADD COLUMN {cname} {ddl}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_mtype  ON files(mtype)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orient ON files(orient)")
        # 时长筛选是「>= 阈值」的范围条件，没索引时 count 要全表扫（实测 166ms）；
        # 复合 (kind, duration) 能把扫描限制在视频子集，count 降到 <1ms。
        # 单列 idx_duration 被它完全覆盖，删掉省空间。
        con.execute("DROP INDEX IF EXISTS idx_duration")
        con.execute("CREATE INDEX IF NOT EXISTS idx_kind_dur ON files(kind, duration)")
        # mtype 由扩展名纯派生，直接回填历史数据（27 万行 < 5s）
        if con.execute("SELECT count(*) c FROM files WHERE mtype IS NULL").fetchone()["c"]:
            from .config import MTYPE_MAP
            for mt, exts in MTYPE_MAP.items():
                con.execute(
                    f"UPDATE files SET mtype='{mt}' WHERE mtype IS NULL "
                    f"AND lower(ext) IN ({','.join('?' * len(exts))})", tuple(exts))
            con.execute("UPDATE files SET mtype='other' WHERE mtype IS NULL")
        con.commit()
        vcols = {r[1] for r in con.execute("PRAGMA table_info(video_vec_map)")}
        if vcols and "frame_idx" not in vcols:
            con.execute("ALTER TABLE video_vec_map ADD COLUMN frame_idx INTEGER DEFAULT 0")
            # 历史数据回填：帧向量是按帧号顺序连续追加的，按 vec_row 排序即帧序。
            # 用临时表一次性重建（相关子查询在 12 万行上是 O(n²)，要跑几十分钟）。
            con.execute("CREATE TEMP TABLE _vvm AS "
                        "SELECT file_id, vec_row, "
                        "row_number() OVER (PARTITION BY file_id ORDER BY vec_row) - 1 AS fidx "
                        "FROM video_vec_map")
            con.execute("DELETE FROM video_vec_map")
            con.execute("INSERT INTO video_vec_map(file_id, vec_row, frame_idx) "
                        "SELECT file_id, vec_row, fidx FROM _vvm")
            con.execute("DROP TABLE _vvm")
        con.commit()
    finally:
        con.close()


@contextmanager
def cursor(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    con = connect(path)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
