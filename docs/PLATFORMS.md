# 平台适配：Windows / macOS / Linux

## 1. 推理设备

`clip_device: auto` 的解析顺序：**CUDA → MPS（Apple Silicon）→ CPU**。显式写 `cuda` / `mps` / `cpu`
但当前机器不可用时，会自动降级而不是启动失败。

| 平台 | 典型设备 | 安装 torch |
|---|---|---|
| Windows + NVIDIA | `cuda` | 按 [pytorch.org](https://pytorch.org/get-started/locally/) 选对应 CUDA 版本 |
| macOS Apple Silicon | `mps` | `pip install torch`（官方 wheel 自带 MPS） |
| macOS Intel | `cpu` | `pip install torch` |
| Linux + NVIDIA | `cuda` | 同 Windows |
| 无 GPU | `cpu` | `pip install torch --index-url https://download.pytorch.org/whl/cpu` |

`python -m app.cli doctor` 会打印「配置 → 实际」的解析结果，以及 `cuda` / `mps` 可用性。

### 性能参考

| 设备 | 向量编码吞吐 | 说明 |
|---|---|---|
| Quadro P4000（本机实测） | 279 张/s | 21 万张约 12 分钟 |
| Apple Silicon MPS | 明显快于 CPU，慢于同代独显 | 未在本机实测，建议用 `python -m app.cli encode` 的日志自行计时 |
| CPU | 个位数 张/s | 21 万张需要数小时，建议只在夜间跑 |

编码是**可断点续跑**的：中断后重启会从 `vec_count` 继续，不会重头再来。

### MPS 的两个注意点

1. **首次调用会编译 Metal kernel**，前几批明显慢，属于正常现象。
2. 个别算子 MPS 未实现时会报错。本项目只用到 `ChineseCLIPModel.forward` 的常规算子，实测路径简单；
   如果遇到 `NotImplementedError`，把 `clip_device` 改成 `cpu` 即可（只影响编码速度）。

## 2. 路径

| 平台 | 写法 | 备注 |
|---|---|---|
| Windows | `"E:\\"`、`"E:\\Design Assets"` | YAML 里反斜杠要转义或用正斜杠 |
| macOS | `"/Volumes/Design"`、`"~/Pictures"` | 外置盘挂载点通常带空格，注意引号 |
| Linux | `"/mnt/data/design"`、`"~/assets"` | |

- 支持 `~` 和环境变量展开；
- 相对路径以**仓库根目录**为基准；
- 路径统一经 `os.path.normpath` 处理，数据库里存的是平台原生分隔符。

### macOS 外置盘挂载点会变

同名卷被重复挂载时会变成 `/Volumes/Design 1`。这会触发一次「源目录变更」→ 按相对路径领养旧记录，
缩略图会跟着改名。正确做法是卸载后重新挂载，而不是用带序号的新挂载点。

### Windows 长路径

素材路径经常超过 260 字符。建议开启长路径支持（管理员 PowerShell）：

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
```

Python 3.6+ 在 Windows 上默认已启用长路径感知，但部分第三方库（如旧版 rawpy）仍可能失败。

## 3. 文件系统差异

| 差异 | 影响 | 处理 |
|---|---|---|
| 大小写敏感性 | macOS 默认不敏感、Linux 敏感 | 名称搜索是 `LIKE`/FTS 匹配，不做路径去重，一般无感 |
| Unicode 规范化 | macOS 可能以 NFD 存储文件名 | CJK 不受影响；带音标的西文（é → e+́）搜索可能漏，必要时用 `unicodedata.normalize` 归一 |
| inode 稳定性 | ext4 / APFS / NTFS 稳定；部分网络盘（SMB/NFS）不稳定 | 网络盘上改名/重挂载会导致缩略图重新生成 |
| Windows inode 无符号 64 位 | NTFS 文件索引号可能 > 2^63，直接写 SQLite 会溢出 | 扫描入库前统一做无符号→有符号转换（`_sid`），缩略图 key 与库内表示保持一致 |
| macOS/Linux inode 有符号 | 与 SQLite 范围天然兼容 | `_sid` 转换为恒等操作，无影响 |
| 硬链接/软链接 | 硬链接共用 inode（会去重）；软链接按目标文件处理 | 符合预期 |

## 4. 平台特有的行为

### Windows

- **杀软实时扫描**：某些机器上删除一个 3 MB 临时文件要 1.4 秒。因此上传图常规格式（JPG/PNG/TIFF/HEIC）
  走内存解码，只有 PSD/RAW 才落盘到 `<data_root>/tmp`（`tmp` 会在启动时清理 1 小时前的残留）。
- **打开文件所在位置**：`explorer /select,<path>`。
- **启动脚本**：`start.bat`。

### macOS

- **权限**：遍历素材盘需要「完全磁盘访问权限」（系统设置 → 隐私与安全性 → 完全磁盘访问权限，
  把终端 / iTerm / VS Code 加进去）。外置盘首次访问还会弹「允许访问可移除卷」。
- **打开文件所在位置**：`open -R <path>`。
- **Spotlight 竞争 IO**：素材盘刚接入时 Spotlight 会狂扫，扫描/生成缩略图会明显变慢，可先把该卷加入
  Spotlight 隐私列表。
- **启动脚本**：`start.sh`。

### Linux

- **打开文件所在位置**：`xdg-open <目录>`（需要桌面环境）。
- **文件描述符限制**：`ulimit -n` 建议 ≥ 4096。
- **启动**：`start.sh`，或写 systemd unit 常驻。

#### systemd 示例

```ini
# /etc/systemd/system/asset-lib.service
[Unit]
Description=Asset Library
After=network.target

[Service]
WorkingDirectory=/opt/asset-lib
Environment=ASSET_LIB_SOURCE=/mnt/data/design
Environment=ASSET_LIB_DATA=/var/lib/asset-lib
ExecStart=/opt/asset-lib/.venv/bin/python -m app.cli serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 5. 依赖的平台差异

| 包 | Windows | macOS | Linux | 缺失时 |
|---|---|---|---|---|
| `torch` | 需装对应 CUDA 轮子 | pip 默认 MPS/CPU | 同 Windows | 启动失败 |
| `rawpy` | wheel | wheel | wheel | RAW 无缩略图 |
| `pillow-heif` | wheel | wheel | wheel | HEIC/HEIF 无缩略图 |
| `PyMuPDF` | wheel | wheel | wheel | AI/EPS/PDF 无缩略图 |
| `psd-tools` | 纯 Python | 纯 Python | 纯 Python | 无影响（当前用内置解析器读 PSD 预览图） |

可选库缺失不会让程序崩溃：对应格式的缩略图生成失败，文件仍可通过名称检索到，
缩略图位置显示格式占位符。
