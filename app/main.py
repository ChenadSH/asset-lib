"""FastAPI 应用入口。"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import CFG, setup_logging
from .db import connect, init_db, set_meta
from . import search as S
from .scanner import scan as do_scan
from .worker import ThumbWorker

setup_logging()
log = logging.getLogger("main")

BASE = Path(__file__).resolve().parent
app = FastAPI(title="素材库检索", docs_url="/api/docs")

_worker: ThumbWorker | None = None
_scan_lock = threading.Lock()
_scan_state: dict = {"running": False, "started_at": None, "last": None, "last_at": None, "last_error": None}

KEY_RE = re.compile(r"^\d+_\d+$")
MAX_UPLOAD = 300 * 1024 * 1024
PLACEHOLDER = (b"RIFF\x24\x00\x00\x00WEBPVP8L\x18\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00")


def _purge_tmp() -> None:
    """清掉上传解析留下的临时文件（正常情况下已在后台删掉，这里兜底）。"""
    d = CFG.tmp_dir
    if not d.is_dir():
        return
    cutoff = time.time() - 3600
    for f in d.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


@app.on_event("startup")
def _startup() -> None:
    init_db()
    log.info("数据库就绪：%s", CFG.db_path)
    # 扩大同步接口线程池（默认 40）：扫描/worker 忙时接口会等 SQLite 锁，池满会让前端请求无限排队
    try:
        from anyio import to_thread
        to_thread.current_default_thread_limiter().total_tokens = 64
    except Exception:  # noqa: BLE001
        pass

    # 先用只读连接确认索引可用，页面立刻能打开；扫描与 worker 在后台按顺序启动
    def boot() -> None:
        global _worker
        _purge_tmp()
        # 模型预热只读文件系统、不写库，可以和扫描并行；否则首个搜索请求要等模型加载十多秒
        if CFG.encode_on_start:
            try:
                from . import embed
                embed.warmup_async()
            except Exception as ex:  # noqa: BLE001
                log.warning("模型预热未能启动：%s", ex)
        if CFG.rescan_on_start:
            _background_scan(min_gap_s=600)  # 距上次成功扫描 <10 分钟则跳过
        _worker = ThumbWorker()
        _worker.start()
        if CFG.encode_on_start:
            try:
                from . import embed
                embed.encode_async()
            except Exception as ex:  # noqa: BLE001
                log.warning("向量编码未能启动：%s", ex)

    threading.Thread(target=boot, name="boot", daemon=True).start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if _worker:
        _worker.stop()


# ---------------- 扫描 ----------------
def _background_scan(min_gap_s: float = 0.0) -> None:
    if not _scan_lock.acquire(blocking=False):
        log.info("已有扫描在进行，跳过")
        return
    # 启动防抖：距上次成功扫描不足 min_gap_s 则跳过，避免每次重启都全量扫 3 分多钟
    if min_gap_s > 0:
        try:
            con = connect(timeout=8)
            try:
                row = con.execute("SELECT v FROM meta WHERE k='last_scan'").fetchone()
            finally:
                con.close()
            last_ts = float(row[0]) if row else 0
            if last_ts and time.time() - last_ts < min_gap_s:
                log.info("距上次成功扫描不足 %d 分钟，跳过启动扫描", min_gap_s / 60)
                _scan_lock.release()
                return
        except Exception:  # noqa: BLE001
            pass
    _scan_state["running"] = True
    _scan_state["started_at"] = time.time()
    try:
        r = do_scan(CFG)
        _scan_state["last"] = r
        _scan_state["last_at"] = time.time()
        _scan_state["last_error"] = None if r.get("ok") else r.get("message")
        log.info("扫描结果：%s", r)
    except Exception as ex:  # noqa: BLE001
        _scan_state["last_error"] = str(ex)
        log.exception("扫描异常")
    finally:
        _scan_state["running"] = False
        _scan_lock.release()


def _last_scan_stats() -> dict | None:
    """最近一次扫描的结果（内存优先，重启后回退 meta 表持久化值）。"""
    if _scan_state["last"]:
        return _scan_state["last"]
    try:
        con = connect(timeout=8)
        try:
            row = con.execute("SELECT v FROM meta WHERE k='last_scan_stats'").fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None


@app.post("/api/rescan")
def api_rescan():
    if _scan_lock.locked():
        return {"ok": False, "message": "扫描已在进行中"}
    threading.Thread(target=_background_scan, name="manual-scan", daemon=True).start()
    return {"ok": True, "message": "已开始后台扫描"}


@app.get("/api/rescan/estimate")
def api_rescan_estimate():
    """给前端确认弹窗的预估信息：上次真实用时 / 兜底估算。"""
    last = _last_scan_stats()
    out: dict = {"running": _scan_lock.locked()}
    if last and last.get("ok"):
        out["last_total_s"] = last.get("total_s")
        out["last_walk_s"] = last.get("walk_s")
        out["last_total"] = last.get("total")
        out["last_new"] = last.get("new")
        out["last_changed"] = last.get("changed")
        out["last_missing"] = last.get("missing")
        out["last_at"] = _scan_state["last_at"]
    try:
        con = connect(timeout=8)
        try:
            out["files"] = (con.execute("SELECT count(*) FROM files").fetchone() or [0])[0]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        out["files"] = 0
    return out


# ---------------- 检索 ----------------
@app.get("/api/search")
def api_search(
    q: str = "",
    kind: str = "",
    top: str = "",
    year: str = "",
    offset: int = 0,
    limit: int = Query(60, ge=1, le=200),
):
    kinds = [k for k in kind.split(",") if k]
    tops = [t for t in top.split("||") if t] if top else []
    years = [y for y in year.split(",") if y]
    return S.name_search(q, kinds, tops, years, offset, limit)


@app.get("/api/file/{fid}")
def api_file(fid: int):
    d = S.get_file(fid)
    if not d:
        raise HTTPException(404, "not found")
    return d


def _reveal_in_file_manager(path: str) -> None:
    """在资源管理器 / 访达 / 文件管理器里定位文件（跨平台）。"""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])


@app.post("/api/reveal/{fid}")
def api_reveal(fid: int):
    d = S.get_file(fid)
    if not d:
        raise HTTPException(404, "not found")
    p = d["path"]
    if not os.path.exists(p):
        return JSONResponse({"ok": False, "message": "文件已不存在（可能被移动或删除）"}, status_code=409)
    try:
        _reveal_in_file_manager(p)
    except Exception as ex:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"无法打开文件管理器：{ex}"}, status_code=500)
    return {"ok": True}


@app.get("/api/facets")
def api_facets():
    return S.facets()


@app.get("/api/stats")
def api_stats():
    st = S.stats()
    st["worker"] = {
        "done": _worker.done if _worker else 0,
        "failed": _worker.failed if _worker else 0,
    }
    st["scan"] = {
        "running": _scan_state["running"],
        "started_at": _scan_state["started_at"] if _scan_state["running"] else None,
        "error": _scan_state["last_error"],
        "last": _last_scan_stats(),
        "last_at": _scan_state["last_at"],
    }
    st["source_online"] = os.path.isdir(CFG.source_root)
    try:
        from . import embed
        st["coverage"] = embed.coverage()
    except Exception:  # noqa: BLE001
        st["coverage"] = {}
    return st


# ---------------- 缩略图 ----------------
@app.get("/thumb/{key}")
def api_thumb(key: str):
    if not KEY_RE.match(key):
        raise HTTPException(400, "bad key")
    p = CFG.thumb_dir / f"{key}.webp"
    if p.exists():
        return FileResponse(str(p), media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=604800"})
    # 未生成：返回 204，前端显示占位
    return Response(status_code=204)


# ---------------- 图片搜索 ----------------
@app.post("/api/similar/stream")
async def api_similar_stream(file: UploadFile = File(...), limit: int = 60):
    """流式返回 NDJSON：每行一个阶段事件，前端据此显示真实进度。

    不用普通 POST 的原因：首次查询要加载模型（可能十几秒）、上传 PSD 要抽缩略图、
    冷缓存读盘也慢，这些阶段必须让用户看见，否则就是一个卡住的白屏。
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"文件过大（{len(data) / 1024**2:.0f} MB），单次上限 300 MB")
    from . import embed

    def gen():
        try:
            for ev in embed.stream_image_search(data, file.filename or "", limit):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as ex:  # noqa: BLE001
            log.exception("图搜失败")
            yield json.dumps({"stage": "error", "pct": 0, "text": f"{type(ex).__name__}: {ex}"},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/similar")
async def api_similar(file: UploadFile = File(...), limit: int = 60):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, f"文件过大（{len(data) / 1024**2:.0f} MB），单次上限 300 MB")
    from . import embed
    try:
        return embed.search_image(data, file.filename or "", limit)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.get("/api/similar/{fid}")
def api_similar_by_id(fid: int, limit: int = 60):
    from . import embed
    return embed.search_by_file_id(fid, limit)


@app.get("/api/text-search")
def api_text_search(q: str, limit: int = 60):
    from . import embed
    if not embed.is_ready():
        raise HTTPException(503, "向量索引尚未构建完成")
    return embed.search_by_text(q, limit)


# ---------------- 静态页 ----------------
@app.get("/")
def index():
    return FileResponse(str(BASE / "static" / "index.html"))


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
