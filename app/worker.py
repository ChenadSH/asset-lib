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
from .extract import (VIDEO_SKIP, cleanup_video_frames, extract,
                      extract_video_frames, ffmpeg_bin, grab_frame, orient_of,
                      phash, plan_video_frames, save_thumb, video_duration)

log = logging.getLogger("worker")

# 超过这个体积的视频先抽一帧封面落库，再补副帧（实测 500MB+ 的多是
# 4K ProRes 母版，整段抽完要几十秒，先出图体验好得多）
COVER_FIRST_SIZE = 500 * 1024 * 1024

# 分轮次处理：TIFF 单张几百 MB，放最后，避免拖慢首屏可用时间；
# 视频抽帧最慢（每帧一次 seek 解码），排最末，不挤占图片的首屏时间。
# 第三项是排序键：视频按体积升序，让大量小片子先出图，
# 几百 MB 的母版丢到队尾慢慢磨，避免用户以为「所有视频都没预览图」。
PASSES = [
    ("raster", "AND f.ext NOT IN ('.tif','.tiff')", "q.id"),
    ("design", "", "q.id"),
    ("doc", "", "q.id"),
    ("raw", "", "q.id"),
    ("raster", "AND f.ext IN ('.tif','.tiff')", "q.id"),
    ("video", "", "f.size"),
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
    def _fetch(self, con, kind: str, extra: str, limit: int,
               order: str = "q.id") -> list:
        return con.execute(
            "SELECT q.id AS qid, q.attempts, f.id AS fid, f.dev, f.ino, f.path, f.ext, "
            "f.kind, f.frame_count, f.size "
            "FROM queue q JOIN files f ON f.id=q.file_id "
            "WHERE q.job='thumb' AND f.kind=? " + extra +
            " ORDER BY " + order + " LIMIT ?",
            (kind, limit),
        ).fetchall()

    def _process(self, row) -> tuple:
        fid = row["fid"]
        dest = os.path.join(str(self.cfg.thumb_dir), f"{row['dev']}_{row['ino']}.webp")
        try:
            if row["kind"] == "video":
                return self._process_video(row, dest)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                return (row["qid"], fid, 1, None, None, None)
            im = extract(row["path"], row["ext"], self.cfg.thumb_max_side)
            if im is None:
                return (row["qid"], fid, 0, None, "no_image", None)
            ph = phash(im)
            if not save_thumb(im, dest, self.cfg.thumb_max_side, self.cfg.thumb_quality):
                return (row["qid"], fid, 0, None, "save_failed", None)
            # extract 返回原图，宽高即原始分辨率
            extra = {"width": im.width, "height": im.height,
                     "orient": orient_of(im.width, im.height)}
            return (row["qid"], fid, 1, ph, None, extra)
        except Exception as ex:  # noqa: BLE001
            return (row["qid"], fid, 0, None, f"{type(ex).__name__}: {ex}"[:200], None)

    def _process_video(self, row, dest: str) -> tuple:
        """视频抽多帧：f0 作代表帧（沿用普通缩略图命名），f1.. 存 _f<i>.webp。"""
        fid = row["fid"]
        if row["ext"].lower() in VIDEO_SKIP:
            return (row["qid"], fid, 0, None, "unsupported_container", None)
        if not ffmpeg_bin(self.cfg):
            return (row["qid"], fid, 0, None, "ffmpeg_not_found", None)

        base = os.path.join(str(self.cfg.thumb_dir), f"{row['dev']}_{row['ino']}")
        fc = row["frame_count"] or 0
        # 已抽过且代表帧还在 → 直接完成（changed 重扫进队的会被跳过，与图片行为一致）。
        # 但要多验一个末帧：抽帧中途被打断会留下「有代表帧、帧条缺失」的残局，
        # 只查代表帧会把这种半成品永久当成已完成。
        if fc and os.path.exists(dest) and os.path.getsize(dest) > 0:
            last = f"{base}_f{fc - 1}.webp" if fc > 1 else dest
            if os.path.exists(last):
                return (row["qid"], fid, 1, None, None, None)

        dur = video_duration(row["path"])
        if not dur:
            return (row["qid"], fid, 0, None, "probe_failed", None)
        vf = self.cfg.video_frames
        n = plan_video_frames(dur, vf["per_minute"], vf["min"], vf["max"])

        # 帧数可能比上一次少（配置改小），先清掉多余副帧。
        # 注意：缩略图目录有 20 万文件，扫一次要 600ms，只能在这里按需调用，
        # 绝不能无条件放在每个视频的热路径上。
        if fc > n:
            cleanup_video_frames(base, keep=n)

        # 大文件先抽第一帧并立刻落库：整段抽完要几十秒，先让列表里有图，
        # 用户不必盯着占位图等。副帧随后补，失败也不影响已有封面。
        big = (row["size"] or 0) > COVER_FIRST_SIZE
        if big and n > 1 and not os.path.exists(dest):
            t0 = min(dur * 0.5 / n, max(0.0, dur * 0.995))
            im0 = grab_frame(row["path"], t0, self.cfg.thumb_max_side)
            if im0 is not None and save_thumb(im0, dest, self.cfg.thumb_max_side,
                                             self.cfg.thumb_quality):
                self._mark_ready(fid, dest, dur, im0.width, im0.height)

        saved = extract_video_frames(row["path"], base, n,
                                     self.cfg.thumb_max_side,
                                     self.cfg.thumb_quality,
                                     duration=dur, size=row["size"] or 0)
        if saved == 0:
            return (row["qid"], fid, 0, None, "no_frames", None)
        ph = None
        w = h = None
        try:
            from PIL import Image
            with Image.open(dest) as im:
                w, h = im.size
                ph = phash(im)
        except Exception:  # noqa: BLE001
            pass
        return (row["qid"], fid, 1, ph, None,
                {"frame_count": saved, "duration": round(dur, 2),
                 "width": w, "height": h, "orient": orient_of(w, h)})

    def _mark_ready(self, fid: int, dest: str, dur: float | None = None,
                    w: int | None = None, h: int | None = None) -> None:
        """把单个视频提前标成「有缩略图」，不等整批写库。

        worker 是 240 个一批才统一写库的，大文件一批能跑十几分钟；
        封面已经落盘了就先落库，前端刷新即可见。向量由 embed 跟进补。
        """
        ph = None
        try:
            from PIL import Image
            ph = phash(Image.open(dest))
        except Exception:  # noqa: BLE001
            pass
        if not (w and h):
            try:
                from PIL import Image
                with Image.open(dest) as im:
                    w, h = im.size
            except Exception:  # noqa: BLE001
                pass
        for _ in range(3):
            con = None
            try:
                con = connect()
                con.execute("UPDATE files SET thumb=1, phash=?, duration=?, "
                            "width=?, height=?, orient=? WHERE id=?",
                            (ph, dur, w, h, orient_of(w, h), fid))
                con.commit()
                return
            except sqlite3.OperationalError:
                time.sleep(0.3)
            finally:
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass


    def _write(self, results: list) -> bool:
        ok_ids = [r[1] for r in results if r[2] == 1]
        ph_rows = [(r[3], r[1]) for r in results if r[2] == 1 and r[3] is not None]
        # 视频帧数写入（r[5] 是 extra dict）
        fc_rows = [(r[5]["frame_count"], r[1]) for r in results
                   if r[2] == 1 and r[5] and "frame_count" in r[5]]
        # 宽高/横竖屏/时长（图片来自原图尺寸，视频来自代表帧与 probe）
        dim_rows = [(r[5].get("width"), r[5].get("height"), r[5].get("orient"),
                     r[5].get("duration"), r[1]) for r in results
                    if r[2] == 1 and r[5] and ("orient" in r[5] or "duration" in r[5])]
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
                    if fc_rows:
                        con.executemany("UPDATE files SET frame_count=?, frames_done=0 WHERE id=?", fc_rows)
                    if dim_rows:
                        con.executemany("UPDATE files SET width=?, height=?, orient=?, duration=? WHERE id=?",
                                        dim_rows)
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
                for kind, extra, order in PASSES:
                    batch = self._fetch(read_con, kind, extra, BATCH, order)
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
