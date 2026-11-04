r"""配置加载与全局常量。

优先级：环境变量 > config.yaml > 内置默认值

常用环境变量（便于在不同机器/容器里迁移，无需改文件）：

    ASSET_LIB_CONFIG     配置文件路径（默认 <repo>/config.yaml）
    ASSET_LIB_SOURCE     素材根目录，只读（如 E:\\ 或 /Volumes/Assets 或 ~/Pictures）
    ASSET_LIB_DATA       数据目录，存索引/缩略图/向量/日志（默认 <repo>/data）
    ASSET_LIB_MODEL_DIR  模型目录（默认 <data>/models）
    ASSET_LIB_DEVICE     cuda / mps / cpu / auto（默认 auto）
    ASSET_LIB_HOST / ASSET_LIB_PORT

典型用法：

    # Windows：素材在 E 盘，数据放 D 盘
    set ASSET_LIB_SOURCE=E:\
    set ASSET_LIB_DATA=D:\asset-lib\data

    # macOS：素材在外置盘
    export ASSET_LIB_SOURCE=/Volumes/Design
    export ASSET_LIB_DATA=~/asset-lib-data
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 环境变量 -> 配置字段
ENV_MAP = {
    "ASSET_LIB_SOURCE": "source_root",
    "ASSET_LIB_DATA": "data_root",
    "ASSET_LIB_MODEL_DIR": "model_dir",
    "ASSET_LIB_DEVICE": "clip_device",
    "ASSET_LIB_HOST": "host",
    "ASSET_LIB_PORT": "port",
}

# 需要按数值解析的字段
INT_FIELDS = {"port", "thumb_max_side", "thumb_quality", "thumb_workers",
              "clip_batch", "rescan_interval_min"}


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 没有 pyyaml / 文件损坏时退回默认值，保证程序仍能启动
        return {}


def _abspath(p: str) -> str:
    """展开 ~ 与环境变量，并把相对路径锚定到仓库根目录。"""
    if not p:
        return ""
    s = os.path.expanduser(os.path.expandvars(str(p)))
    if not os.path.isabs(s):
        s = str(BASE_DIR / s)
    return os.path.normpath(s)


def resolve_device(pref: str = "auto") -> str:
    """把 cuda/mps/cpu/auto 解析成当前机器真正可用的设备。

    - auto：CUDA > MPS（Apple Silicon）> CPU
    - 显式指定但不可用时自动降级，不让程序起不来
    """
    want = (pref or "auto").strip().lower()
    try:
        import torch  # 延迟导入：仅 CLI 统计等场景不需要 torch
    except Exception:
        return "cpu"

    cuda_ok = bool(torch.cuda.is_available())
    mps_backend = getattr(torch.backends, "mps", None)
    mps_ok = bool(mps_backend is not None and mps_backend.is_available())

    if want == "cuda":
        return "cuda" if cuda_ok else ("mps" if mps_ok else "cpu")
    if want == "mps":
        return "mps" if mps_ok else ("cuda" if cuda_ok else "cpu")
    if want == "cpu":
        return "cpu"
    return "cuda" if cuda_ok else ("mps" if mps_ok else "cpu")


@dataclass
class Config:
    # 素材目录（只读）。留空则必须通过 config.yaml / 环境变量提供
    source_root: str = ""
    # 派生数据目录。留空 -> <repo>/data
    data_root: str = ""
    # 模型目录。留空 -> <data_root>/models
    model_dir: str = ""
    # 界面标题（浏览器标签 + 左上角品牌名），多实例共用代码时按配置区分
    site_title: str = "素材库"
    # 外挂元数据库（可选）：SQLite，assets 表按 lib_path 相对路径关联素材文件。
    # 配置后 /api/file/{id} 会附带 mldna 字段与源站链接（如 MLDNA 台账）
    meta_db: str = ""

    skip_dirs: list[str] = field(default_factory=lambda: [
        "$recycle.bin", "system volume information", ".trashes", ".spotlight-v100", ".fseventsd",
    ])
    thumb_max_side: int = 448
    thumb_quality: int = 82
    thumb_workers: int = 8
    # 视频抽帧策略：每个视频按「时长 × per_minute」抽帧，上下限截断。
    # 例：per_minute=5, min=5, max=50 → 1 分钟 5 帧、5 分钟 25 帧、10 分钟及以上 50 帧
    video_frames: dict = field(default_factory=lambda: {"per_minute": 5, "min": 5, "max": 50})
    # ffmpeg 可执行文件路径；留空自动从 PATH 与 ffmpeg 同目录探测
    ffmpeg_path: str = ""
    clip_model: str = "OFA-Sys/chinese-clip-vit-base-patch16"
    clip_batch: int = 64
    # auto / cuda / mps / cpu；auto 会自动挑选当前机器可用的最快设备
    clip_device: str = "auto"
    host: str = "127.0.0.1"
    port: int = 8765
    rescan_on_start: bool = True
    rescan_interval_min: int = 120
    encode_on_start: bool = True

    def __post_init__(self) -> None:
        self.source_root = _abspath(self.source_root)
        self.data_root = _abspath(self.data_root) or str(BASE_DIR / "data")
        self.model_dir = _abspath(self.model_dir) or str(Path(self.data_root) / "models")
        self.meta_db = _abspath(self.meta_db)
        # 视频抽帧策略归一化：非法值回退默认，保证 workers 拿到的一定是三个 int
        vf = self.video_frames if isinstance(self.video_frames, dict) else {}
        try:
            per_minute = max(1, int(vf.get("per_minute", 5)))
            vmin = max(1, int(vf.get("min", 5)))
            vmax = max(vmin, int(vf.get("max", 50)))
        except (TypeError, ValueError):
            per_minute, vmin, vmax = 5, 5, 50
        self.video_frames = {"per_minute": per_minute, "min": vmin, "max": vmax}

    # ---------- 派生路径 ----------
    @property
    def db_path(self) -> Path:
        return Path(self.data_root) / "index.db"

    @property
    def thumb_dir(self) -> Path:
        return Path(self.data_root) / "thumbs"

    @property
    def vec_path(self) -> Path:
        return Path(self.data_root) / "vectors.npy"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_root) / "logs"

    @property
    def tmp_dir(self) -> Path:
        return Path(self.data_root) / "tmp"

    @property
    def clip_dir(self) -> Path:
        return Path(self.model_dir) / "chinese-clip"

    @property
    def device(self) -> str:
        """解析后的实际设备（cuda / mps / cpu）。"""
        return resolve_device(self.clip_device)

    def ensure_dirs(self) -> None:
        for d in (self.data_root, self.thumb_dir, self.log_dir, self.model_dir, self.tmp_dir):
            Path(d).mkdir(parents=True, exist_ok=True)

    def source_ready(self) -> tuple[bool, str]:
        """素材目录是否可用，返回 (ok, 说明)。"""
        if not self.source_root:
            return False, "未配置素材目录：请设置环境变量 ASSET_LIB_SOURCE 或在 config.yaml 里填 source_root"
        p = Path(self.source_root)
        if not p.exists():
            return False, f"素材目录不存在或未挂载：{self.source_root}"
        if not p.is_dir():
            return False, f"source_root 不是目录：{self.source_root}"
        return True, ""


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_file = Path(path) if path else Path(os.environ.get(
        "ASSET_LIB_CONFIG", str(BASE_DIR / "config.yaml")))
    raw = _load_yaml(cfg_file) if cfg_file.exists() else {}

    known = set(Config.__dataclass_fields__)  # type: ignore[attr-defined]
    data = {k: v for k, v in raw.items() if k in known and v is not None}

    # 环境变量覆盖
    for env, fname in ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            data[fname] = int(val) if fname in INT_FIELDS else val

    cfg = Config(**data)
    cfg.ensure_dirs()
    return cfg


CFG = load_config()


# ---------- 文件类型分类 ----------
RASTER = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
RAW = {".dng", ".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2"}
DESIGN = {".psd", ".psb", ".ai", ".eps", ".indd", ".idml", ".sketch", ".fig", ".xd", ".afdesign"}
DOC = {".pdf", ".pptx", ".ppt", ".key"}
VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mxf", ".r3d"}
AUDIO = {".wav", ".mp3", ".aif", ".aiff", ".m4a"}


def classify(ext: str) -> str:
    e = ext.lower()
    if e in RASTER:
        return "raster"
    if e in RAW:
        return "raw"
    if e in DESIGN:
        return "design"
    if e in DOC:
        return "doc"
    if e in VIDEO:
        return "video"
    if e in AUDIO:
        return "audio"
    return "other"


# ---------------- 素材类型（用户视角，比 kind 更细） ----------------
# kind 是技术分类（raster 把 jpg 和 tif 混在一起，doc 把 ppt 和 pdf 混在一起），
# 筛选时用户想找的是「PPT」「TIFF 大图」这类颗粒度，故单独一列 mtype。
MTYPE_MAP: dict[str, set[str]] = {
    "image":  {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif", ".avif"},
    "tiff":   {".tif", ".tiff"},
    "video":  {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mxf", ".r3d"},
    "ppt":    {".ppt", ".pptx", ".pptm", ".key"},
    "doc":    {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".md"},
    "design": {".psd", ".psb", ".ai", ".eps", ".indd", ".idml", ".sketch", ".fig", ".xd", ".afdesign"},
    "raw":    {".dng", ".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2"},
    "audio":  {".wav", ".mp3", ".aif", ".aiff", ".m4a"},
}
_MTYPE_BY_EXT: dict[str, str] = {}
for _mt, _exts in MTYPE_MAP.items():
    for _e in _exts:
        _MTYPE_BY_EXT[_e] = _mt


def mtype_of(ext: str | None) -> str:
    return _MTYPE_BY_EXT.get((ext or "").lower(), "other")


# 可进入图像索引的类型
INDEXABLE = {"raster", "raw", "design", "doc"}

# 抽图优先级（数值小的先跑）
PRIORITY = {"raster": 0, "design": 1, "doc": 2, "raw": 3, "other": 9}


def setup_logging(cfg: Config | None = None) -> None:
    import logging
    from logging.handlers import TimedRotatingFileHandler

    c = cfg or CFG
    c.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)

    # 同一台机器可能跑多个实例（主服务 / 内嵌 worker 的其他服务 / CLI 工具），
    # app.log 被其他实例握住时绝不能让进程起不来：退回带实例名的日志文件
    try:
        fh = TimedRotatingFileHandler(c.log_dir / "app.log", when="midnight",
                                      backupCount=30, encoding="utf-8")
    except PermissionError:
        alt = c.log_dir / f"app.{os.getpid()}.log"
        logging.getLogger(__name__).warning(
            "app.log 被其他实例占用，日志改写到 %s", alt.name)
        fh = TimedRotatingFileHandler(alt, when="midnight", backupCount=7, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
