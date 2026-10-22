import sys, os, time, statistics
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, r"D:\asset-lib")
sys.stdout.reconfigure(encoding="utf-8")
from app.db import connect
from app.extract import extract

con = connect()
rows = con.execute(
    "SELECT path, ext, size FROM files WHERE kind='raster' AND status='ready' "
    "AND ext IN ('.jpg','.jpeg','.png') ORDER BY id LIMIT 120").fetchall()
con.close()
print("样本", len(rows), "个，平均 %.0f KB" % (sum(r["size"] for r in rows) / len(rows) / 1024))


def one(r):
    t0 = time.perf_counter()
    im = extract(r["path"], r["ext"], 448)
    return (time.perf_counter() - t0), r["size"], (im is not None)


t0 = time.time()
res = [one(r) for r in rows]
el = time.time() - t0
print("单线程 %d 个：%.1fs → %.1f 个/秒  解码均值 %.0f ms" % (len(res), el, len(res) / el, statistics.mean(x[0] for x in res) * 1000))

t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    res2 = list(ex.map(one, rows))
el2 = time.time() - t0
print("8 线程 %d 个：%.1fs → %.1f 个/秒" % (len(res2), el2, len(res2) / el2))
print("失败数", sum(1 for x in res2 if not x[2]))
