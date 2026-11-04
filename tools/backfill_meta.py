"""一次性回填筛选派生字段：mtype / width / height / orient / duration。

- mtype：纯扩展名派生，db 迁移已处理，这里兜底再跑一遍（幂等）
- width/height/orient：从 D 盘缩略图读 header（长宽比与原文件一致；
  图片缩略图由原图等比生成，orient 完全可靠），只 open 不 decode，
  本地盘 ~1ms/个，22 万个几分钟跑完
- duration：仅视频，ffprobe 探测（只读容器头），E 盘是 USB 机械盘，
  8 线程跑 2 万个约 30–60 分钟，断点续跑（duration IS NULL 的才探）

用法：python tools/backfill_meta.py [--skip-duration]
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import CFG, MTYPE_MAP  # noqa: E402
from app.db import connect  # noqa: E402
from app.extract import orient_of, video_duration  # noqa: E402

DB = CFG.db_path
BATCH = 400


def backfill_mtype() -> int:
    con = connect()
    try:
        n = con.execute("SELECT count(*) c FROM files WHERE mtype IS NULL").fetchone()["c"]
        if not n:
            return 0
        for mt, exts in MTYPE_MAP.items():
            con.execute(f"UPDATE files SET mtype='{mt}' WHERE mtype IS NULL "
                        f"AND lower(ext) IN ({','.join('?' * len(exts))})", tuple(exts))
        con.execute("UPDATE files SET mtype='other' WHERE mtype IS NULL")
        con.commit()
        return n
    finally:
        con.close()


def _thumb_dims(key: tuple[int, int]):
    dev, ino = key
    p = CFG.thumb_dir / f"{dev}_{ino}.webp"
    try:
        from PIL import Image
        with Image.open(p) as im:
            return key, im.width, im.height
    except Exception:
        return key, None, None


def backfill_dims() -> int:
    """给有缩略图但缺 width/height 的记录回填（来自 thumb，比例与原图一致）。"""
    con = connect()
    try:
        keys = [(r["dev"], r["ino"]) for r in con.execute(
            "SELECT dev, ino FROM files WHERE thumb=1 AND width IS NULL")]
    finally:
        con.close()
    print(f"[dims] 待回填 {len(keys)} 条", flush=True)
    t0 = time.time()
    done = 0
    for i in range(0, len(keys), 20000):
        chunk = keys[i:i + 20000]
        rows = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            for (key, w, h) in ex.map(_thumb_dims, chunk):
                if w and h:
                    rows.append((w, h, orient_of(w, h), key[0], key[1]))
        con = connect()
        try:
            con.executemany("UPDATE files SET width=?, height=?, orient=? "
                            "WHERE dev=? AND ino=?", rows)
            con.commit()
        finally:
            con.close()
        done += len(rows)
        print(f"[dims] {done}/{len(keys)}  {time.time()-t0:.0f}s", flush=True)
    return done


def backfill_duration() -> int:
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, path FROM files WHERE kind='video' AND duration IS NULL "
            "AND status='ready'").fetchall()
    finally:
        con.close()
    if not rows:
        print("[dur] 无待回填", flush=True)
        return 0
    print(f"[dur] 待探测 {len(rows)} 个视频（8 线程，USB 盘较慢，耐心等）", flush=True)
    t0 = time.time()
    done = failed = 0
    buf: list[tuple] = []

    def probe(r):
        return r["id"], video_duration(r["path"], timeout=45)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fid, dur in ex.map(probe, rows):
            if dur:
                buf.append((round(dur, 2), fid))
            else:
                failed += 1
                buf.append((-1.0, fid))  # 探不出的标 -1，避免每次重跑都重探
            done += 1
            if len(buf) >= BATCH:
                con = connect()
                try:
                    con.executemany("UPDATE files SET duration=? WHERE id=?", buf)
                    con.commit()
                finally:
                    con.close()
                buf = []
                if done % 2000 < BATCH:
                    rate = done / max(1, time.time() - t0)
                    eta = (len(rows) - done) / max(0.1, rate) / 60
                    print(f"[dur] {done}/{len(rows)}  {rate:.1f}/s  剩余约 {eta:.0f} 分钟",
                          flush=True)
    if buf:
        con = connect()
        try:
            con.executemany("UPDATE files SET duration=? WHERE id=?", buf)
            con.commit()
        finally:
            con.close()
    print(f"[dur] 完成 {done}，探不出(标-1) {failed}，耗时 {(time.time()-t0)/60:.1f} 分钟",
          flush=True)
    return done


if __name__ == "__main__":
    skip_dur = "--skip-duration" in sys.argv
    n = backfill_mtype()
    print(f"[mtype] 兜底回填 {n} 条", flush=True)
    backfill_dims()
    if not skip_dur:
        backfill_duration()
    print("全部完成", flush=True)
