"""全量扫描与对账。

设计要点
--------
1. 对 __source_root__ 只做 os.scandir / stat，不写入任何内容。
2. 文件身份 = (st_dev, st_ino)。改名、移动父目录后 st_ino 不变，
   因此「移动」只是 UPDATE path，缩略图与向量全部复用。
3. 整卷不可读时直接放弃本轮，绝不把「读不到」当成「已删除」。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

from .config import CFG, Config, INDEXABLE, classify
from .db import TRIGGERS, connect, get_meta, set_meta

log = logging.getLogger("scanner")

YEAR_RE = re.compile(r"(?<!\d)(20[0-2]\d)(?!\d)")


def _guess_year(rel: str) -> int | None:
    m = YEAR_RE.findall(rel)
    return int(m[0]) if m else None


def _sid(v: int) -> int:
    """把无符号 64 位文件标识转成 SQLite 能存的有符号 64 位整数。

    Windows（NTFS）的 st_dev / st_ino 来自文件索引号，可能是 2^63 以上，
    直接入库会抛 "Python int too large to convert to SQLite INTEGER"。
    统一在入库前做一次双射转换，读出来做键比较时两边都是同一表示。
    """
    v &= (1 << 64) - 1
    return v - (1 << 64) if v >= (1 << 63) else v


def _iter_files(root: str, skip: set[str]) -> "list[tuple]":
    """用 scandir 递归，DirEntry.stat() 在 Windows 上直接取目录枚举缓存，比 os.stat 快。"""
    out: list[tuple] = []
    root = os.path.abspath(root)
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except (PermissionError, FileNotFoundError, OSError):
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name.lower() in skip:
                            continue
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        if not st.st_ino:
                            st = os.stat(e.path, follow_symlinks=False)
                        # Windows 的 st_dev / st_ino 是无符号 64 位（NTFS 文件索引号可能 > 2^63），
                        # 直接写库会溢出 SQLite 的有符号 INTEGER；统一转成有符号表示。
                        out.append((_sid(st.st_dev), _sid(st.st_ino), e.path, e.name.lower(),
                                    os.path.splitext(e.name)[1].lower(), st.st_size, int(st.st_mtime)))
                except (PermissionError, FileNotFoundError, OSError):
                    continue
    return out


def _adopt_paths(con, old_root: str, new_root: str, cur: list[tuple]) -> tuple[int, list[tuple[int, int, int, int]]]:
    """源目录变了（换盘 / 换文件夹 / 换机器）：按相对路径把旧记录平移到新位置。

    迁移后 inode 和 st_dev 都变了，但相对路径不变，因此可以沿用旧行的
    缩略图（thumb）、向量位置（vec_row）、指纹（phash），不必全库重算。

    返回 (领养条数, [(旧dev, 旧ino, 新dev, 新ino), ...])，后者用于重命名缩略图文件。
    """
    old_root = os.path.normpath(old_root)
    new_root = os.path.normpath(new_root)
    pref_old = old_root.rstrip(os.sep) + os.sep
    pref_new = new_root.rstrip(os.sep) + os.sep
    if pref_old == pref_new:
        return 0, []

    by_path = {row[2]: (row[0], row[1]) for row in cur}  # 新路径 -> (dev, ino)
    rows = con.execute("SELECT id, dev, ino, path FROM files").fetchall()
    adopted, moves = 0, []
    for r in rows:
        p = r["path"]
        if not p.startswith(pref_old):
            continue
        newp = pref_new + p[len(pref_old):]
        ident = by_path.get(newp)
        if not ident:
            continue
        dev, ino = ident
        if (r["dev"], r["ino"]) == (dev, ino):
            continue
        try:
            con.execute("UPDATE OR IGNORE files SET path=?, dev=?, ino=? WHERE id=?",
                        (newp, dev, ino, r["id"]))
            moves.append((r["dev"], r["ino"], dev, ino))
            adopted += 1
        except Exception:  # noqa: BLE001  唯一约束冲突时保留旧行，交给后续 diff 处理
            continue
    return adopted, moves


def _rename_thumbs(thumb_dir, moves: list[tuple[int, int, int, int]]) -> int:
    """把缩略图从旧身份改名到新身份，文件名规则 <dev>_<ino>.webp。"""
    renamed = 0
    for od, oi, nd, ni in moves:
        src = thumb_dir / f"{od}_{oi}.webp"
        if not src.exists():
            continue
        try:
            os.replace(src, thumb_dir / f"{nd}_{ni}.webp")
            renamed += 1
        except OSError:
            pass
    return renamed


def scan(cfg: Config | None = None, progress: Callable[[str], None] | None = None) -> dict:
    c = cfg or CFG
    root = os.path.abspath(c.source_root)
    skip = {d.lower() for d in c.skip_dirs}
    say = progress or (lambda m: log.info(m))

    if not os.path.isdir(root):
        msg = f"源目录不可访问，跳过本轮：{root}"
        log.warning(msg)
        return {"ok": False, "reason": "offline", "message": msg}

    root_dev = _sid(os.stat(root).st_dev)

    con = connect()
    try:
        known_dev = get_meta(con, "vol_dev")

        t0 = time.time()
        say("开始遍历源目录…")
        cur = _iter_files(root, skip)
        walk_s = time.time() - t0
        say(f"遍历完成：{len(cur)} 个文件，耗时 {walk_s:.1f}s")

        # 卷存在但一个文件都没有 —— 大概率是盘刚插上还没就绪，按离线处理
        if not cur:
            msg = "扫描到 0 个文件，按离线处理，不做任何删除"
            log.warning(msg)
            return {"ok": False, "reason": "empty", "message": msg}

        # 换盘 / 换目录 / 换机器：按相对路径领养旧记录，避免缩略图与向量全部重算
        old_root = get_meta(con, "source_root")
        if old_root and os.path.normpath(old_root) != os.path.normpath(root):
            say(f"检测到源目录变更：{old_root} → {root}，尝试按相对路径沿用旧索引…")
            con.executescript("DROP TRIGGER IF EXISTS files_au;")  # 批量改 path 时避免逐行写 FTS
            try:
                n, moves = _adopt_paths(con, old_root, root, cur)
                con.commit()
                if n:
                    con.executescript(TRIGGERS)                     # 恢复触发器
                    con.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
                    con.commit()
                    say(f"已沿用 {n} 条旧记录（缩略图/向量/指纹保留）")
                    t_rn = time.time()
                    rn = _rename_thumbs(c.thumb_dir, moves)
                    say(f"缩略图改名 {rn}/{len(moves)} 个，耗时 {time.time() - t_rn:.1f}s")
            finally:
                con.executescript(TRIGGERS)                         # 幂等恢复
        set_meta(con, "source_root", root)
        set_meta(con, "vol_dev", str(root_dev))

        now = int(time.time())
        existing: dict[tuple[int, int], tuple] = {}
        for r in con.execute("SELECT id, dev, ino, path, size, mtime, status FROM files"):
            existing[(r["dev"], r["ino"])] = (r["id"], r["path"], r["size"], r["mtime"], r["status"])

        new_rows, changed, moved, seen_keys = [], [], [], set()
        for dev, ino, path, name_l, ext, size, mtime in cur:
            key = (dev, ino)
            seen_keys.add(key)
            kind = classify(ext)
            old = existing.get(key)
            if old is None:
                rel = os.path.relpath(path, root)
                top = rel.split(os.sep)[0] if os.sep in rel else ""
                new_rows.append((dev, ino, path, Path(path).stem, ext, kind, size, mtime,
                                 top, _guess_year(rel), "ready", now))
            else:
                fid, old_path, old_size, old_mtime, old_status = old
                if old_path != path:
                    moved.append((path, Path(path).stem, ext, size, mtime,
                                  os.path.relpath(path, root).split(os.sep)[0], fid))
                elif old_size != size or old_mtime != mtime:
                    changed.append((size, mtime, now, fid))
                elif old_status == "missing":
                    con.execute("UPDATE files SET status='ready', seen_at=? WHERE id=?", (now, fid))

        missing = [v[0] for k, v in existing.items() if k not in seen_keys and v[4] != "missing"]

        say(f"diff：新增 {len(new_rows)} · 变更 {len(changed)} · 移动 {len(moved)} · 失效 {len(missing)}")

        bulk = not existing
        if bulk:
            # 首次全量导入：先摘掉 FTS 触发器批量插入，结束再统一 rebuild，快一个数量级
            con.executescript("""
                DROP TRIGGER IF EXISTS files_ai;
                DROP TRIGGER IF EXISTS files_ad;
                DROP TRIGGER IF EXISTS files_au;
            """)

        if new_rows:
            # 分块提交：单条超大事务会长时间锁住写者，且中断后全部丢失
            CHUNK = 20000
            for i in range(0, len(new_rows), CHUNK):
                con.executemany(
                    "INSERT OR IGNORE INTO files"
                    "(dev,ino,path,name,ext,kind,size,mtime,top,year,status,seen_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", new_rows[i:i + CHUNK])
                con.commit()
                if len(new_rows) > CHUNK:
                    say(f"入库 {min(i + CHUNK, len(new_rows))}/{len(new_rows)}")

        if changed:
            con.executemany("UPDATE files SET size=?, mtime=?, seen_at=?, thumb=0, vec_row=NULL, phash=NULL WHERE id=?",
                            changed)

        if moved:
            con.executemany("UPDATE files SET path=?, name=?, ext=?, size=?, mtime=?, top=? WHERE id=?", moved)

        if missing:
            con.executemany("UPDATE files SET status='missing' WHERE id=?", [(i,) for i in missing])

        # 新增 / 变更的文件进队列（file_id+job 唯一，重复入队自动忽略）
        # 按文件体积升序入队：小图先出，页面能最快有图可看
        cur_jobs = con.execute(
            "INSERT OR IGNORE INTO queue(file_id, job, created) "
            "SELECT id, 'thumb', ? FROM files "
            "WHERE thumb=0 AND status='ready' AND kind IN ('raster','design','doc','raw') "
            "ORDER BY size ASC",
            (now,),
        )
        jobs_n = cur_jobs.rowcount if cur_jobs.rowcount and cur_jobs.rowcount > 0 else 0

        if bulk:
            say("重建全文索引…")
            con.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
            con.executescript("""
                CREATE TRIGGER files_ai AFTER INSERT ON files BEGIN
                  INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
                END;
                CREATE TRIGGER files_ad AFTER DELETE ON files BEGIN
                  INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
                END;
                CREATE TRIGGER files_au AFTER UPDATE OF name, path ON files BEGIN
                  INSERT INTO files_fts(files_fts, rowid, name, path) VALUES('delete', old.id, old.name, old.path);
                  INSERT INTO files_fts(rowid, name, path) VALUES (new.id, new.name, new.path);
                END;
            """)

        set_meta(con, "last_scan", str(now))
        con.commit()

        stats = {
            "ok": True, "total": len(cur), "new": len(new_rows), "changed": len(changed),
            "moved": len(moved), "missing": len(missing), "queued": jobs_n,
            "walk_s": round(walk_s, 1), "total_s": round(time.time() - t0, 1),
        }
        # 持久化本轮真实用时，供前端“重新扫描”弹窗估算耗时（重启后仍在）
        set_meta(con, "last_scan_stats", json.dumps(stats, ensure_ascii=False))
        # 扫描完成 → 失效 facets/stats 缓存，新类型/新目录立即可见
        from . import search as _S
        _S._FACETS_CACHE = None
        _S._STATS_CACHE = None
        say(f"扫描完成 {stats}")
        return stats
    finally:
        con.close()


if __name__ == "__main__":
    from .config import setup_logging
    setup_logging()
    print(scan())
