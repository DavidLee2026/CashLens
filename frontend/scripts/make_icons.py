#!/usr/bin/env python3
"""生成 PWA 图标到 frontend/public/（2026-10-20 立）

## 为什么要这个脚本

图标是二进制，**按项目纪律不入库**（隐私闸会拦 `.png`：合成素材一律放 `samples/`）。
所以图标是「构建产物」而不是「源文件」：形状与配色写在这个脚本里，谁 clone 下来都能重新生成。

## 用法（发布前跑一次，或本地开发前跑一次）

    cd 09-代码/frontend
    python3 scripts/make_icons.py

产出（均被 .gitignore 忽略）：
    public/icon-192.png
    public/icon-512.png
    public/icon-512-maskable.png   # 四周留品牌色安全区，Android 自适应图标用
    public/apple-touch-icon.png    # iOS 加到主屏用（iOS 不认 SVG 图标）

⚠️ 忘了跑不会报错，只是「加到主屏」时图标是空白/截图。发布脚本里跟着 `next build` 一起跑即可。
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

BRAND_FROM = (99, 91, 255)    # --indigo-500
BRAND_TO = (0, 184, 217)      # --cyan-500


def _rounded_gradient(size: int) -> Image.Image:
    """圆角方 + 对角渐变（与 .brand-mark 的 linear-gradient(135deg) 同向同色）。"""
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(int(BRAND_FROM[i] + (BRAND_TO[i] - BRAND_FROM[i]) * t) for i in range(3))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _magnifier(size: int) -> Image.Image:
    """白色放大镜：圆环 + 斜柄，和登录页/左栏 logo 里的 SVG 同形。"""
    img = _rounded_gradient(size)
    d = ImageDraw.Draw(img)
    cx, cy, rad = size * 0.46, size * 0.46, size * 0.22
    w = max(2, int(size * 0.055))
    d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=(255, 255, 255, 255), width=w)
    d.line([cx + rad * 0.72, cy + rad * 0.72, size * 0.76, size * 0.76], fill=(255, 255, 255, 255), width=w)
    return img


def main() -> int:
    out = pathlib.Path(__file__).resolve().parents[1] / "public"
    out.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        _magnifier(size).save(out / f"icon-{size}.png")
    # maskable：图形缩到 76%，四周填品牌色（Android 自适应图标会裁边）
    size = 512
    base = Image.new("RGBA", (size, size), BRAND_FROM + (255,))
    inner = _magnifier(int(size * 0.76))
    off = (size - inner.size[0]) // 2
    base.paste(inner, (off, off), inner)
    base.save(out / f"icon-{size}-maskable.png")
    _magnifier(180).save(out / "apple-touch-icon.png")
    print(f"✓ 图标已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
