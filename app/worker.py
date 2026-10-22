"""缩略图队列消费。

关键工程约束（踩过坑，别改回去）：
1. 扫描线程和 worker 不能同时写库。SQLite WAL 只允许一个写者，
   两进程/两线程并发写会出现 SQLITE_READONLY / SQLITE_BUSY，
   因此 worker 在扫描结束后才启动（见 main.py）。
2. 每批写入用独立连接，失败自动重连重试，避免长连接因 WAL 被 checkpoint 而失效。
3. 失败重试 2 次后标记 thumb=2，不阻塞队列。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import CFG, Config
from .db import connect
from .extract import extract, phash, save_thumb

log = logging.getLogger("worker")

# 分轮次处理：TIFF 单张几百 MB，放最后，避免拖慢首屏可用时间
PASSES = [
    ("raster", "AND f.ext NOT IN ('.tif','.tiff')"),
    ("design", ""),
    ("doc", ""),
    ("raw", ""),
    ("raster", "AND f.ext IN ('.tif','.tiff')"),
]

BATCH = 240


class ThumbWorker:
    def __init__(self, cfg: Config | None = None, workers: int | None = None):
        self.cfg = cfg or CFG
        self.workers = workers or self.cfg.thumb_workers
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.done = 0
        self.failed = 0
        self.started_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="thumb-worker", daemon=True)
        self._thread.start()
        log.info("缩略图 worker 已启动，线程数 %d", self.workers)

    def stop(self) -> None:
        self._stop.set()

    def pending(self) -> int:
        con = connect()
        try:
            return con.execute("SELECT count(*) c FROM queue WHERE job='thumb'").fetchone()["c"]
        finally:
            con.close()

    # ---------- 内部 ----------
    def _fetch(self, con, kind: str, extra: str, limit: int) -> list:
        return con.execute(
            "SELECT q.id AS qid, q.attempts, f.id AS fid, f.dev, f.ino, f.path, f.ext "
            "FROM queue q JOIN files f ON f.id=q.file_id "
            "WHERE q.job='thumb' AND f.kind=? " + extra +
            " ORDER BY q.id LIMIT ?",
            (kind, limit),
        ).fetchall()

    def _process(self, row) -> tuple:
        fid = row["fid"]
        dest = os.path.join(str(self.cfg.thumb_dir), f"{row['dev']}_{row['ino']}.webp")
        try:
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                return (row["qid"], fid, 1, None, None)
            im = extract(row["path"], row["ext"], self.cfg.thumb_max_side)
            if im is None:
                return (row["qid"], fid, 0, None, "no_image")
            ph = phash(im)
            if not save_thumb(im, dest, self.cfg.thumb_max_side, self.cfg.thumb_quality):
                return (row["qid"], fid, 0, None, "save_failed")
            return (row["qid"], fid, 1, ph, None)
        except Exception as ex:  # noqa: BLE001
            return (row["qid"], fid, 0, None, f"{type(ex).__name__}: {ex}"[:200])

    def _write(self, results: list) -> bool:
        ok_ids = [r[1] for r in results if r[2] == 1]
        ph_rows = [(r[3], r[1]) for r in results if r[2] == 1 and r[3] is not None]
        bad = [(r[4], r[0], r[1]) for r in results if r[2] == 0]
        ok_q = [r[0] for r in results if r[2] == 1]

        for attempt in range(4):
            con = None
            try:
                con = connect()
                if ok_ids:
                    con.executemany("UPDATE files SET thumb=1 WHERE id=?", [(i,) for i in ok_ids])
                    if ph_rows:
                        con.executemany("UPDATE files SET phash=? WHERE id=?", ph_rows)
                for err, qid, fid in bad:
                    row = con.execute("SELECT attempts FROM queue WHERE id=?", (qid,)).fetchone()
                    att = (row["attempts"] if row else 0) + 1
                    if att >= 2:
                        con.execute("UPDATE files SET thumb=2 WHERE id=?", (fid,))
                        con.execute("DELETE FROM queue WHERE id=?", (qid,))
                    else:
                        con.execute("UPDATE queue SET attempts=?, err=? WHERE id=?", (att, err, qid))
                done_q = ok_q
                con.executemany("DELETE FROM queue WHERE id=?", [(q,) for q in done_q])
                con.commit()
                return True
            except sqlite3.OperationalError as ex:
                log.warning("写库失败（第 %d 次）：%s", attempt + 1, ex)
                time.sleep(0.5 * (attempt + 1))
            except Exception as ex:  # noqa: BLE001
                log.warning("写库异常：%s", ex)
                break
            finally:
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass
        return False

    def _run(self) -> None:
        self.started_at = time.time()
        ex = ThreadPoolExecutor(max_workers=self.workers)
        read_con = connect()
        idle = 0
        t_last = time.time()
        try:
            while not self._stop.is_set():
                batch = []
                for kind, extra in PASSES:
                    batch = self._fetch(read_con, kind, extra, BATCH)
                    if batch:
                        break
                if not batch:
                    idle += 1
                    if idle % 20 == 0:
                        log.info("队列已空，等待中（已完成 %d，失败 %d）", self.done, self.failed)
                        try:
                            read_con.close()
                        except Exception:
                            pass
                        read_con = connect()
                    time.sleep(2)
                    continue
                idle = 0

                results = list(ex.map(self._process, batch))
                ok_n = sum(1 for r in results if r[2] == 1)
                self.done += ok_n
                self.failed += len(results) - ok_n
                self._write(results)

                now = time.time()
                if now - t_last > 30:
                    rate = self.done / max(1, now - self.started_at)
                    log.info("缩略图：完成 %d，失败 %d，%.1f 张/秒", self.done, self.failed, rate)
                    t_last = now
        finally:
            ex.shutdown(wait=False)
            try:
                read_con.close()
            except Exception:
                pass
            log.info("缩略图 worker 退出（完成 %d，失败 %d）", self.done, self.failed)


if __name__ == "__main__":
    from .config import setup_logging
    setup_logging()
    w = ThumbWorker()
    w.start()
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        w.stop()
