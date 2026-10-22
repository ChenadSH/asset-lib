# 架构与设计决策

## 1. 总体结构

单机、无外部服务、无网络依赖。四个进程内角色：

| 角色 | 代码 | 职责 |
|---|---|---|
| 扫描器 | `app/scanner.py` | 遍历素材盘，与索引库做 diff，维护文件清单与任务队列 |
| 缩略图 worker | `app/worker.py` + `app/extract.py` | 解码各格式、生成 WebP 缩略图与 pHash 指纹 |
| 向量编码 | `app/embed.py` | Chinese-CLIP 编码、`vectors.npy` 存储、相似检索 |
| Web 服务 | `app/main.py` + `app/static/` | FastAPI 接口与单页前端 |

它们共享一个 SQLite 库（WAL 模式）和一个向量矩阵文件。**写者只有一个**：扫描与 worker 串行启动，不并发写库。

## 2. 磁盘布局

```
<repo>/                     代码（入库）
├─ app/                     服务代码
├─ docs/                    文档
├─ tools/                   探测与转换脚本
├─ config.yaml              本机配置（不入库）
└─ config.example.yaml      配置样例

<data_root>/                派生数据（不入库，可独立备份/迁移）
├─ index.db                 SQLite：清单 + FTS5 + 任务队列
├─ vectors.npy              向量矩阵（float32，memmap 预分配）
├─ thumbs/<dev>_<ino>.webp  缩略图
├─ models/chinese-clip/     本地模型权重
├─ logs/app.log             按天轮转
└─ tmp/                     上传图的临时落地目录（仅 PSD/RAW 需要）
```

素材盘**只读**：`os.walk` + `os.stat` + `open(...,'rb')`，不写、不锁、不改 mtime。

## 3. 数据模型

```sql
files(id, dev, ino, path, name, ext, kind, size, mtime, top, year,
      status,        -- new / ready / missing
      thumb,         -- 0 未处理 / 1 成功 / 2 失败
      vec_row,       -- 在 vectors.npy 中的行号
      phash,         -- 64 位感知哈希（有符号 int64 存储）
      seen_at)

queue(id, file_id, job, attempts, err, created)   -- job='thumb'
meta(k, v)                                        -- vol_dev / source_root / vec_count / last_scan ...
files_fts(name, path)                             -- FTS5 trigram 外部内容表
```

索引：`idx_status_mtime(status, mtime DESC)` 是所有列表查询的关键（见 5.3）。

## 4. 三条检索链路

### 4.1 名称检索（`app/search.py::name_search`）

```
关键词 ≤2 字符 → LIKE '%kw%' + ORDER BY mtime DESC（走 idx_status_mtime，34ms/27万行）
关键词 ≥3 字符 → FTS5 trigram
                 ├ 命中 < 2000 → ORDER BY rank（相关度优先）
                 └ 命中 ≥ 2000 → FTS 子查询 + JOIN 主表按 mtime 排序（避免全量排序）
```

**为什么分两条路**：FTS5 的 trigram 分词要求至少 3 个字符，中文「卫士」「路虎」是 2 个字，
走 FTS 会 0 命中。短词必须回退 LIKE。

### 4.2 以图搜图（`app/embed.py::search_by_image_bytes`）

```
上传字节
  → 解码（JPG/PNG/TIFF/HEIC 内存解码；PSD/AI/PSB/RAW 落 <data_root>/tmp 再解析）
  → pHash 指纹，与全库指纹比对（汉明距离 ≤8 判同源）   ← 20 ms 级别
  → Chinese-CLIP 图像编码（512 维）→ 与 vectors.npy 余弦相似度 top-N
  → 合并：同源置顶（打「同源」徽标），向量结果补足
```

**为什么要两段式**：向量编码是后台分批进行的，全量 20 万张要数小时。在覆盖率不足时，
纯向量检索会出现「库里明明有这张图却搜不到」。指纹只依赖缩略图，能立刻兜住这一类。
另外 CLIP 擅长语义相似，对「同一张图的裁切/换格式/不同尺寸」反而不敏感，指纹正好补这一块。

