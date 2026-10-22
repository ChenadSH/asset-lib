# 运维手册

## 1. 启动与停止

```bash
python -m app.cli serve      # 前台启动，Ctrl+C 停止
./start.sh                   # macOS/Linux（先检查配置）
start.bat                    # Windows
```

启动后自动发生三件事：增量扫描（10 分钟内已扫过则跳过）、缩略图 worker、续跑向量编码。

## 2. 什么时候需要手动扫描

| 情况 | 操作 |
|---|---|
| 素材盘新增/删除/改名了文件 | 网页点「重新扫描」，或 `python -m app.cli scan` |
| 换了素材盘/目录 | 见 [MIGRATION.md](MIGRATION.md)，扫描会自动领养旧索引 |
| 改了 `skip_dirs` | 手动扫描一次 |
| 只想刷新缩略图 | 扫描即可，缺图的文件会重新入队 |

「重新扫描」按钮会先弹确认框，并显示按上次实测用时估算的耗时（默认全量约 3~4 分钟 / 27 万文件）。
扫描期间页面可以正常搜索，只是接口会略慢。

> `rescan_interval_min` 目前是预留配置，**定时扫描尚未实现**。需要定时对账的话，用系统计划任务调用
> `python -m app.cli scan`。

## 3. 覆盖率怎么看

右上角状态栏：

```
121,948 张已就绪 · 向量 58%
```

`/api/stats` 里的 `coverage`：

| 字段 | 含义 |
|---|---|
| `searchable` | 可进入图像索引的文件数（图片/设计源文件/RAW/PDF） |
| `fingerprinted` | 已算出 pHash 指纹的数量（依赖缩略图） |
| `vectorized` | 已编码向量的数量 |

- **指纹覆盖**决定「以图搜图能不能找到库里的同一张图」；
- **向量覆盖**决定「语义相似检索」的召回范围；
- 两者都在后台自动补齐，覆盖率到 90%+ 才算跑完。编码中断不影响使用，重启自动续跑。

## 4. 日志

`<data_root>/logs/app.log`，按天轮转，保留 30 天。

```bash
tail -f data/logs/app.log
grep -E "ERROR|WARNING" data/logs/app.log | tail -50
```

常见正常日志：

```
扫描完成 {'ok': True, 'total': 271754, 'new': 0, ...}
模型就绪，device=cuda，耗时 4.2s
```

## 5. 故障排查

### 5.1 页面一直转圈、搜索不出结果

历史上踩过三次，按顺序排查：

1. **加载圈常驻**：看是不是 CSS 覆盖了 `hidden`（本项目已用 `[hidden]{display:none!important}` 兜底）。
2. **请求超时**：后台正在扫描/写库时接口会变慢，前端 15 秒超时后会提示而不是无限等待。
   若持续超时，看日志有没有 `database is locked`。
3. **扫描卡住**：`/api/stats` 里 `scan.running` 与 `started_at`，状态栏会显示「已 X 分 Y 秒」。
   超过 15 分钟没结束，检查素材盘是否掉线（网络盘常见）。

### 5.2 以图搜图搜不到库里明明有的图

先看覆盖率：
- 向量覆盖率低 → 等编码，或先靠指纹（同源）命中；
- 指纹也没命中 → 该文件可能没有缩略图（`thumb=0`），点开 `stats` 看队列是否还有积压；
- 上传的是 PSD/PSB/AI 的变体 → PSB 只取到 160×90 内嵌预览，指纹可能匹配不上，属已知限制。

### 5.3 名称搜不到中文两字词

两字词走 `LIKE`，需要 `%词%` 全表匹配，命中数很大时会被截断为 `1000+`。
三个字以上走 FTS5，速度更快、排序更相关。搜索建议：加一个限定词（如「卫士 海报」）。

### 5.4 缩略图大量显示占位符

```bash
python -m app.cli stats        # 看 queue 与 thumb_failed
```

- `queue` 很大：worker 还在跑，等；机械盘慢属正常；
- `thumb_failed` 增长：看日志里是哪个格式失败（缺 `rawpy` / `pillow-heif` / `PyMuPDF` 最常见）；
- 缩略图文件丢失但 `thumb=1`：迁移时没搬 `thumbs/`，可重置后重生成：

```sql
UPDATE files SET thumb=0 WHERE thumb=1;   -- 谨慎：会重新解码全库
DELETE FROM queue WHERE job='thumb';
```

### 5.5 设备没选对

```bash
python -m app.cli doctor
```

输出会写「配置 auto → 实际 cpu」以及 `cuda/mps` 可用性。Mac 上装了 Apple Silicon 版 torch 但显示 cpu，
通常是 `clip_device` 被固定成了 `cuda`。

### 5.6 服务起来但打不开页面

- 端口被占用：`lsof -i :8765` / `netstat -ano | findstr 8765`；
- 只监听 `127.0.0.1`，局域网访问需要改 `host: 0.0.0.0`（注意本项目没有鉴权，不要暴露到公网）。

## 6. 备份

需要备份的只有派生数据，体积很小：

```bash
# 建议先停服务，保证 index.db 与 vectors.npy 一致
tar -czf asset-index-$(date +%F).tar.gz \
    -C <data_root> index.db vectors.npy thumbs/
```

| 文件 | 丢失后果 |
|---|---|
| `index.db` | 重新扫描重建清单（几分钟），但缩略图/向量会失去映射 |
| `vectors.npy` | 需要重新编码（数小时） |
| `thumbs/` | 需要重新解码生成（数小时） |
| `models/` | 重新下载 750 MB |

`index.db` 与 `vectors.npy` 必须**成对备份**：`vec_row` 是行号，两者不一致会导致检索结果错乱。

## 7. 性能调优

| 目标 | 调整 |
|---|---|
| 缩略图更快 | `thumb_workers` 调到 8~12（SSD）/ 4（机械盘，太多反而抖动） |
| 编码更快 | 提高 `clip_batch`（显存够时 128），或换 GPU |
| 名称搜索更快 | 保证 `idx_status_mtime` 存在；避免频繁用 1~2 个字搜索 |
| 内存占用更低 | `thumb_workers` 降下来；SQLite `cache_size` 在 `db.py` 里（默认 200 MB/连接） |
| 扫描更快 | 素材盘用本地 NVMe；网络盘会慢一个数量级 |

CPU 编码时建议限制线程数，避免抢满机器：`OMP_NUM_THREADS=4 python -m app.cli encode`。

## 8. 安全

- 默认只监听 `127.0.0.1`，没有登录鉴权，**不要直接暴露到公网**；
- 「在资源管理器中打开」会调用系统文件管理器，仅本机可用；
- 素材盘全程只读，程序没有任何删除素材的路径；
- 上传的图只在内存或 `<data_root>/tmp` 中临时存在，1 小时后自动清理。
