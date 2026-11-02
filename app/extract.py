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


def _pptx_media_fallback(path: str, max_side: int = 448) -> Image.Image | None:
    """python-pptx 打不开或找不到图片时的兜底：直接翻 zip 的 ppt/media/。

    素材库里的 PPT 失败分三类，python-pptx 全都无能为力：
    - 内嵌视频 CRC 损坏 → 整包加载抛 BadZipFile（zip 逐 member 读可绕开）
    - 只嵌视频、没有静态图 → 正常打开但返回 None（这时抽内嵌视频一帧）
    - 超大文件（4GB+）→ 全量加载又慢又占内存（zip 只读 central directory）
    """
    import zipfile
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return None
    pics: list[str] = []
    vids: list[str] = []
    for info in z.infolist():
        low = info.filename.lower()
        if not low.startswith("ppt/media/"):
            continue
        if low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
                         ".tif", ".tiff")):
            pics.append(info.filename)
        elif low.endswith((".mp4", ".m4v", ".mov", ".wmv", ".avi")):
            vids.append(info.filename)

    # 图片：按体积降序最多试 6 张，CRC 坏的 member 单独跳过不拖累整包
    for name in sorted(pics, key=lambda n: z.getinfo(n).file_size, reverse=True)[:6]:
        try:
            im = Image.open(io.BytesIO(z.read(name)))
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            if im.width >= 100 and im.height >= 100:
                z.close()
                return im
        except Exception:  # noqa: BLE001
            continue

    # 没有可用图片 → 抽最大的内嵌视频一帧。media member 必须落盘再喂
    # ffmpeg（moov 在尾的 mp4 没法从管道流式读），临时目录用完即清。
    if vids:
        import tempfile
        biggest = max(vids, key=lambda n: z.getinfo(n).file_size)
        try:
            with tempfile.TemporaryDirectory(prefix="pptx_vid_") as td:
                fp = os.path.join(td, "media" + os.path.splitext(biggest)[1])
                with z.open(biggest) as src, open(fp, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 22)
                        if not chunk:
                            break
                        dst.write(chunk)
                im = grab_frame(fp, 1.0, max_side)
                if im is not None:
                    z.close()
                    return im
        except Exception:  # noqa: BLE001
            pass
    try:
        z.close()
    except Exception:
        pass
    return None


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

        if e in (".pptx", ".pptm"):
            # zip 直读优先：比 python-pptx 快一个量级、省内存，还能绕开
            # 内嵌视频 CRC 损坏；拿不到可解码图片（如全 EMF 矢量）再回退
            im = _pptx_media_fallback(path, max_side)
            if im is not None:
                return im
            return _from_pptx(path)

        if e == ".ppt":
            return None  # 二进制老格式，python-pptx 不支持

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

