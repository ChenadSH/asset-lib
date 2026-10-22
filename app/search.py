"""检索层。

两种检索路径，按「命中规模」自适应切换 —— 这是实测出来的结论：

  命中少（< 2000）  → FTS5 + ORDER BY rank   大字报 0.4ms / hero film 2.5ms
  命中多（>= 2000） → FTS 子查询 + mtime 索引   defender 76ms（rank 方式要 239ms）

短词（1–2 字符）FTS 的 trigram 分词根本不支持，必须走 LIKE；
LIKE 配合 idx_status_mtime 索引可提前终止：卫士 34ms / 路虎 1ms。
不可达时（命中极少）退化为全表扫描，约 180ms，仍在可接受范围。
"""
from __future__ import annotations

import logging
import time

from .db import connect

log = logging.getLogger("search")

COUNT_CAP = 20000          # 全文检索的命中数上限
COUNT_CAP_LIKE = 1000      # LIKE 路径的上限：再往上统计要全表扫，实测 1000 是性价比拐点
RANK_LIMIT = 2000          # 小于此规模用 rank 排序，兼顾相关度
ROW_COLS = ("f.id, f.path, f.name, f.ext, f.kind, f.size, f.mtime, f.top, f.year, "
            "f.dev, f.ino, f.thumb, f.phash")


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_phrase(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'


def _filters(kinds, tops, years, prefix="f.") -> tuple[str, dict]:
    sql, params = [], {}
    for group, (col, vals) in {
        "kind": ("kind", kinds),
        "top": ("top", tops),
        "year": ("year", years),
    }.items():
        if not vals:
            continue
        keys = []
        for i, v in enumerate(vals):
            if v in (None, ""):
                continue
            key = f"{group}{i}"
            if col == "year":
                try:
                    params[key] = int(v)
                except (TypeError, ValueError):
                    continue
            else:
                params[key] = v
            keys.append(f":{key}")
        if keys:
            sql.append(f"{prefix}{col} IN ({','.join(keys)})")
    return (" AND " + " AND ".join(sql)) if sql else "", params


def _row_to_item(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"] + (r["ext"] or ""),
        "stem": r["name"],
        "path": r["path"],
        "ext": (r["ext"] or "").lstrip("."),
        "kind": r["kind"],
        "size": r["size"],
        "mtime": r["mtime"],
        "top": r["top"],
        "year": r["year"],
        "thumb": f"/thumb/{r['dev']}_{r['ino']}" if r["thumb"] == 1 else None,
        "score": None,
        "same_source": False,
    }


def _sort_page(items: list[dict], q: str) -> list[dict]:
    """只在第一页做相关度微调：文件名命中优先，其次文件名更短。"""
    ql = q.lower()

    def key(it):
        nm = it["stem"].lower()
        exact = 0 if nm == ql else (1 if nm.startswith(ql) else (2 if ql in nm else 3))
        return (exact, len(nm), -(it["mtime"] or 0))

    return sorted(items, key=key)


def name_search(q: str, kinds=None, tops=None, years=None, offset: int = 0, limit: int = 60) -> dict:
    t0 = time.perf_counter()
    q = (q or "").strip()
    con = connect(timeout=8)
    try:
        where_f, fp = _filters(kinds, tops, years)

        if not q:
            sql = ("SELECT " + ROW_COLS + " FROM files f WHERE f.status='ready'" + where_f +
                   " ORDER BY f.mtime DESC LIMIT :lim OFFSET :off")
            rows = con.execute(sql, {**fp, "lim": limit, "off": offset}).fetchall()
            total = con.execute(
                "SELECT count(*) c FROM (SELECT 1 FROM files f WHERE f.status='ready'"
                + where_f + f" LIMIT {COUNT_CAP + 1})", fp).fetchone()["c"]
            capped = total > COUNT_CAP
            total = min(total, COUNT_CAP)
            mode = "browse"
        elif len(q) <= 2:
            mode = "like"
            rows, total, capped = _like(con, q, where_f, fp, offset, limit)
        else:
            mode = "fts"
            try:
                rows, total, capped = _fts(con, q, where_f, fp, offset, limit)
            except Exception as ex:  # FTS 语法异常等，退回 LIKE
                log.debug("FTS 失败转 LIKE: %s", ex)
                mode = "like"
                rows, total, capped = _like(con, q, where_f, fp, offset, limit)

        items = [_row_to_item(r) for r in rows]
        if offset == 0 and q:
            items = _sort_page(items, q)

        return {
            "total": total,
            "capped": capped,
            "took_ms": round((time.perf_counter() - t0) * 1000, 1),
            "mode": mode,
            "items": items,
            "q": q,
        }
    finally:
        con.close()


def _count_fts(con, q, where_f, fp) -> int:
    return con.execute(
        "SELECT count(*) c FROM (SELECT 1 FROM files_fts JOIN files f ON f.id=files_fts.rowid "
        "WHERE files_fts MATCH :m AND f.status='ready'" + where_f +
        f" LIMIT {COUNT_CAP + 1})",
        {**fp, "m": _fts_phrase(q)}).fetchone()["c"]


def _fts(con, q, where_f, fp, offset, limit):
    m = _fts_phrase(q)
    total = _count_fts(con, q, where_f, fp)

    if total <= RANK_LIMIT:
        # 小结果集：用 rank，相关度更好
        sql = ("SELECT " + ROW_COLS + " FROM files_fts JOIN files f ON f.id=files_fts.rowid "
               "WHERE files_fts MATCH :m AND f.status='ready'" + where_f +
               " ORDER BY rank LIMIT :lim OFFSET :off")
    else:
        # 大结果集：把匹配集作为子查询，外层走 mtime 索引提前终止
        sql = ("SELECT " + ROW_COLS + " FROM files f "
               "WHERE f.id IN (SELECT rowid FROM files_fts WHERE files_fts MATCH :m) "
               "AND f.status='ready'" + where_f +
               " ORDER BY f.mtime DESC LIMIT :lim OFFSET :off")

    rows = con.execute(sql, {**fp, "m": m, "lim": limit, "off": offset}).fetchall()
    return rows, min(total, COUNT_CAP), total > COUNT_CAP


def _like(con, q, where_f, fp, offset, limit):
    like = "%" + _escape_like(q) + "%"
    cond = ("f.status='ready' AND (f.name LIKE :like ESCAPE '\\' "
            "OR f.path LIKE :like ESCAPE '\\')" + where_f)
    sql = ("SELECT " + ROW_COLS + " FROM files f WHERE " + cond +
           " ORDER BY f.mtime DESC LIMIT :lim OFFSET :off")
    rows = con.execute(sql, {**fp, "like": like, "lim": limit, "off": offset}).fetchall()
    total = con.execute(
        "SELECT count(*) c FROM (SELECT 1 FROM files f WHERE " + cond +
        f" LIMIT {COUNT_CAP_LIKE + 1})", {**fp, "like": like}).fetchone()["c"]
    return rows, min(total, COUNT_CAP_LIKE), total > COUNT_CAP_LIKE


def get_file(fid: int) -> dict | None:
    con = connect(timeout=8)
    try:
        r = con.execute("SELECT " + ROW_COLS + ", f.status FROM files f WHERE f.id=?", (fid,)).fetchone()
        if not r:
            return None
        d = _row_to_item(r)
        d["status"] = r["status"]
        return d
    finally:
        con.close()


_FACETS_CACHE: tuple[float, dict] | None = None
FACETS_TTL = 60.0    # 三次全表 GROUP BY 实测约 1.2s，前端每次刷新都会拉，缓存 1 分钟


def facets() -> dict:
    global _FACETS_CACHE
    now = time.time()
    if _FACETS_CACHE and now - _FACETS_CACHE[0] < FACETS_TTL:
        return dict(_FACETS_CACHE[1])
    con = connect(timeout=8)
    try:
        kinds = [(r["kind"], r["c"]) for r in con.execute(
            "SELECT kind, count(*) c FROM files WHERE status='ready' GROUP BY kind ORDER BY c DESC")]
        tops = [(r["top"], r["c"]) for r in con.execute(
            "SELECT top, count(*) c FROM files WHERE status='ready' GROUP BY top ORDER BY c DESC")]
        years = [(r["year"], r["c"]) for r in con.execute(
            "SELECT year, count(*) c FROM files WHERE status='ready' AND year IS NOT NULL "
            "GROUP BY year ORDER BY year DESC")]
        out = {"kinds": kinds, "tops": tops, "years": years}
    finally:
        con.close()
    _FACETS_CACHE = (now, out)
    return dict(out)


_STATS_CACHE: tuple[float, dict] | None = None
STATS_TTL = 10.0    # 前端每 5 秒轮询一次；这些 count 在 27 万行上要几百毫秒，不能每次都算


def stats() -> dict:
    global _STATS_CACHE
    now = time.time()
    if _STATS_CACHE and now - _STATS_CACHE[0] < STATS_TTL:
        return dict(_STATS_CACHE[1])
    con = connect(timeout=8)
    try:
        g = lambda sql, *a: (con.execute(sql, a).fetchone() or [0])[0]
        out = {
            "total": g("SELECT count(*) FROM files"),
            "ready": g("SELECT count(*) FROM files WHERE status='ready'"),
            "missing": g("SELECT count(*) FROM files WHERE status='missing'"),
            "thumbs": g("SELECT count(*) FROM files WHERE thumb=1"),
            "thumb_failed": g("SELECT count(*) FROM files WHERE thumb=2"),
            # thumb_pending 已移除：thumb=0 AND status='ready' 要扫 19 万行（实测 415ms），
            # 而它在界面上没有用处，待处理量直接用 queue 表示
            "embeddings": g("SELECT count(*) FROM files WHERE vec_row IS NOT NULL"),
            "queue": g("SELECT count(*) FROM queue WHERE job='thumb'"),
            "last_scan": g("SELECT v FROM meta WHERE k='last_scan'"),
        }
    finally:
        con.close()
    _STATS_CACHE = (now, out)
    return dict(out)