**为什么流式返回**：首次查询可能要加载模型、解析 PSD、冷读磁盘，耗时不可预测。
接口用 NDJSON 逐行推送阶段（`decode` → `fingerprint` → `encode` → `search`），前端把真实进度显示在搜索框里。

### 4.3 按文件反查相似（`/api/similar/{fid}`）

已入库文件直接用其 `vec_row` 取向量，无需重新解码；没有向量的文件回退到缩略图重新编码。

## 5. 关键设计决策

| 决策 | 原因 |
|---|---|
| 文件身份用 `(st_dev, st_ino)` 而非路径 | 改名、移动父目录后 inode 不变，缩略图与向量可零成本复用 |
| 缩略图命名 `<dev>_<ino>.webp` | 即使 `index.db` 丢失重建，缩略图仍可复用，不产生孤儿文件 |
| 向量用 `np.memmap` 预分配 + 扩容 1.5 倍 | 逐条追加不重写整个矩阵；20 万 × 512 × 4B ≈ 400 MB |
| pHash 存**有符号** int64 | SQLite INTEGER 上限 2^63-1，无符号值写入会失败 |
| `st_dev` / `st_ino` 入库前统一转有符号 int64 | Windows（NTFS）的文件索引号是无符号 64 位，可能超过 2^63；缩略图 key 因此可能是负号开头，接口校验需允许 `-` |
| 队列按文件体积升序 | 小图先出，页面最快有图可看；大型 TIFF 排最后 |
| 上传图常规格式内存解码 | 某些机器上落盘再 unlink 单文件要 1.4 s（杀软实时扫描），内存解码约 150 ms |
| 向量库增量刷新 ids | 编码进程每批都改 `vec_count`，整体重载一次要 400 ms |
| 扫描与 worker 不并发写库 | SQLite 单写者，并发会 `SQLITE_READONLY` / `SQLITE_BUSY` |
| 读连接 `busy_timeout=8s`，写连接 30s | 读请求快速失败好过挂住前端；写请求需要等锁 |
| 前端所有请求带 AbortController 超时 | 后台扫描/写库时接口会变慢，请求堆积会打满线程池、让页面永久转圈 |
| 源目录变更时按相对路径「领养」旧记录 | 换盘后 inode 全变，但相对路径不变，可保留缩略图/向量/指纹，避免全库重算 |

## 6. 并发与顺序

启动顺序（`main.py::_startup`）：

```
init_db()
  └─ boot 线程
       ├─ 清理 tmp/
       ├─ 模型预热（只读文件系统，可与扫描并行）
       ├─ 扫描（距上次成功扫描 <10 分钟则跳过）
       ├─ 启动缩略图 worker
       └─ 续跑向量编码
```

扫描必须在 worker 之前完成，否则扫描的大事务与 worker 的小事务互相抢写锁。

同步接口跑在 anyio 线程池里（默认 40，本项目调到 64）。扫描期间接口变慢是正常的，
但**不应无限等待**——所以前端有超时，读连接的锁等待也限制在 8 秒。

## 7. 性能特征

| 环节 | 实测 | 说明 |
|---|---|---|
| 遍历 27 万文件 | 201 s | 12.7 TB，热缓存；冷启动更慢 |
| diff + 入库 | ~4 s | 无变更时（增量） |
| 缩略图 | 8 线程，机械盘约 30~60 张/s | 大型 TIFF / RAW 明显更慢 |
| 向量编码 | 279 张/s（P4000） | CPU 约 3~8 张/s，MPS 介于两者之间 |
| 名称搜索 | 60~90 ms | 命中数大时排序更久 |
| 以图搜图 | 200~800 ms | 首次含模型加载 |

## 8. 扩展点

- **换向量模型**：只改 `clip_model` 与 `embed.py` 的维度常量，然后删掉 `vectors.npy` 重新 `encode`。数据库结构不用动。
- **加第二套向量**（例如 DINOv2 做构图相似）：给 `files` 加 `vec_row2`，`vectors2.npy` 独立存储，检索时合并排序。
- **多素材根目录**：`source_root` 目前单值，扩展成列表需要在 `_iter_files` 外层循环并给 `top` 加前缀。
