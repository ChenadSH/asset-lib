"""抽样验证各类格式的缩略图提取，输出到 D 盘供人工查看。"""
import os
import sys
import time
import collections

sys.path.insert(0, r"D:\asset-lib")
sys.stdout.reconfigure(encoding="utf-8")

from app.extract import extract, phash, save_thumb

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"E:\JLR"
OUT = r"D:\asset-lib\logs\_probe"
os.makedirs(OUT, exist_ok=True)

WANT = {".jpg", ".jpeg", ".png", ".psd", ".psb", ".ai", ".pdf", ".tif", ".dng", ".pptx", ".mov"}
found = collections.defaultdict(list)
for dp, dns, fns in os.walk(ROOT):
    low = dp.lower()
    if "recycle" in low or "system volume" in low:
        dns[:] = []
        continue
    for f in fns:
        e = os.path.splitext(f)[1].lower()
        if e in WANT and len(found[e]) < 3:
            found[e].append(os.path.join(dp, f))
    if found and all(len(found.get(k, [])) >= 3 for k in WANT):
        break

print("样本:", {k: len(v) for k, v in found.items()})
print()
for ext in sorted(found):
    for i, p in enumerate(found[ext]):
        t0 = time.time()
        im = extract(p, ext, 448)
        dt = (time.time() - t0) * 1000
        size_mb = os.path.getsize(p) / 1024**2
        if im is None:
            print("%-6s FAIL  %7.1f MB  %s" % (ext, size_mb, os.path.basename(p)[:60]))
            continue
        ph = phash(im)
        dest = os.path.join(OUT, f"{ext.strip('.')}_{i}.webp")
        ok = save_thumb(im, dest, 448, 82)
        kb = os.path.getsize(dest) / 1024 if ok else 0
        print("%-6s OK    %7.1f MB → %dx%-4d %6.1fms  thumb %5.1fKB  ph=%016x  %s"
              % (ext, size_mb, im.width, im.height, dt, kb, ph, os.path.basename(p)[:45]))
