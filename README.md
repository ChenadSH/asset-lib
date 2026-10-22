# 素材库检索 · Asset Library

给本地大容量设计素材盘做的**离线图片检索网页**：按名称搜、按画面搜（中文描述）、以图搜图。
只读挂载素材盘，所有派生数据（索引 / 缩略图 / 向量 / 日志）写在独立的可配置目录里。

- 素材目录**可换任意盘、任意文件夹**（`E:\` / `/Volumes/Design` / `~/Pictures` 都可以）
- 推理设备**自动适配** CUDA / Apple Silicon MPS / CPU
- 全程本地运行，不联网、不上传素材

```
┌──────────────┐   os.walk / stat / read   ┌──────────────────────────┐
│ 素材盘（只读）│ ────────────────────────► │ 扫描器 scanner            │
└──────────────┘                           │  · (dev, ino) 文件身份    │
                                           │  · 增量对账 / 路径领养    │
                                           └────────────┬─────────────┘
                                                        ▼
                                    ┌───────────────────────────────────┐
                                    │ index.db (SQLite + FTS5)          │
                                    │ 清单 / 队列 / 元数据 / 名称全文索引│
                                    └───────┬─────────────────┬─────────┘
                                            ▼                 ▼
                            ┌───────────────────┐   ┌──────────────────────┐
                            │ 缩略图 worker      │   │ 向量编码 embed        │
                            │ → thumbs/*.webp    │   │ → vectors.npy (512d) │
                            │ + pHash 指纹       │   │ CUDA / MPS / CPU     │
                            └─────────┬─────────┘   └──────────┬───────────┘
                                      └────────┬───────────────┘
                                               ▼
                                   ┌───────────────────────┐
                                   │ FastAPI + 静态前端     │
                                   │ 名称 / 画面 / 以图搜图 │
                                   └───────────────────────┘
```

## 快速开始

### Windows

```bat
git clone <repo-url> asset-lib
cd asset-lib
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

copy config.example.yaml config.yaml
:: 编辑 config.yaml，把 source_root 改成你的素材目录，例如 E:\ 或 E:\Design Assets

start.bat
```

### macOS / Linux

```bash
git clone <repo-url> asset-lib && cd asset-lib
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# 编辑 config.yaml，把 source_root 改成 /Volumes/Design 之类

chmod +x start.sh && ./start.sh
```

打开 <http://127.0.0.1:8765>。首次启动会自动扫描 → 生成缩略图 → 后台编码向量（进度显示在右上角）。

不想改配置文件也可以用环境变量：

```bash
# macOS
ASSET_LIB_SOURCE=/Volumes/Design ASSET_LIB_DATA=~/asset-lib-data ./start.sh
# Windows PowerShell
$env:ASSET_LIB_SOURCE="E:\Design"; $env:ASSET_LIB_DATA="D:\asset-lib-data"; python -m app.cli serve
```

### 先自检再启动

换机器、换盘、换目录之后，先跑一次自检，它会把路径、依赖、设备、模型逐项列出来：

```bash
python -m app.cli doctor
```

## 配置

| 字段 | 说明 | 默认 |
|---|---|---|
| `source_root` | **必填**，素材根目录，只读 | 空 |
| `data_root` | 索引/缩略图/向量/日志所在目录 | `<repo>/data` |
| `model_dir` | 模型目录，本地权重放 `<model_dir>/chinese-clip/` | `<data_root>/models` |
| `clip_device` | `auto` / `cuda` / `mps` / `cpu`，不可用时自动降级 | `auto` |
| `clip_batch` | 编码批大小，显存不足调到 16~32 | `64` |
| `thumb_workers` | 缩略图并发数，机械盘建议 4 | `8` |
| `host` / `port` | 服务监听地址 | `127.0.0.1:8765` |
| `rescan_on_start` | 启动时增量对账（10 分钟内已扫过会跳过） | `true` |

对应的环境变量：`ASSET_LIB_SOURCE`、`ASSET_LIB_DATA`、`ASSET_LIB_MODEL_DIR`、`ASSET_LIB_DEVICE`、`ASSET_LIB_HOST`、`ASSET_LIB_PORT`、`ASSET_LIB_CONFIG`。

## 命令行

```bash
python -m app.cli serve     # 启动 Web 服务
python -m app.cli doctor    # 环境自检（路径/依赖/设备/模型）
python -m app.cli scan      # 全量扫描 + 对账
python -m app.cli encode    # 生成 CLIP 向量（可重复执行，断点续跑）
python -m app.cli worker    # 只跑缩略图 worker
python -m app.cli stats     # 索引统计
python -m app.cli init      # 只建表
```

## 实测数据（参考）

在 271,754 个文件 / 12.7 TB 的素材盘、Quadro P4000 上：

| 操作 | 耗时 |
|---|---|
| 全量扫描 + 对账（热缓存） | ~205 s |
| 名称搜索（3 字符以上，FTS5） | 60~90 ms |
| 名称搜索（中文两字，LIKE 路径） | 30~80 ms |
| 以图搜图（指纹 + 512 维向量） | 200~800 ms |
| 向量编码 | ~279 张/s（GPU） |

冷启动首次扫描要建立文件清单，机械盘会明显更慢；之后都是增量。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构、数据模型、检索流程、关键设计决策 |
| [docs/MIGRATION.md](docs/MIGRATION.md) | 换盘 / 换目录 / 换机器，索引如何复用 |
| [docs/PLATFORMS.md](docs/PLATFORMS.md) | Windows / macOS / Linux 差异与调优 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 日常运维、覆盖率、故障排查 |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | 代码结构、HTTP API、调试技巧 |

## 已知限制

- **PSB** 只能取到 160×90 内嵌缩略图（格式限制），可被搜到但视觉模糊。
- **大型 TIFF** 解码慢，排在队列最后；全量首次处理需要数小时。
- **RAW** 部分机型无内嵌预览，需半尺寸解码（约 0.4 s/张）。
- 命中数上限：短词 1000、全文 2 万，界面显示为 `1000+` / `20000+`。
- 首次安装需要联网下载模型（约 750 MB），之后可完全离线。
- 迁移到新盘时，若目录结构发生变化（文件被重新组织），只能按相对路径复用一部分缓存。

## 许可

代码以 MIT 许可发布，见 [LICENSE](LICENSE)。
模型 [OFA-Sys/chinese-clip-vit-base-patch16](https://huggingface.co/OFA-Sys/chinese-clip-vit-base-patch16) 为 MIT 许可，可商用。

本项目按"现状"提供，不构成任何形式的担保。用于商业素材库前请自行确认模型与依赖的许可。
