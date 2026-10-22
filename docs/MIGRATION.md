# 迁移指南：换盘、换目录、换机器

## 0. 先理解迁移的代价

索引里有三样东西绑定"文件身份"：

| 数据 | 绑定方式 | 换盘/换机器后 |
|---|---|---|
| 缩略图 | 文件名 `<st_dev>_<st_ino>.webp` | 身份变了 → 需按相对路径改名复用 |
| 向量 | `files.vec_row` 行号 | 与身份无关，**可直接复用** |
| 指纹 pHash | 存在 `files.phash` | 与身份无关，**可直接复用** |

扫描时会自动做这一步：检测到 `source_root` 变化后，按**相对路径**把旧记录平移到新路径，
并把缩略图文件改名到新身份。日志里会打印：

```
检测到源目录变更：E:\ → /Volumes/Design，尝试按相对路径沿用旧索引…
已沿用 206431 条旧记录（缩略图/向量/指纹保留）
缩略图改名 118902/206431 个，耗时 12.4s
```

**能复用的前提是目录结构不变**（文件的相对路径一致）。如果搬家时顺手重组了文件夹，
未匹配到的文件会被标记 `missing`，新路径会重新走缩略图 + 向量编码（耗时但结果正确）。

## 1. 场景 A：同一台机器，素材换盘符 / 换挂载点 / 换文件夹

最常见。例如 `E:\` → `D:\Design Assets`，或 macOS 上 `/Volumes/Design 1` → `/Volumes/Design`。

```bash
# 1) 停服务（避免迁移中途被扫描）
#    Windows：关掉 start.bat 的窗口
#    macOS/Linux：Ctrl+C

# 2) 改配置（两种方式任选）
#    a. 编辑 config.yaml
#       source_root: "D:\\Design Assets"
#    b. 或用环境变量
export ASSET_LIB_SOURCE="/Volumes/Design"

# 3) 自检 → 扫描（迁移就发生在这一步）
python -m app.cli doctor
python -m app.cli scan

# 4) 启动
python -m app.cli serve
```

检查点：

- `scan` 输出的 JSON 里 `moved` / `new` 应该都接近 0（如果没有重组目录）；
- 资源管理器/访达里随便打开几张图，缩略图应能正常显示；
- 右上角覆盖率不应暴跌。

## 2. 场景 B：换机器（同操作系统）

### 2.1 数据目录要不要一起搬？

| 迁移内容 | 体积（21 万素材实测） | 建议 |
|---|---|---|
| `index.db` | ~370 MB | **建议搬**，省掉一次全量扫描（约 3~5 分钟，机械盘更久） |
| `vectors.npy` | ~400 MB | **建议搬**，否则要重新编码数小时 |
| `thumbs/` | ~1.2 GB | 建议搬，否则要重新解码生成 |
| `models/chinese-clip/` | ~750 MB | 可选，能省一次下载；跨平台也能用（权重与平台无关） |

搬过去以后，`source_root` 大概率变了（盘符不同），走场景 A 的流程即可。

```bash
# 旧机器：停服务后打包（只打派生数据）
tar -czf asset-lib-data.tar.gz -C /path/to/asset-lib data/
# 新机器：解到仓库目录，然后改 source_root 并 scan
```

### 2.2 环境准备

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.cli doctor      # 逐项检查路径 / 依赖 / 设备 / 模型
```

`doctor` 会明确告诉你缺什么：素材目录没挂载、数据目录不可写、缺哪个格式库、`clip_device` 实际解析成了什么。

## 3. 场景 C：Windows → macOS（跨平台）

代码本身跨平台，但有四处差异需要知道（详见 [PLATFORMS.md](PLATFORMS.md)）：

1. **推理设备**：`clip_device: auto` 会在 Apple Silicon 上自动选 `mps`，无需改配置。首次会慢一些（Metal 编译），之后正常。
2. **路径写法**：`E:\Design` → `/Volumes/Design`。Windows 路径里存进数据库的反斜杠会被新扫描替换，无需手工处理。
3. **缩略图/向量可直接复用**，但缩略图文件名里的 `<dev>_<ino>` 会变，靠自动改名衔接。
4. **首次运行 macOS 会弹权限请求**：访问外置盘或「桌面/文稿/下载」需要授权；终端/IDE 需要「完全磁盘访问权限」才能遍历受保护目录。

迁移步骤同场景 B，然后 `python -m app.cli scan` 触发领养。

## 4. 只搬数据目录（素材不动）

例如把派生数据从 `D:\asset-lib` 搬到 `F:\asset-index`：

```yaml
# config.yaml
source_root: "E:\\"
data_root: "F:\\asset-index"
```

把 `index.db`、`vectors.npy`、`thumbs/`、`models/` 整体拷到新目录，启动即可。
`source_root` 没变，不会触发领养，缩略图文件名继续有效。

## 5. 彻底重建（不保留任何缓存）

```bash
# 停服务
rm -rf data/                 # 或删掉你配置的 data_root 下所有内容
python -m app.cli scan       # 重建清单（27 万文件约 3~5 分钟）
python -m app.cli encode     # 重新编码向量（GPU 约 12 分钟 / 21 万张，CPU 数小时）
```

重建是全量代价：缩略图 worker 会按体积从小到大排队，页面很快就有图可看。

## 6. 校验清单

迁移完成后按顺序确认：

```bash
python -m app.cli doctor        # 路径 / 依赖 / 设备
python -m app.cli stats         # total 是否与预期一致，missing 是否异常
```

- `stats.total` ≈ 素材文件数；
- `stats.missing` 应该接近 0（大说明目录结构变了，或部分文件没搬过来）；
- `stats.thumbs` / `stats.embeddings` 不应明显低于迁移前；
- 网页里做一次名称搜索 + 一次以图搜图。

## 7. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 扫描输出 `源目录不可访问，跳过本轮` | 盘没挂载 / 路径写错 | 检查挂载点，重新 `doctor` |
| 扫描输出 `扫描到 0 个文件，按离线处理` | 挂载了一个空目录 | 这是保护机制，不会清空索引；修正路径后重扫 |
| 迁移后缩略图全是占位符 | 改名前中断 / `thumbs/` 没一起搬 | 重新 `scan` 一次；仍不行就让它重新生成（`UPDATE files SET thumb=0`） |
| 覆盖率骤降 | `vectors.npy` 没搬，或维度不匹配 | 看日志里的维度报错；不匹配只能重新 `encode` |
| macOS 上遍历到一半权限错误 | 缺完全磁盘访问权限 | 系统设置 → 隐私与安全性 → 完全磁盘访问权限，加上终端/IDE |
| 服务起来但设备是 `cpu` | 没装 CUDA 版 torch / 非 Apple Silicon | `doctor` 里看解析结果；Mac 上正常就是 `mps` 或 `cpu` |
| 换盘后 inode 全变，缩略图重新生成很久 | 目录结构变了，领养匹配失败 | 属于预期代价；也可先整理回原结构再迁移 |
