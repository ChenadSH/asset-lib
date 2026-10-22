# 开发指南

## 1. 代码结构

```
app/
├─ config.py    配置加载（环境变量 > config.yaml > 默认）、路径解析、设备选择、文件类型分类
├─ db.py        SQLite 连接与 schema、FTS 触发器
├─ scanner.py   目录遍历、diff、换路径领养、任务入队
├─ extract.py   各格式缩略图提取 + pHash 计算
├─ worker.py    缩略图队列消费者（多线程）
├─ embed.py     CLIP 模型加载、编码、向量存储、相似检索、NDJSON 阶段流
├─ search.py    名称检索（LIKE / FTS 自适应）、facets、stats
├─ main.py      FastAPI 路由、后台任务编排、静态资源
├─ cli.py       命令行入口（serve / doctor / scan / encode / worker / stats）
└─ static/      前端（无构建步骤，原生 HTML/CSS/JS）
tools/          一次性脚本：格式探测、权重转换、基准测试
docs/           文档
```

## 2. 模块边界

| 模块 | 可以做 | 不要做 |
|---|---|---|
| `config.py` | 读配置、算路径、判设备 | 读数据库、起线程 |
| `scanner.py` | 遍历、写 files/queue/meta | 解码图片、调用模型 |
| `extract.py` | 纯函数式解码，失败返回 `None` | 访问数据库、写日志以外的副作用 |
| `worker.py` | 消费队列、写 thumb/phash | 遍历目录、改 status |
| `embed.py` | 模型、向量、相似度 | 改名称检索逻辑 |
| `search.py` | 只读查询 | 任何写操作 |
| `main.py` | 路由、启动编排 | 业务算法 |

**贯穿全局的约定**：

1. **素材盘只读**。任何代码路径都不得写入 `source_root`。
2. **失败降级不抛异常**。`extract()` 返回 `None`、接口返回占位、编码失败记 `attempts` 后跳过。
3. **时间戳统一为整秒 Unix 时间**（`int(time.time())`），与 `files.mtime` 一致。
4. **写库必须用独立连接**，不要跨线程共享 `sqlite3.Connection`。

## 3. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 单页前端 |
| GET | `/api/search?q=&kind=&top=&year=&offset=&limit=` | 名称检索；`kind` 逗号分隔，`top` 用 `\|\|` 分隔 |
| GET | `/api/facets` | 筛选维度与计数（60 秒缓存） |
| GET | `/api/stats` | 索引统计、队列、覆盖率、扫描状态（10 秒缓存） |
| GET | `/api/file/{fid}` | 单文件详情 |
| POST | `/api/reveal/{fid}` | 在系统文件管理器中定位 |
| GET | `/thumb/{dev}_{ino}` | 缩略图 WebP；未生成为 204 |
| POST | `/api/similar` | 以图搜图（一次性返回 JSON） |
| POST | `/api/similar/stream` | 以图搜图（NDJSON 流式，推荐） |
| GET | `/api/similar/{fid}` | 用库内文件的向量找相似 |
| GET | `/api/text-search?q=` | 文本搜图（跨语言语义检索） |
| POST | `/api/rescan` | 触发后台扫描 |
| GET | `/api/rescan/estimate` | 上次扫描用时与预估 |

### NDJSON 流的约定

`/api/similar/stream` 每行一个 JSON：

```json
{"stage":"decode","pct":10,"text":"解码上传图…"}
{"stage":"fingerprint","pct":30,"text":"比对全库指纹…"}
{"stage":"encode","pct":55,"text":"编码向量…"}
{"stage":"search","pct":80,"text":"检索相似…"}
{"stage":"done","pct":100,"text":"完成","result":{"items":[...],"total":42}}
```

前端收到 `done` 之前只更新进度；`error` 阶段用红字提示。新增阶段时保持 `stage/pct/text` 三字段不变。

## 4. 前端

