"""SQLite 访问层：建表、连接、迁移。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import CFG

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

-- 视频帧的向量映射：一个视频多帧多向量，全部指向同一 file_id
CREATE TABLE IF NOT EXISTS video_vec_map (
  file_id INTEGER NOT NULL,
  vec_row INTEGER NOT NULL,
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
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")  # 约 200MB
    return con


def init_db(path: Path | None = None) -> None:
    con = connect(path)
    try:
        con.executescript(SCHEMA)
        con.executescript(TRIGGERS)
        # 已有库的列迁移：SQLite 的 ALTER 只支持加列，够用
        cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
        if "frame_count" not in cols:
            con.execute("ALTER TABLE files ADD COLUMN frame_count INTEGER DEFAULT 0")
        if "frames_done" not in cols:
            con.execute("ALTER TABLE files ADD COLUMN frames_done INTEGER DEFAULT 0")
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
