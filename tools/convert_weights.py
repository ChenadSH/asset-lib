"""把 Chinese-CLIP 的 pytorch_model.bin 转成 safetensors。

背景：transformers 5 只解析 model.safetensors，而 OFA-Sys 仓库只提供
pytorch_model.bin，直接 from_pretrained 会报
"does not appear to have a file named pytorch_model.bin or model.safetensors"。

用法（只跑一次）：
    python tools/convert_weights.py
"""
import os
import sys
import time

import torch

BASE = r"D:\asset-lib\models\chinese-clip"


def main() -> None:
    bin_path = os.path.join(BASE, "pytorch_model.bin")
    st_path = os.path.join(BASE, "model.safetensors")
    if not os.path.exists(bin_path):
        print("缺少 pytorch_model.bin，请先下载：")
        print("  curl -L -o %s https://huggingface.co/OFA-Sys/chinese-clip-vit-base-patch16/resolve/main/pytorch_model.bin" % bin_path)
        sys.exit(1)
    if os.path.exists(st_path):
        print("已存在 model.safetensors，跳过")
        return

    from safetensors.torch import save_file

    t0 = time.time()
    sd = torch.load(bin_path, map_location="cpu", weights_only=True)
    sd = {k: (v.contiguous() if hasattr(v, "contiguous") else v) for k, v in sd.items()}
    save_file(sd, st_path)
    print("转换完成：%d 个张量 → %.0f MB，耗时 %.1fs"
          % (len(sd), os.path.getsize(st_path) / 1024**2, time.time() - t0))
    print("原始 .bin 可删除以省 753 MB")


if __name__ == "__main__":
    main()
