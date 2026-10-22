"""命令行入口。

    python -m app.cli serve     # 启动 Web 服务（等价于 uvicorn app.main:app）
    python -m app.cli init      # 只建表
    python -m app.cli scan      # 全量扫描 + 对账（支持换盘/换目录后的路径领养）
    python -m app.cli stats     # 查看统计
    python -m app.cli encode    # 生成 CLIP 向量（可重复执行，断点续跑）
    python -m app.cli worker    # 只跑缩略图 worker
    python -m app.cli doctor    # 环境自检：路径、依赖、设备、模型
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

from .config import CFG, setup_logging
from .db import init_db
from . import search as S


def _doctor() -> int:
    """打印一次可迁移性自检报告，迁移到新机器/新盘时先跑这个。"""
    from .config import resolve_device

    ok = True
    print("=" * 62)
    print("素材库检索 · 环境自检")
    print("=" * 62)
    print(f"平台      : {platform.system()} {platform.release()} / Python {platform.python_version()}")
    print(f"配置来源  : {os.environ.get('ASSET_LIB_CONFIG', str(Path(__file__).resolve().parent.parent / 'config.yaml'))}")
    print(f"素材目录  : {CFG.source_root or '(未配置)'}")
    print(f"数据目录  : {CFG.data_root}")
    print(f"模型目录  : {CFG.model_dir}")

    ready, why = CFG.source_ready()
    if ready:
        try:
            usage = shutil.disk_usage(CFG.source_root)
            print(f"  └ 可访问，剩余 {usage.free / 1e9:.0f} GB")
        except OSError:
            print("  └ 可访问")
    else:
        ok = False
        print(f"  └ ✗ {why}")

    # 数据目录可写
    try:
        probe = Path(CFG.data_root) / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = shutil.disk_usage(CFG.data_root)
        print(f"数据目录可写 ✓ 剩余 {usage.free / 1e9:.0f} GB")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"数据目录不可写 ✗ {ex}")

    # 依赖
    print("-" * 62)
    deps = ["torch", "transformers", "numpy", "PIL", "fastapi", "uvicorn",
            "yaml", "psd_tools", "rawpy", "pillow_heif", "fitz"]
    for d in deps:
        try:
            __import__(d)
            print(f"  {d:<14} ✓")
        except Exception:
            mark = "—" if d in {"psd_tools", "rawpy", "pillow_heif", "fitz"} else "✗"
            print(f"  {d:<14} {mark} 未安装" + ("（可选，缺省时对应格式降级）" if mark == "—" else ""))
            if mark == "✗":
                ok = False

    print("-" * 62)
    print(f"推理设备  : 配置 {CFG.clip_device} → 实际 {resolve_device(CFG.clip_device)}")
    try:
        import torch
        print(f"  torch {torch.__version__} | cuda={torch.cuda.is_available()} "
              f"| mps={bool(getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available())}")
    except Exception:
        pass

    clip = CFG.clip_dir
    has_local = (clip / "model.safetensors").exists()
    print(f"本地模型  : {clip}" + (" ✓" if has_local else " （未找到，首次运行会联网下载）"))

    db = CFG.db_path
    if db.exists():
        print(f"索引库    : {db} ({db.stat().st_size / 1e6:.0f} MB)")
    else:
        print(f"索引库    : {db} （尚未创建，首次扫描时生成）")

    print("=" * 62)
    print("自检结果：" + ("通过 ✓" if ok else "存在问题 ✗，请按上面提示修正"))
    return 0 if ok else 1


def main() -> None:
    setup_logging()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if cmd in {"serve", "web", "run"}:
        import uvicorn
        uvicorn.run("app.main:app", host=CFG.host, port=CFG.port)
        return

    if cmd == "doctor":
        raise SystemExit(_doctor())

    if cmd == "init":
        init_db()
        print("数据库已初始化：", CFG.db_path)
        return

    if cmd == "scan":
        from .scanner import scan
        ready, why = CFG.source_ready()
        if not ready:
            print("✗", why)
            raise SystemExit(2)
        init_db()
        r = scan(CFG, progress=lambda m: print(m, flush=True))
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    if cmd == "stats":
        print(json.dumps(S.stats(), ensure_ascii=False, indent=2))
        return

    if cmd == "encode":
        from .embed import encode_pending
        print(json.dumps(encode_pending(progress=lambda m: print(m, flush=True)),
                         ensure_ascii=False, indent=2))
        return

    if cmd == "worker":
        import time
        from .worker import ThumbWorker
        w = ThumbWorker()
        w.start()
        print("缩略图 worker 运行中，Ctrl+C 退出")
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            w.stop()
        return

    print("未知命令：", cmd)
    print("可用：serve | doctor | init | scan | stats | encode | worker")


if __name__ == "__main__":
    main()
