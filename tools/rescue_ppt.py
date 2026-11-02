"""给失败 PPT 一次性补缩略图：用新代码（zip 直读 + 内嵌视频兜底）。

worker 进程加载的还是旧 extract 模块，所以这批任务单独在进程外跑，
跑完写 thumb=1，embed 的跟进编码会自动补向量。worker 之后即使
再扫到这些队列项，也会因为缩略图已存在而直接标记完成。
"""
import os
import sys
import time

sys.path.insert(0, r"D:\asset-lib")
from PIL import Image

from app.config import CFG
from app.db import connect
from app.extract import extract, phash, save_thumb


def main() -> None:
    con = connect()
    rows = con.execute(
        "SELECT id, dev, ino, path, ext FROM files "
        "WHERE kind='doc' AND thumb=0 AND ext IN ('.pptx','.pptm') "
        "ORDER BY size").fetchall()
    con.close()
    print(f"待补图 PPT：{len(rows)} 个", flush=True)

    ok = fail = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        dest = os.path.join(str(CFG.thumb_dir), f"{r['dev']}_{r['ino']}.webp")
        im = None
        try:
            im = extract(r["path"], r["ext"], CFG.thumb_max_side)
        except Exception as ex:  # noqa: BLE001
            print(f"  异常 {type(ex).__name__}: {str(ex)[:100]} | {r['path'][-60:]}", flush=True)
        if im is not None and save_thumb(im, dest, CFG.thumb_max_side, CFG.thumb_quality):
            ph = None
            try:
                ph = phash(Image.open(dest))
            except Exception:  # noqa: BLE001
                pass
            c = connect()
            c.execute("UPDATE files SET thumb=1, phash=? WHERE id=?", (ph, r["id"]))
            c.commit()
            c.close()
            ok += 1
        else:
            fail += 1
        if i % 20 == 0 or i == len(rows):
            rate = i / max(0.1, time.time() - t0)
            print(f"  [{i}/{len(rows)}] 成功 {ok} 失败 {fail}  {rate:.1f} 个/秒", flush=True)
    print(f"完成：成功 {ok}，失败 {fail}", flush=True)


if __name__ == "__main__":
    main()
