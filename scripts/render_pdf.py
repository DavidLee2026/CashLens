#!/usr/bin/env python3
"""PDF → 高清 PNG 渲染（macOS Quartz 实现，无第三方依赖）
用途：数电发票等 PDF 版式票据 → 图片，喂给 VLM 票据识别管线（receipt_mcp）
用法：
  python3 render_pdf.py <输入.pdf> [输出.png] [倍率]
倍率默认 3（约 1800×2500px，发票文字足够清晰）
"""
import sys

import Quartz
from Foundation import NSURL


def render_pdf(pdf_path: str, out_path: str, scale: float = 3.0) -> str:
    url = NSURL.fileURLWithPath_(pdf_path)
    doc = Quartz.CGPDFDocumentCreateWithURL(url)
    if doc is None:
        raise RuntimeError(f"无法打开 PDF: {pdf_path}")

    page = Quartz.CGPDFDocumentGetPage(doc, 1)
    rect = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFMediaBox)
    w = int(rect.size.width * scale)
    h = int(rect.size.height * scale)

    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, 0, cs, Quartz.kCGImageAlphaPremultipliedLast
    )
    # 白底
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextDrawPDFPage(ctx, page)

    img = Quartz.CGBitmapContextCreateImage(ctx)

    dest = Quartz.CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(out_path), "public.png", 1, None
    )
    Quartz.CGImageDestinationAddImage(dest, img, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise RuntimeError(f"写入 PNG 失败: {out_path}")

    print(f"✅ 渲染完成: {out_path} ({w}x{h})")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 render_pdf.py <输入.pdf> [输出.png] [倍率]")
        sys.exit(1)
    pdf = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else pdf.rsplit(".", 1)[0] + ".png"
    scale = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
    render_pdf(pdf, out, scale)
