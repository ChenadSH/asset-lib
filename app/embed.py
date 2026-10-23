"""CLIP 向量层：编码、存储、相似检索。

模型选择：OFA-Sys/chinese-clip-vit-base-patch16（MIT 许可，可商用）
- 中文图文对训练，中文查询效果远好于原版 CLIP
- 512 维，实测单张约 11 ms（P4000）
换模型只需重跑 encode，不影响数据库结构与前端。
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from .config import CFG, resolve_device
from .db import connect, get_meta, set_meta

log = logging.getLogger("embed")

_MODEL = None
_PROC = None
_DEV = "cpu"
_LOCK = threading.Lock()
_DIM = 512


def is_ready() -> bool:
    try:
        con = connect(readonly=True)
        try:
            return int(get_meta(con, "vec_count", "0") or 0) > 0
        finally:
            con.close()
    except Exception:
        return False


def _sync_device(torch, dev: str) -> None:
    """等设备上的异步计算真正结束（CUDA/MPS 都是异步的，计时与读回要同步）。"""
    try:
        if dev == "cuda":
            torch.cuda.synchronize()
        elif dev == "mps":
            torch.mps.synchronize()
    except Exception:
        pass


# ---------------- 模型 ----------------
def load_model():
    global _MODEL, _PROC, _DEV
    with _LOCK:
        if _MODEL is not None:
            return _MODEL, _PROC, _DEV
        import json

        import torch
        from transformers import (BertTokenizerFast, ChineseCLIPImageProcessor,
                                  ChineseCLIPModel, ChineseCLIPProcessor)

        name = CFG.clip_model
        log.info("加载模型 %s …", name)
        t0 = time.time()
        # auto 会按 CUDA > MPS(Apple Silicon) > CPU 选择，显式配置不可用时自动降级
        _DEV = resolve_device(CFG.clip_device)

        # transformers 5 改了 processor 的加载约定（找 processor_config.json、
        # 分词器参数名从 vocab_file 改为 vocab），直接 from_pretrained 会失败，
        # 因此这里用仓库自带的 preprocessor_config.json + vocab.txt 手工组装。
        assets = CFG.clip_dir
        cfg_file, vocab_file = assets / "preprocessor_config.json", assets / "vocab.txt"
        # 仓库原始权重是 pytorch_model.bin，而 transformers 5 只解析 safetensors，
        # 首次使用前已用 tools/convert_weights.py 转过一次，之后完全离线加载
        model_src = str(assets) if (assets / "model.safetensors").exists() else name
        if cfg_file.exists() and vocab_file.exists():
            ip = ChineseCLIPImageProcessor(**json.loads(cfg_file.read_text(encoding="utf-8")))
            tk = BertTokenizerFast(vocab=str(vocab_file), do_lower_case=True)
            _PROC = ChineseCLIPProcessor(image_processor=ip, tokenizer=tk)
        else:
            _PROC = ChineseCLIPProcessor.from_pretrained(name)

        _MODEL = ChineseCLIPModel.from_pretrained(model_src).to(_DEV).eval()
        # 预热，避免首次查询等十几秒
        try:
            dummy = torch.zeros((1, 3, 224, 224), device=_DEV)
            with torch.no_grad():
                _MODEL.get_image_features(pixel_values=dummy)
            _sync_device(torch, _DEV)
        except Exception:
            pass
        log.info("模型就绪，device=%s，耗时 %.1fs", _DEV, time.time() - t0)
        return _MODEL, _PROC, _DEV


# ---------------- 向量存储 ----------------
class VectorStore:
    """vectors.npy 用 memmap 预分配，避免每次追加都整体重写。"""

    def __init__(self, path: Path, dim: int = _DIM):
        self.path = Path(path)
        self.dim = dim
        self.mat: np.memmap | None = None
        self.count = 0
        self.ids = np.zeros(0, dtype=np.int64)  # row -> file_id
        self._load()

    def _load(self) -> None:
        con = connect()
        try:
            self.count = int(get_meta(con, "vec_count", "0") or 0)
            rows = con.execute("SELECT vec_row, id FROM files WHERE vec_row IS NOT NULL").fetchall()
        finally:
            con.close()

        if self.path.exists():
            self.mat = np.load(self.path, mmap_mode="r+")
            cap, dim = self.mat.shape
            if dim != self.dim:
                raise RuntimeError(f"向量维度不匹配：磁盘 {dim}，配置 {self.dim}。请删除 vectors.npy 重新编码")
        else:
            cap = max(200_000, self.count + 50_000)
            self.mat = np.lib.format.open_memmap(self.path, mode="w+", dtype=np.float32, shape=(cap, self.dim))
            log.info("新建向量库 %s，容量 %d × %d", self.path.name, cap, self.dim)

        self.ids = np.zeros(cap, dtype=np.int64)
        for r in rows:
            if 0 <= r["vec_row"] < cap:
                self.ids[r["vec_row"]] = r["id"]

    @property
    def capacity(self) -> int:
        self._ensure_loaded()
        return 0 if self.mat is None else self.mat.shape[0]

    def _ensure_loaded(self) -> None:
        """memmap 映射意外丢失时自动重开，避免一次扩容失败永久废掉查询。"""
        if getattr(self, "mat", None) is None and self.path.exists():
            log.warning("向量库映射丢失，重新打开 %s", self.path.name)
            self.mat = np.load(self.path, mmap_mode="r+")
            if self.count > self.mat.shape[0]:
                self.count = self.mat.shape[0]

    def _grow(self, need: int) -> None:
        self._ensure_loaded()
        assert self.mat is not None
        old_cap = int(self.mat.shape[0])
        new_cap = max(need, int(old_cap * 1.5))
        log.info("向量库扩容 %d → %d", old_cap, new_cap)
        tmp = self.path.with_suffix(".tmp.npy")
        # 先写临时文件并释放其句柄，再替换正式文件；替换前不丢旧映射，
        # 任何一步失败都能恢复原状，绝不让 self.mat 悬空。
        new = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.float32, shape=(new_cap, self.dim))
        try:
            new[:old_cap] = self.mat[:]
            new.flush()
        finally:
            del new                                    # 释放 tmp 句柄，Windows 才能 replace
        self.mat = None                                # 释放旧映射句柄，允许原子替换
        try:
            os.replace(tmp, self.path)
            self.mat = np.load(self.path, mmap_mode="r+")
        except BaseException:
            log.exception("向量库扩容失败，尝试恢复旧映射")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                self.mat = np.load(self.path, mmap_mode="r+")
            except BaseException:
                self.mat = None
            raise
        ids = np.zeros(new_cap, dtype=np.int64)
        ids[: self.ids.shape[0]] = self.ids
        self.ids = ids

    def append(self, vecs: np.ndarray) -> int:
        self._ensure_loaded()
        assert self.mat is not None
        n = vecs.shape[0]
        start = self.count
        if start + n > self.capacity:
            self._grow(start + n)
        self.mat[start:start + n] = vecs
        self.count += n
        return start

    def flush(self) -> None:
        self._ensure_loaded()
        if self.mat is not None:
            self.mat.flush()

    def refresh(self, cur: int) -> bool:
        """增量跟进编码进程新写入的向量：只补 ids 映射，不重建 memmap。

        返回 False 表示无法增量，需要整体重载。
        之前每次 vec_count 变化都整体重载（读 5 万行 + 重开 memmap），
        编码期间每次查询要多花 400ms。
        """
        if cur < self.count or cur > self.capacity:
            return False
        if cur == self.count:
            return True
        con = connect(readonly=True)
        try:
            rows = con.execute(
                "SELECT vec_row, id FROM files WHERE vec_row >= ? AND vec_row < ?",
                (self.count, cur)).fetchall()
        finally:
            con.close()
        for r in rows:
            self.ids[r["vec_row"]] = r["id"]
        self.count = cur
        return True

    def normalize_and_search(self, q: np.ndarray, topk: int = 200) -> tuple[np.ndarray, np.ndarray]:
        self._ensure_loaded()
        if self.count == 0 or self.mat is None:
            return np.zeros(0), np.zeros(0, dtype=np.int64)
        m = np.asarray(self.mat[: self.count])
        q = q.astype(np.float32).reshape(-1)
        q /= (np.linalg.norm(q) + 1e-8)
        sims = m @ q
        k = min(topk, self.count)
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return sims[idx], self.ids[idx]


_STORE: VectorStore | None = None
_STORE_LOCK = threading.Lock()
_STORE_CHECK = 0.0
STORE_RECHECK = 2.0   # 秒：编码进程在另一个进程里追加向量，最多 2 秒后可见


def store() -> VectorStore:
    """按需加载；若编码进程追加了新向量，这里会自动重新挂载。

    编码进行中 vec_count 每批都会变，若每次都重挂载（读 7 万行 + 重开 memmap），
    查询会被拖慢，所以这里做 2 秒节流。
    """
    global _STORE, _STORE_CHECK
    now = time.time()
    with _STORE_LOCK:
        if _STORE is not None and (now - _STORE_CHECK) < STORE_RECHECK:
            return _STORE
        try:
            con = connect(readonly=True)
            try:
                cur = int(get_meta(con, "vec_count", "0") or 0)
            finally:
                con.close()
        except Exception:
            cur = 0
        if _STORE is None:
            _STORE = VectorStore(CFG.vec_path, _DIM)
        elif _STORE.count != cur and not _STORE.refresh(cur):
            log.info("向量库需整体重载（%d → %d）", _STORE.count, cur)
            _STORE = VectorStore(CFG.vec_path, _DIM)
        _STORE_CHECK = now
        return _STORE


# ---------------- 编码 ----------------
def _feat(out):
    """transformers 5 的 get_*_features 返回 BaseModelOutputWithPooling 而不是张量，
    真正的向量在 pooler_output（已过投影层，维度 = projection_dim）。"""
    if hasattr(out, "pooler_output"):
        return out.pooler_output
    if hasattr(out, "image_embeds"):
        return out.image_embeds
    return out


def encode_pending(batch: int | None = None, progress=None) -> dict:
    """把所有已生成缩略图但还没编码的文件编码入库。可重复调用（断点续跑）。"""
    import torch
    from PIL import Image

    model, proc, dev = load_model()
    st = store()
    bs = batch or CFG.clip_batch
    say = progress or (lambda m: log.info(m))

    con = connect()
    total = 0
    failed = 0
    try:
        while True:
            rows = con.execute(
                "SELECT id, dev, ino FROM files "
                "WHERE vec_row IS NULL AND thumb=1 AND status='ready' "
                "ORDER BY id LIMIT ?", (bs,)
            ).fetchall()
            if not rows:
                break
            imgs, keep = [], []
            for r in rows:
                p = CFG.thumb_dir / f"{r['dev']}_{r['ino']}.webp"
                if not p.exists():
                    failed += 1
                    con.execute("UPDATE files SET thumb=0 WHERE id=?", (r["id"],))
                    continue
                try:
                    im = Image.open(p).convert("RGB")
                    imgs.append(im)
                    keep.append(r["id"])
                except Exception:
                    failed += 1
                    con.execute("UPDATE files SET thumb=2 WHERE id=?", (r["id"],))
            if not imgs:
                con.commit()
                continue

            inputs = proc(images=imgs, return_tensors="pt")
            inputs = {k: v.to(dev) for k, v in inputs.items()}
            with torch.no_grad():
                out = _feat(model.get_image_features(**inputs))
            out = out / out.norm(dim=-1, keepdim=True)
            vecs = out.detach().float().cpu().numpy()

            start = st.append(vecs)
            con.executemany("UPDATE files SET vec_row=? WHERE id=?",
                            [(start + i, fid) for i, fid in enumerate(keep)])
            # 每批都同步 meta：这样服务端能实时看到「已就绪」，不必等全量跑完
            set_meta(con, "vec_count", str(st.count))
            set_meta(con, "vec_dim", str(_DIM))
            con.commit()

            total += len(keep)
            if total % (bs * 10) < bs:
                say(f"编码进度：{total} 张")
    finally:
        st.flush()
        con.execute("INSERT INTO meta(k,v) VALUES('vec_count',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(st.count),))
        con.execute("INSERT INTO meta(k,v) VALUES('vec_dim',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(_DIM),))
        con.commit()
        con.close()

    say(f"编码完成：新增 {total}，失败 {failed}，库内共 {st.count}")
    return {"encoded": total, "failed": failed, "total": st.count}


def _encode_image(im) -> np.ndarray:
    import torch
    model, proc, dev = load_model()
    inputs = proc(images=[im.convert("RGB")], return_tensors="pt")
    inputs = {k: v.to(dev) for k, v in inputs.items()}
    with torch.no_grad():
        out = _feat(model.get_image_features(**inputs))
    out = out / out.norm(dim=-1, keepdim=True)
    return out[0].detach().float().cpu().numpy()


def _encode_text(text: str) -> np.ndarray:
    import torch
    model, proc, dev = load_model()
    inputs = proc(text=[text], return_tensors="pt", padding=True, truncation=True)
    inputs = {k: v.to(dev) for k, v in inputs.items()}
    with torch.no_grad():
        out = _feat(model.get_text_features(**inputs))
    out = out / out.norm(dim=-1, keepdim=True)
    return out[0].detach().float().cpu().numpy()


# ---------------- 检索 ----------------
PHASH_SAME = 8            # 汉明距离 ≤ 8 视为同源（同一素材的不同版本/尺寸/裁切）
PHASH_REFRESH = 15        # 指纹索引后台刷新间隔（秒）

UPLOAD_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
               ".heic", ".heif", ".avif", ".psd", ".psb", ".ai", ".eps", ".pdf",
               ".pptx", ".ppt", ".dng", ".arw", ".cr2", ".cr3", ".nef", ".orf",
               ".raf", ".rw2"}


class PHashIndex:
    """全库感知指纹索引。

    为什么必须有它：CLIP 向量是后台分批编码的，全量跑完之前绝大多数素材还没有向量，
    这时上传一张库里早就存在的图会「搜不到」——这不是 bug，是覆盖度问题。
    指纹只要缩略图存在就能算（已覆盖 7 万+），全库比对实测 20ms，
    可以立刻把「同一张图」找出来，完全不依赖向量进度。
    """

    def __init__(self) -> None:
        self.ids = np.zeros(0, dtype=np.int64)
        self.hashes = np.zeros(0, dtype=np.int64)
        self.n = 0
        self.loaded_at = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def refresh(self) -> None:
        t0 = time.perf_counter()
        con = connect(readonly=True)
        try:
            rows = con.execute("SELECT id, phash FROM files WHERE phash IS NOT NULL").fetchall()
        finally:
            con.close()
        self.ids = np.fromiter((r["id"] for r in rows), dtype=np.int64, count=len(rows))
        self.hashes = np.fromiter((r["phash"] for r in rows), dtype=np.int64, count=len(rows))
        self.n = len(rows)
        self.loaded_at = time.time()
        log.debug("指纹索引 %d 条，%.0f ms", self.n, (time.perf_counter() - t0) * 1000)

    def ensure_loaded(self) -> None:
        """请求路径最多同步加载一次；之后由后台线程刷新，绝不在查询里扫库。"""
        if self.loaded_at == 0.0:
            with self._lock:
                if self.loaded_at == 0.0:
                    self.refresh()
        if self._thread is None:
            with self._lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._loop, name="phash-refresh", daemon=True)
                    self._thread.start()

    def _loop(self) -> None:
        while True:
            time.sleep(PHASH_REFRESH)
            try:
                self.refresh()
            except Exception as ex:  # noqa: BLE001
                log.debug("指纹索引刷新失败：%s", ex)

    def search(self, ph: int, limit: int = 200, max_dist: int = PHASH_SAME) -> list[tuple[int, int]]:
        if self.n == 0:
            return []
        q = np.int64(ph)
        if hasattr(np, "bitwise_count"):        # numpy >= 2.0
            dist = np.bitwise_count(np.bitwise_xor(self.hashes, q))
        else:                                    # 旧版本兜底
            raw = np.bitwise_xor(self.hashes, q).astype(np.uint64).view(np.uint8).reshape(-1, 8)
            dist = np.unpackbits(raw, axis=1).sum(axis=1)
        hit = np.nonzero(dist <= max_dist)[0]
        if hit.size == 0:
            return []
        order = hit[np.argsort(dist[hit], kind="stable")]
        return [(int(dist[i]), int(self.ids[i])) for i in order[:limit]]


_PHASH: PHashIndex | None = None
_PHASH_LOCK = threading.Lock()


def phash_index() -> PHashIndex:
    global _PHASH
    with _PHASH_LOCK:
        if _PHASH is None:
            _PHASH = PHashIndex()
        idx = _PHASH
    idx.ensure_loaded()
    return idx


_COV_CACHE: tuple[float, dict] | None = None
COV_TTL = 5.0


def coverage() -> dict:
    """索引覆盖度：前端要如实告诉用户「为什么这张图没搜到」。

    三个 count 在 27 万行上要几百毫秒，而 /api/stats 每 5 秒被轮询一次，
    所以这里做 5 秒缓存 —— 覆盖率不需要秒级实时。
    """
    global _COV_CACHE
    now = time.time()
    if _COV_CACHE and now - _COV_CACHE[0] < COV_TTL:
        return dict(_COV_CACHE[1])
    con = connect(readonly=True)
    try:
        g = lambda s: con.execute(s).fetchone()[0]
        out = {
            "searchable": g("SELECT count(*) FROM files WHERE kind IN ('raster','design','raw','doc')"),
            "fingerprinted": g("SELECT count(*) FROM files WHERE phash IS NOT NULL"),
            "vectorized": g("SELECT count(*) FROM files WHERE vec_row IS NOT NULL"),
        }
    finally:
        con.close()
    _COV_CACHE = (now, out)
    return dict(out)


def _sniff_ext(data: bytes) -> str:
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:4] == b"8BPS":
        return ".psd"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif"):
        return ".heic"
    return ".bin"


def load_query_image(data: bytes, filename: str = ""):
    """上传图解码。

    关键取舍：JPG/PNG/TIFF 等直接在内存解码，不落盘。
    实测这台机器上「写 3MB 临时文件再删除」光 unlink 就要 1.4 秒（Defender 实时扫描），
    而用户上传的九成是 JPG/PNG，完全没必要碰磁盘。

    只有 PSD/PSB/AI/PDF/PPTX/RAW 这类必须按路径解析的格式才落盘，
    且清理放到后台线程，不阻塞请求。临时目录在 D 盘，E 盘依然不写。
    """
    from PIL import Image

    ext = Path(filename or "").suffix.lower()
    if ext not in UPLOAD_EXTS:
        ext = _sniff_ext(data)

    im = _load_from_bytes(data, ext)
    if im is None:
        from .extract import extract

        tmpdir = CFG.tmp_dir
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmp = tmpdir / f"q_{uuid.uuid4().hex}{ext}"
        tmp.write_bytes(data)
        try:
            im = extract(str(tmp), ext, max_side=1024)
        finally:
            _reap(tmp)

    if im is None:
        raise ValueError(f"解析失败：不支持或已损坏的格式（{ext.lstrip('.') or '未知'}）")
    im = im.convert("RGB")
    if max(im.size) > 1024:
        im.thumbnail((1024, 1024), Image.LANCZOS)
    return im, (ext.lstrip(".") or "?").upper()


# 能在内存里直接解码的格式
_BYTES_OK = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
             ".heic", ".heif", ".avif"}


def _load_from_bytes(data: bytes, ext: str):
    from PIL import Image

    if ext in (".heic", ".heif", ".avif"):
        from .extract import _ensure_heif
        if not _ensure_heif():
            return None
    if ext not in _BYTES_OK:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        try:
            im.draft("RGB", (512, 512))   # JPEG 可降采样解码，快数倍
        except Exception:
            pass
        im.load()
        return im
    except Exception:
        return None


def _reap(path: Path) -> None:
    """后台删除临时文件：这台机器上删除 3MB 文件要 1.4s，绝不能放在请求路径里。"""
    def _rm():
        for _ in range(3):
            try:
                time.sleep(2)
                path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                continue
    threading.Thread(target=_rm, daemon=True).start()


def _fetch_rows(ids) -> dict:
    if not ids:
        return {}
    con = connect(readonly=True)
    try:
        out: dict = {}
        ids = [int(x) for x in ids]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in con.execute(
                    "SELECT f.id, f.path, f.name, f.ext, f.kind, f.size, f.mtime, f.top,"
                    " f.year, f.dev, f.ino, f.thumb, f.phash FROM files f WHERE f.id IN (" + ph + ")",
                    chunk):
                out[r["id"]] = r
        return out
    finally:
        con.close()


def _item(r, score=None, dist=None) -> dict:
    same = dist is not None and dist <= PHASH_SAME
    return {
        "id": r["id"],
        "name": r["name"] + (r["ext"] or ""),
        "path": r["path"],
        "ext": (r["ext"] or "").lstrip("."),
        "kind": r["kind"],
        "size": r["size"],
        "mtime": r["mtime"],
        "top": r["top"],
        "year": r["year"],
        "thumb": f"/thumb/{r['dev']}_{r['ino']}" if r["thumb"] == 1 else None,
        "score": None if score is None else round(float(score), 4),
        "dist": dist,
        "match": "fingerprint" if same else "vector",
        "same_source": same,
    }


def _assemble(qvec, limit: int, fp: list | None = None) -> dict:
    """两段式：指纹命中（同源，置顶） + CLIP 向量相似（补足）。

    指纹命中的 score 是 None —— 它不代表「相似度」，代表「同一张图」，
    前端应当用不同标签呈现，而不是和 CLIP 分数混在一起排序。
    """
    t0 = time.perf_counter()
    fp = fp or []
    rows = _fetch_rows([fid for _, fid in fp])
    fp_items = []
    for dist, fid in fp:
        r = rows.get(fid)
        if r:
            fp_items.append(_item(r, None, dist))
    t_fp = (time.perf_counter() - t0) * 1000

    seen = {x["id"] for x in fp_items}
    t1 = time.perf_counter()
    vec_items = []
    if qvec is not None:
        sims, ids = store().normalize_and_search(qvec, max(200, limit * 3))
        pairs = [(int(i), float(s)) for i, s in zip(ids.tolist(), sims.tolist()) if int(i) not in seen]
        rows2 = _fetch_rows([fid for fid, _ in pairs])
        for fid, s in pairs:
            r = rows2.get(fid)
            if r:
                vec_items.append(_item(r, s, None))
    t_vec = (time.perf_counter() - t1) * 1000

    fp_items.sort(key=lambda x: x["dist"])
    vec_items.sort(key=lambda x: -(x["score"] or 0))
    merged = (fp_items + vec_items)[:limit]
    return {"items": merged, "same_source": len(fp_items),
            "t_fp_ms": round(t_fp, 1), "t_vec_ms": round(t_vec, 1)}


def search_image(data: bytes, filename: str = "", limit: int = 60) -> dict:
    """上传图检索（非流式，保留给脚本/兼容用）。"""
    from .extract import phash
    t0 = time.perf_counter()
    im, fmt = load_query_image(data, filename)
    t1 = time.perf_counter()
    ph = phash(im)
    t2 = time.perf_counter()
    qv = _encode_image(im) if store().count else None
    t3 = time.perf_counter()
    fp = phash_index().search(ph, limit=max(80, limit))
    res = _assemble(qv, limit, fp)
    t4 = time.perf_counter()
    return {
        "mode": "image", "total": len(res["items"]), "items": res["items"],
        "same_source": res["same_source"],
        "query": {"w": im.width, "h": im.height, "format": fmt, "bytes": len(data)},
        "coverage": coverage(),
        "took_ms": round((t4 - t0) * 1000, 1),
        "breakdown_ms": {"decode": round((t1 - t0) * 1000, 1),
                         "fingerprint": round((t2 - t1) * 1000, 1),
                         "encode": round((t3 - t2) * 1000, 1),
                         "search": round((t4 - t3) * 1000, 1)},
    }


def search_by_image_bytes(data: bytes, limit: int = 60) -> dict:
    return search_image(data, "", limit)


def stream_image_search(data: bytes, filename: str, limit: int = 60):
    """NDJSON 流式检索：每个阶段一行，前端显示的是真实进度而不是假动画。"""
    from .extract import phash

    t0 = time.perf_counter()

    def ev(stage: str, pct: int, text: str, **kw) -> dict:
        return {"stage": stage, "pct": pct, "text": text,
                "ms": round((time.perf_counter() - t0) * 1000, 1), **kw}

    yield ev("decode", 6, "解码图片…")
    try:
        im, fmt = load_query_image(data, filename)
    except Exception as ex:  # noqa: BLE001
        yield {"stage": "error", "pct": 0, "text": str(ex)}
        return
    yield ev("decode_done", 16, f"{im.width} × {im.height} · {fmt}")

    yield ev("fingerprint", 26, "计算感知指纹…")
    ph = phash(im)
    idx = phash_index()
    fp = idx.search(ph, limit=max(80, limit))
    yield ev("fingerprint_done", 40, f"指纹比对 {idx.n:,} 张 · 同源 {len(fp)} 张")

    if store().count == 0:
        res = _assemble(None, limit, fp)
        yield {"stage": "done", "pct": 100, "text": "完成（仅指纹匹配）", "result": {
            "mode": "image", "total": len(res["items"]), "items": res["items"],
            "same_source": res["same_source"],
            "query": {"w": im.width, "h": im.height, "format": fmt, "bytes": len(data)},
            "coverage": coverage(), "took_ms": round((time.perf_counter() - t0) * 1000, 1)}}
        return

    yield ev("encode", 52, "CLIP 编码…")
    qv = _encode_image(im)
    yield ev("encode_done", 72, "编码完成")

    yield ev("search", 82, f"向量检索 {store().count:,} 条…")
    res = _assemble(qv, limit, fp)
    t4 = time.perf_counter()

    yield {"stage": "done", "pct": 100, "text": "完成", "result": {
        "mode": "image", "total": len(res["items"]), "items": res["items"],
        "same_source": res["same_source"],
        "query": {"w": im.width, "h": im.height, "format": fmt, "bytes": len(data)},
        "coverage": coverage(),
        "took_ms": round((t4 - t0) * 1000, 1),
        "breakdown_ms": {"fingerprint": res["t_fp_ms"], "search": res["t_vec_ms"]},
    }}


def search_by_file_id(fid: int, limit: int = 60) -> dict:
    """库内某文件「找同源/找相似」。

    未编码的文件（向量还没跑到）也能用：现场从缩略图编码一份向量，
    否则用户点「找相似」只会得到空结果，看起来像坏了。
    """
    t0 = time.perf_counter()
    con = connect(readonly=True)
    try:
        r = con.execute("SELECT vec_row, phash, thumb, dev, ino FROM files WHERE id=?", (fid,)).fetchone()
    finally:
        con.close()
    if not r:
        return {"mode": "image", "total": 0, "items": [], "took_ms": 0}

    qv = None
    if r["vec_row"] is not None:
        qv = np.asarray(store().mat[r["vec_row"]]).astype(np.float32)
    elif r["thumb"] == 1 and store().count:
        try:
            from PIL import Image
            p = CFG.thumb_dir / f"{r['dev']}_{r['ino']}.webp"
            qv = _encode_image(Image.open(p))
        except Exception as ex:  # noqa: BLE001
            log.debug("现场编码失败 id=%s: %s", fid, ex)

    fp = []
    if r["phash"] is not None:
        fp = [(d, i) for d, i in phash_index().search(r["phash"], limit=max(80, limit)) if i != fid]
    res = _assemble(qv, limit + 1, fp)
    items = [x for x in res["items"] if x["id"] != fid][:limit]
    return {"mode": "image", "total": len(items), "items": items,
            "same_source": res["same_source"], "coverage": coverage(),
            "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


def search_by_text(text: str, limit: int = 60) -> dict:
    t0 = time.perf_counter()
    q = _encode_text(text)
    t1 = time.perf_counter()
    res = _assemble(q, limit)
    return {"mode": "text", "total": len(res["items"]), "items": res["items"],
            "coverage": coverage(),
            "took_ms": round((time.perf_counter() - t0) * 1000, 1),
            "breakdown_ms": {"encode": round((t1 - t0) * 1000, 1),
                             "search": res["t_vec_ms"]}}


def warmup_async() -> None:
    def _w():
        try:
            load_model()
            store()
            phash_index()          # 预热指纹索引，避免首次图搜多等 500ms
        except Exception as ex:  # noqa: BLE001
            log.warning("模型预热失败：%s", ex)
    threading.Thread(target=_w, name="clip-warmup", daemon=True).start()


def encode_async() -> None:
    """启动时续跑未编码的文件；随缩略图产出持续跟进，队列追平后退出。"""
    def _w():
        try:
            encode_pending()
        except Exception as ex:  # noqa: BLE001
            log.warning("向量编码中断：%s", ex)
    threading.Thread(target=_w, name="clip-encode", daemon=True).start()


if __name__ == "__main__":
    from .config import setup_logging
    setup_logging()
    print(encode_pending())
