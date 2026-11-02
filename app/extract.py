"""各格式的缩略图与感知哈希提取。

统一契约：extract(path, ext) -> PIL.Image | None
任何失败都返回 None，绝不抛异常打断队列。
"""
from __future__ import annotations

import io
import logging
import os
import struct

from PIL import Image

log = logging.getLogger("extract")

Image.MAX_IMAGE_PIXELS = None  # 印刷级 TIFF 动辄上亿像素，放开限制

PSD_SIGS = (b"8BIM", b"MeSa", b"AgHg", b"PHUT", b"DCSR", b"8B64")

_HEIF_READY: bool | None = None


def _ensure_heif() -> bool:
    global _HEIF_READY
    if _HEIF_READY is None:
        try:
            import pillow_heif  # type: ignore
            pillow_heif.register_heif_opener()
            _HEIF_READY = True
        except Exception:
            _HEIF_READY = False
    return _HEIF_READY


def psd_thumbnail_bytes(path: str) -> bytes | None:
    """读 PSD/PSB 的 image resource 1036。

    实测：1036 的数据是「28 字节 TIFF 风格头 + JPEG 流」，
    不是裸 JPEG，必须先跳过 28 字节再判断 FFD8。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(26)
            if head[:4] != b"8BPS":
                return None
            f.seek(26)
            cmd_len = struct.unpack(">I", f.read(4))[0]
            f.seek(cmd_len, 1)
            res_len = struct.unpack(">I", f.read(4))[0]
            end = f.tell() + res_len
            while f.tell() < end - 12:
                sig = f.read(4)
                if sig not in PSD_SIGS:
                    return None
                rid = struct.unpack(">H", f.read(2))[0]
                nlen = f.read(1)[0]
                pad = nlen + 1
                if pad % 2:
                    pad += 1
                f.seek(pad - 1, 1)
                sz = struct.unpack(">I", f.read(4))[0]
                if rid == 1036:
                    data = f.read(sz)
                    if data[:2] == b"\xff\xd8":
                        return data
                    # 跳过 28 字节头
                    if data[28:30] == b"\xff\xd8":
                        return data[28:]
                    # 兜底：在前 64 字节内找 SOI
                    i = data.find(b"\xff\xd8", 0, 64)
                    if i >= 0:
                        return data[i:]
                    return None
                f.seek(sz + (sz % 2), 1)
    except Exception:
        return None
    return None


def _from_pillow(path: str) -> Image.Image | None:
    im = Image.open(path)
    # JPEG 内嵌在 TIFF/PSD 时 draft 能显著加速解码
    try:
        im.draft("RGB", (512, 512))
    except Exception:
        pass
    if im.mode in ("CMYK", "LAB", "I;16", "I", "F"):
        im = im.convert("RGB")
    elif im.mode == "P":
        im = im.convert("RGB")
    elif im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.load()
    return im


def _from_pdf(path: str, max_side: int) -> Image.Image | None:
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
    except Exception:
        import fitz  # type: ignore  # 旧版本
    doc = fitz.open(path)
    try:
        if doc.page_count == 0:
            return None
        page = doc.load_page(0)
        r = page.rect
        base = max(r.width, r.height) or 1
        zoom = max(0.3, min(3.0, max_side / base * 1.6))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def _from_pptx(path: str) -> Image.Image | None:
    from pptx import Presentation
    prs = Presentation(path)
    best = None
    best_area = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            try:
                if shape.shape_type == 13 or hasattr(shape, "image"):  # PICTURE
                    blob = shape.image.blob
                    w = getattr(shape, "width", 0) or 0
                    h = getattr(shape, "height", 0) or 0
                    area = w * h
                    if area >= best_area:
                        best_area, best = area, blob
            except Exception:
                continue
    if best:
        return Image.open(io.BytesIO(best))
    return None


def _from_raw(path: str) -> Image.Image | None:
    import rawpy  # type: ignore
    import numpy as np

    with rawpy.imread(path) as raw:
        try:
            t = raw.extract_thumb()
            fmt = str(t.format)
            if "JPEG" in fmt:
                return Image.open(io.BytesIO(t.data))
            if "BITMAP" in fmt:
                return Image.fromarray(t.data)
        except Exception:
            pass
        # 部分 DNG（如无人机直出）不含内嵌预览，退回半尺寸解码，实测约 0.4s
        rgb = raw.postprocess(half_size=True, use_camera_wb=True,
                              no_auto_bright=True, output_bps=8)
        return Image.fromarray(np.asarray(rgb))


def extract(path: str, ext: str, max_side: int = 448) -> Image.Image | None:
    """按扩展名选择提取策略。"""
    e = ext.lower()
    try:
        if e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            return _from_pillow(path)

        if e in (".tif", ".tiff"):
            return _from_pillow(path)

        if e in (".heic", ".heif", ".avif"):
            if not _ensure_heif():
                return None
            return _from_pillow(path)

        if e in (".psd", ".psb"):
            b = psd_thumbnail_bytes(path)
            if b:
                im = Image.open(io.BytesIO(b))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                return im
            if e == ".psd":  # PSB 不支持 Pillow 完整解码
                return _from_pillow(path)
            return None

        if e == ".ai":
            with open(path, "rb") as f:
                magic = f.read(4)
            if magic == b"%PDF":
                return _from_pdf(path, max_side)
            if magic == b"8BPS":
                b = psd_thumbnail_bytes(path)
                if b:
                    return Image.open(io.BytesIO(b))
            return None

        if e == ".pdf":
            return _from_pdf(path, max_side)

        if e in (".pptx", ".ppt"):
            return _from_pptx(path)

        if e in (".dng", ".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2"):
            return _from_raw(path)

        if e == ".eps":
            return _from_pillow(path)  # 需要 Ghostscript，通常失败
    except Exception as ex:
        log.debug("extract failed %s: %s", os.path.basename(path), ex)
    return None


# ---------------- pHash（纯 numpy，不依赖 scipy） ----------------
_DCT_MAT: "np.ndarray | None" = None


def _dct_matrix(n: int = 32):
    import numpy as np
    global _DCT_MAT
    if _DCT_MAT is not None and _DCT_MAT.shape[0] == n:
        return _DCT_MAT
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0, :] *= np.sqrt(0.5)
    _DCT_MAT = m.astype(np.float32)
    return _DCT_MAT


def phash(im: Image.Image) -> int:
    """64 位感知哈希，用于识别「同一素材的不同版本」。

    返回有符号 64 位：SQLite 的 INTEGER 只接受 -2^63..2^63-1，
    直接存无符号值会报 "Python int too large to convert to SQLite INTEGER"。
    """
    import numpy as np
    g = im.convert("L").resize((32, 32), Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    m = _dct_matrix(32)
    d = m @ a @ m.T
    d = d[:8, :8].flatten()
    med = np.median(d[1:])
    bits = 0
    for v in d:
        bits = (bits << 1) | (1 if v > med else 0)
    return bits - (1 << 64) if bits >= (1 << 63) else bits


def hamming(a: int, b: int) -> int:
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def save_thumb(im: Image.Image, dest: str, max_side: int = 448, quality: int = 82) -> bool:
    try:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        im.save(tmp, "WEBP", quality=quality, method=4)
        os.replace(tmp, dest)
        return True
    except Exception as ex:
        log.debug("save_thumb failed: %s", ex)
        return False


# ---------------- 视频抽帧 ----------------
# 帧文件命名：代表帧 <dev>_<ino>.webp，其余 <dev>_<ino>_f<i>.webp（i 从 1 起）。
# 代表帧沿用普通缩略图命名，前端 /thumb/<key> 无需区分类型。

_ff_cache: dict = {}


def ffmpeg_bin(cfg=None) -> str | None:
    """定位 ffmpeg。优先配置项 ffmpeg_path，其次 PATH；结果缓存。"""
    if "ffmpeg" in _ff_cache:
        return _ff_cache["ffmpeg"]
    import shutil
    cand = None
    cfg_path = getattr(cfg, "ffmpeg_path", "") if cfg else ""
    if cfg_path and os.path.isfile(cfg_path):
        cand = cfg_path
    if not cand:
        cand = shutil.which("ffmpeg")
    _ff_cache["ffmpeg"] = cand
    return cand


def _ffprobe_bin() -> str | None:
    if "ffprobe" in _ff_cache:
        return _ff_cache["ffprobe"]
    import shutil
    cand = shutil.which("ffprobe")
    if not cand:
        ff = _ff_cache.get("ffmpeg")
        if ff:
            p = os.path.join(os.path.dirname(ff), "ffprobe" + (".exe" if os.name == "nt" else ""))
            cand = p if os.path.isfile(p) else None
    _ff_cache["ffprobe"] = cand
    return cand


def video_duration(path: str, timeout: int = 60) -> float | None:
    """ffprobe 读容器时长（秒）。读不到返回 None（损坏 / 网络超时 / 无元数据）。"""
    probe = _ffprobe_bin()
    if not probe:
        return None
    import subprocess
    try:
        r = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        out = (r.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        return float(out[0]) if out and out[0] not in ("N/A", "") else None
    except Exception:
        return None


def plan_video_frames(duration: float | None, per_minute: int = 5,
                      lo: int = 5, hi: int = 50) -> int:
    """帧数策略：时长 × per_minute，夹在 [lo, hi]。

    1 分钟 → 5 帧、5 分钟 → 25 帧、10 分钟及以上 → 50 帧（per_minute=5 时）。
    """
    if not duration or duration <= 0:
        return lo
    n = int(round(duration / 60.0 * per_minute))
    return max(lo, min(hi, n))


def grab_frame(path: str, t: float, max_side: int = 448, timeout: int = 120) -> Image.Image | None:
    """取视频 t 秒处一帧，经 stdout 管道直读内存，全程不落盘。"""
    ff = ffmpeg_bin()
    if not ff:
        return None
    import subprocess
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error",
             "-ss", f"{max(0.0, t):.3f}", "-i", path,
             "-frames:v", "1",
             "-vf", f"scale=w={max_side}:h={max_side}:force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=timeout, creationflags=flags)
        if not r.stdout:
            return None
        return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except Exception:
        return None


def extract_video_frames(path: str, base: str, n: int,
                         max_side: int = 448, quality: int = 82,
                         duration: float | None = None) -> int:
    """给视频均匀抽 n 帧：f0 存 <base>.webp（代表帧），其余 <base>_f<i>.webp。

    返回成功保存的帧数。单帧失败跳过，只要有一帧成功即视为部分成功。
    """
    if n <= 0:
        return 0
    dur = duration if duration and duration > 0 else video_duration(path)
    saved = 0
    for i in range(n):
        # 均匀取每帧的中点时刻；尾帧往回收 0.5%，避免越过最后一帧拿不到画面
        t = (dur * (i + 0.5) / n) if dur else (1.0 + i * 2.0)
        if dur:
            t = min(t, max(0.0, dur * 0.995))
        im = grab_frame(path, t, max_side)
        dest = f"{base}.webp" if i == 0 else f"{base}_f{i}.webp"
        if im is not None and save_thumb(im, dest, max_side, quality):
            saved += 1
    return saved


def cleanup_video_frames(base: str) -> int:
    """删掉某个视频的额外帧缩略图（代表帧 <base>.webp 除外）。重抽前调用。"""
    import glob
    n = 0
    for f in glob.glob(f"{base}_f*.webp"):
        try:
            os.remove(f)
            n += 1
        except OSError:
            pass
    return n

