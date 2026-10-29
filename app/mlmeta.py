r"""外挂元数据回查（MLDNA 台账适配层）。

台账 SQLite（catalog/meta.sqlite）的 assets 表按 lib_path（相对 source_root 的
posix 风格相对路径）关联素材文件。配置 meta_db 后，/api/file/{id} 返回值里会
附带 mldna 字典：36 项业务字段 + 三个源站链接（详情页 / 原始文件 / 出现页面）。

台账是只读外挙：本模块只以 read-only URI 打开，绝不写台账文件；
连接按线程缓存（FastAPI 同步接口跑在线程池里，sqlite3 连接不能跨线程复用）。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

from .config import CFG

log = logging.getLogger("mlmeta")

_local = threading.local()

# 展示顺序与中文标签；不在表里的 attrs 键追加在「其他」段
FIELD_LABELS = [
    ("Asset Title",        "素材标题"),
    ("Description",        "描述"),
    ("Brand",              "品牌"),
    ("Nameplate",          "车系"),
    ("Model Code",         "车型代码"),
    ("Model Year",         "年款"),
    ("Campaign",           "Campaign"),
    ("Comms Tier",         "Comms Tier"),
    ("Market Specification", "市场规格"),
    ("Media Channel",      "媒体渠道"),
    ("Media Channel Usage", "渠道用途"),
    ("Region",             "区域"),
    ("Market",             "市场"),
    ("Country",            "国家"),
    ("Type",               "类型"),
    ("File Format",        "文件格式"),
    ("File Size",          "文件大小"),
    ("Resolution",         "分辨率"),
    ("Upload Date",        "上传日期"),
    ("Created By",         "创建者"),
    ("Rights Owner",       "版权方"),
    ("Rights / Licence Territory", "授权地域"),
    ("Licence Details",    "授权详情"),
    ("Start Date",         "授权开始"),
    ("Expiry Date",        "授权到期"),
    ("In Perpetuity",      "永久授权"),
    ("Embargo",            "禁发期"),
    ("Engine Type",        "发动机"),
    ("Vehicle Derivative", "衍生车型"),
    ("Body Style",         "车身形式"),
    ("Collections",        "合集"),
    ("Job Number",         "Job Number"),
    ("Originating Agency", "代理商"),
    ("Photographer / Videographer", "拍摄方"),
    ("Production Company E-mail", "制作公司邮箱"),
]

LABEL_SET = {k for k, _ in FIELD_LABELS}


def _con() -> sqlite3.Connection | None:
    """当前线程的台账只读连接；台账未配置或文件不存在时返回 None。"""
    if not CFG.meta_db or not Path(CFG.meta_db).exists():
        return None
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(f"file:{CFG.meta_db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        _local.con = con
    return con


def available() -> bool:
    con = _con()
    if con is None:
        return False
    try:
        con.execute("SELECT 1 FROM assets LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def _rel_posix(path: str) -> str:
    """把素材绝对路径转成台账 lib_path 的相对 posix 形式。"""
    root = CFG.source_root
    p = os.path.normpath(path)
    try:
        rel = os.path.relpath(p, root)
    except ValueError:
        return ""
    return rel.replace("\\", "/")


_COLS = ("asset_id", "brand", "brand_label", "campaign", "campaign_label",
         "asset_type", "title", "file_name", "description", "size_bytes",
         "resolution", "file_format", "upload_date", "detail_url", "origin_url",
         "source_pages", "attrs_json")


def by_path(path: str) -> dict | None:
    """按素材绝对路径回查台账记录；未配置/未命中返回 None。"""
    con = _con()
    if con is None:
        return None
    rel = _rel_posix(path)
    if not rel:
        return None
    try:
        r = con.execute(
            "SELECT " + ",".join(_COLS) + " FROM assets WHERE lib_path=? LIMIT 1", (rel,)
        ).fetchone()
    except sqlite3.Error as ex:
        log.warning("台账查询失败（%s）：%s", CFG.meta_db, ex)
        return None
    if not r:
        return None

    try:
        attrs = json.loads(r["attrs_json"]) if r["attrs_json"] else {}
    except (TypeError, ValueError):
        attrs = {}
    try:
        pages = json.loads(r["source_pages"]) if r["source_pages"] else []
    except (TypeError, ValueError):
        pages = []

    fields, extra = [], {}
    for k, label in FIELD_LABELS:
        v = attrs.get(k)
        if v not in (None, ""):
            fields.append([label, str(v)])
    for k, v in attrs.items():
        if k not in LABEL_SET and v not in (None, ""):
            extra[k] = str(v)

    return {
        "asset_id": r["asset_id"],
        "brand": r["brand_label"] or r["brand"],
        "campaign": r["campaign_label"] or r["campaign"],
        "asset_type": r["asset_type"],
        "title": r["title"] or attrs.get("Asset Title") or "",
        "description": r["description"] or attrs.get("Description") or "",
        "resolution": r["resolution"] or attrs.get("Resolution") or "",
        "upload_date": r["upload_date"] or attrs.get("Upload Date") or "",
        "fields": fields,           # 有序 [中文标签, 值] 列表
        "extra": extra,             # 未识别的 attrs 键值（少见）
        "detail_url": r["detail_url"] or "",
        "origin_url": r["origin_url"] or "",
        "source_pages": [p for p in pages if p],
    }