# ffmpeg 原生解不了的容器：RED RAW（.r3d）需要 REDCINE-X 先转码，
# 实测 ffmpeg 直接报 "Invalid data found when processing input"，探测时长也是 None。
# 这类文件直接跳过，别在队列里反复重试白耗时间。
VIDEO_SKIP = {".r3d"}

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
    """ffprobe 读容器时长（秒）。读不到返回 None（损坏 / 网络超时 / 无元数据）。

    一并确认存在视频流：素材库里有一批「wav 改名成 .avi」的假视频，
    旧逻辑会拿着有效时长白跑 N 次 ffmpeg 才失败，现在这一层就拦掉。
    """
    probe = _ffprobe_bin()
    if not probe:
        return None
    import json
    import subprocess
    try:
        r = subprocess.run(
            [probe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type", "-of", "json", path],
            capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        info = json.loads((r.stdout or b"{}").decode("utf-8", "ignore") or "{}")
        streams = info.get("streams") or []
        if not any(s.get("codec_type") == "video" for s in streams):
            return None
        raw = (info.get("format") or {}).get("duration")
        return float(raw) if raw not in (None, "N/A", "") else None
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


def _split_jpeg(buf: bytes) -> list[bytes]:
    """把 ffmpeg image2pipe 输出的连续 JPEG 流拆成单张。"""
    out = []
    i = 0
    n = len(buf)
    while i < n - 1:
        s = buf.find(b"\xff\xd8", i)
        if s < 0:
            break
        e = buf.find(b"\xff\xd9", s + 2)
        if e < 0:
            break
        out.append(buf[s:e + 2])
        i = e + 2
    return out


def plan_frame_spans(size: int, dur: float, n: int,
                     budget: int = 200_000_000) -> list[tuple[int, int]]:
    """把 n 帧切成若干「一次 ffmpeg 解出的区间」，返回 [(起始序号, 帧数)]。

    单次 ffmpeg 的进程启动 + 容器打开是固定开销（实测 250~500ms），
    逐帧起进程在短视频上浪费过半时间；但一股脑全片解码对高码率母版
    （4K ProRes 动辄 100+ MB/s）是灾难。

    因此按「解码该区间要读多少字节」估预算：size * span / dur，
    超预算就切细，最细退化成逐帧 seek —— 与旧版行为一致，不会更差。
    """
    if n <= 0:
        return []
    if not dur or dur <= 0 or not size:
        # 没有时长/体积信息时保守地逐帧处理
        return [(i, 1) for i in range(n)]
    step = dur / n
    cost_per_frame = size * step / dur          # 解码一帧跨度的字节成本
    if cost_per_frame <= 0:
        return [(0, n)]
    max_k = max(1, int(budget / cost_per_frame))
    max_k = min(max_k, 8, n)                    # 单组不超过 8 帧，fps 太稀疏会丢帧
    spans = []
    i = 0
    while i < n:
        k = min(max_k, n - i)
        spans.append((i, k))
        i += k
    return spans


def _decode_span(path: str, start_i: int, count: int, dur: float, n: int,
                 max_side: int, timeout: int = 300) -> list:
    """一次 ffmpeg 解出 [start_i, start_i+count) 这些帧。"""
    ff = ffmpeg_bin()
    if not ff:
        return []
    import subprocess
    step = dur / n

    # 单帧必须走精确 seek：用 fps 滤镜取 1 帧时，ffmpeg 要先解码到
    # 该组中点才吐出第一帧，对 4K ProRes 母版（145MB/s）等于白读 800MB，
    # 实测比逐帧 seek 慢 4.5 倍。
    if count == 1:
        t = min(dur * (start_i + 0.5) / n, max(0.0, dur * 0.995))
        im = grab_frame(path, t, max_side, timeout=min(timeout, 180))
        return [im] if im is not None else []

    start = max(0.0, (start_i + 0.5) * step - step * 0.5)
    span = count * step
    if start + span > dur:
        span = max(step * 0.5, dur - start)
    if span <= 0:
        return []
    fps = count / span
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error",
             "-ss", f"{start:.3f}", "-i", path, "-t", f"{span:.3f}",
             "-vf", f"fps={fps:.8f},"
                    f"scale=w={max_side}:h={max_side}:force_original_aspect_ratio=decrease",
             "-frames:v", str(count),
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=timeout, creationflags=flags)
        ims = []
        for blob in _split_jpeg(r.stdout or b""):
            try:
                ims.append(Image.open(io.BytesIO(blob)).convert("RGB"))
            except Exception:  # noqa: BLE001
                pass
        return ims
    except Exception:  # noqa: BLE001
        return []


def extract_video_frames(path: str, base: str, n: int,
                         max_side: int = 448, quality: int = 82,
                         duration: float | None = None,
                         size: int = 0) -> int:
    """给视频均匀抽 n 帧：f0 存 <base>.webp（代表帧），其余 <base>_f<i>.webp。

    按解码预算分组，能合并的帧一次 ffmpeg 出（短视频通常 1 次调用搞定），
    高码率大文件自动退化为逐帧 seek。返回成功保存的帧数。
    """
    if n <= 0:
        return 0
    dur = duration if duration and duration > 0 else video_duration(path)
    if not dur:
        return 0
    saved = 0
    for start_i, count in plan_frame_spans(size, dur, n):
        ims = _decode_span(path, start_i, count, dur, n, max_side)
        for k, im in enumerate(ims):
            i = start_i + k
            dest = f"{base}.webp" if i == 0 else f"{base}_f{i}.webp"
            if save_thumb(im, dest, max_side, quality):
                saved += 1
        # 批量解码少给了帧（fps 取整/尾部截断）时，用精确 seek 补齐
        if len(ims) < count:
            for k in range(len(ims), count):
                i = start_i + k
                t = min(dur * (i + 0.5) / n, max(0.0, dur * 0.995))
                im = grab_frame(path, t, max_side)
                dest = f"{base}.webp" if i == 0 else f"{base}_f{i}.webp"
                if im is not None and save_thumb(im, dest, max_side, quality):
                    saved += 1
    return saved


def cleanup_video_frames(base: str, keep: int = 0) -> int:
    """删掉序号 > keep 的副帧（代表帧 <base>.webp 永不删）。

    只在重抽的帧数比上次少时才调用：缩略图目录有 20 万文件，
    每次 glob 要 600ms，绝不能放进热路径。
    """
    d = os.path.dirname(base)
    prefix = os.path.basename(base) + "_f"
    n = 0
    try:
        with os.scandir(d) as it:
            for e in it:
                if not e.name.startswith(prefix):
                    continue
                try:
                    idx = int(e.name[len(prefix):-5])
                except ValueError:
                    continue
                if idx > keep:
                    try:
                        os.remove(e.path)
                        n += 1
                    except OSError:
                        pass
    except OSError:
        pass
    return n