`static/app.js` 是单文件状态机，没有框架、没有构建步骤：

- `state`：查询词、模式（name/image）、筛选集合、offset、loading、seq；
- `search(append)`：唯一的数据加载入口，两种模式（重置/追加）；
- `searchByImage(file)`：读 NDJSON 流，驱动搜索框里的缩略图与进度条；
- `refreshStatus()`：5 秒轮询 `/api/stats`，带防堆积判断。

注意三点：

1. **加载状态归属**：每次请求自增 `state.seq`，只有 `mySeq === state.seq` 时才允许写 DOM / 复位 loading。
2. **请求可取消**：新查询会 abort 旧请求，否则快速输入时旧结果会覆盖新结果。
3. **静态资源带版本号**：`index.html` 里 `app.js?v=YYYYMMDD`，改了前端记得更新版本号，避免用户拿到缓存。

## 5. 本地调试

```bash
# 热重载
uvicorn app.main:app --reload --port 8765

# 指定临时配置（不影响本机 config.yaml）
ASSET_LIB_CONFIG=./config.test.yaml python -m app.cli serve

# 只跑某个环节
python -m app.cli scan
python -m app.cli worker
python -m app.cli encode
```

### 用无头浏览器验证前端

不需要装 Playwright，Edge/Chrome 自带 CDP：

```bash
# 1) 启动带调试端口的无头浏览器
msedge --headless=new --remote-debugging-port=9223 \
  --user-data-dir=/tmp/cdp-profile http://127.0.0.1:8765/

# 2) 用 Node（22+ 自带 WebSocket）连 CDP 做断言 / 截图
node tools/cdp_probe.mjs
```

`tools/cdp_probe.mjs` 提供了最小封装：`Runtime.evaluate` 读 DOM 状态、`Page.captureScreenshot` 截图。
验证无限滚动时可以循环「滚到底 → 等 2 秒 → 数卡片」看是否持续增长。

### 常用 SQL

```sql
-- 队列与失败分布
SELECT job, count(*) FROM queue GROUP BY job;
SELECT thumb, count(*) FROM files GROUP BY thumb;

-- 覆盖率
SELECT count(*) searchable FROM files WHERE status='ready' AND kind IN ('raster','raw','design','doc');
SELECT count(*) vec FROM files WHERE vec_row IS NOT NULL;

-- 某格式的失败率
SELECT ext, count(*) n, sum(thumb=2) failed FROM files GROUP BY ext ORDER BY n DESC LIMIT 20;
```

## 6. 扩展指引

### 加一种新格式

1. `config.py` 把扩展名放进对应的集合（`RASTER` / `DESIGN` / ...）；
2. `extract.py` 在 `extract()` 里加分支，返回 `PIL.Image`，失败返回 `None`；
3. 跑 `python -m app.cli scan` 让新文件入队。

### 换向量模型

1. `config.yaml` 改 `clip_model`；
2. `embed.py` 里 `_DIM` 与新模型对齐；
3. 删除 `vectors.npy` 与 `meta.vec_count`，重新 `encode`；
4. 数据库结构不用动（`vec_row` 是通用行号）。

### 加筛选维度

1. `files` 表加列（`ALTER TABLE` + 索引）；
2. `scanner.py` 入库时填充；
3. `search.py::name_search` 拼条件，`facets()` 加分组；
4. 前端 `filters` 区加一组 chip，请求里带参数。

## 7. 测试与基准

```bash
python tools/bench_thumb.py       # 缩略图提取吞吐
python tools/probe_extract.py     # 各格式能否出图
python tools/convert_weights.py   # .bin → safetensors（transformers 5 需要）
```

改完关键路径后至少验证：

- `python -m app.cli doctor` 通过；
- 名称搜索（中文两字 / 三字以上 / 空查询）都能出结果；
- 以图搜图能返回同源 + 相似；
- 滚动到底能持续加载（用上面的 CDP 脚本）。
