#!/usr/bin/env python3
"""放大微信登录二维码 PNG，便于手机扫码识别。

原始截图分辨率较低（约 120x120），直接扫码容易失败。

优先用 macOS 自带的 sips 放大（无第三方依赖）；若 sips 不可用，
再退回 Pillow（如果环境里恰好装了）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def upscale_with_sips(path: Path, factor: int = 5) -> bool:
    """用 macOS 自带的 sips 放大图片，返回是否成功。"""
    sips = shutil.which("sips")
    if sips is None:
        return False
    try:
        result = subprocess.run(
            [sips, "-Z", "1200", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def upscale_with_pillow(path: Path, factor: int = 5) -> bool:
    """退路：使用 Pillow 做最近邻放大。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    image = Image.open(path).convert("RGB")
    image = image.resize((image.width * factor, image.height * factor), Image.NEAREST)
    image.save(path, "PNG")
    return True


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: _upscale_qr.py <qr.png>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"QR file not found: {path}", file=sys.stderr)
        return 1
    if upscale_with_sips(path) or upscale_with_pillow(path):
        print(f"upscaled: {path}")
        return 0
    print("no image upscaler available (sips and Pillow both missing)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
