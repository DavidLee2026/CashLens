#!/usr/bin/env python3
"""批量识别票据样本：目录下所有图片 → 结构化对比表（支持 lite/turbo A/B）
用法：
  python3 batch_recognize.py <图片目录>                    # 默认用当前模型
  python3 batch_recognize.py <图片目录> --model doubao-seed-2-1-turbo-XXXX  # 切模型对比
  python3 batch_recognize.py <图片目录> --json             # 输出 JSON 方便程序处理
"""
import argparse
import json
import os
import subprocess
import sys

# 引擎 MCP 目录（receipt_mcp 等）在 09-代码/engine/mcp/
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "engine", "mcp"))
import receipt_mcp

IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".HEIC")


def convert_heic(path: str) -> str:
    """iPhone HEIC → JPG（macOS sips 自带）"""
    jpg = path + ".jpg"
    if not os.path.exists(jpg):
        subprocess.run(["sips", "-s", "format", "jpeg", path, "--out", jpg],
                       capture_output=True, check=True)
    return jpg


def main():
    parser = argparse.ArgumentParser(description="批量识别票据样本")
    parser.add_argument("dir", help="图片目录")
    parser.add_argument("--model", default=None, help="切换模型（A/B 对比用）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if args.model:
        receipt_mcp.MODEL = args.model

    images = sorted(f for f in os.listdir(args.dir)
                    if f.lower().endswith(IMG_EXT))
    if not images:
        print(f"❌ 目录中没有图片: {args.dir}")
        sys.exit(1)

    print(f"📷 共 {len(images)} 张图片 · 模型: {receipt_mcp.MODEL}\n")
    results = []
    for name in images:
        path = os.path.join(args.dir, name)
        if path.lower().endswith((".heic",)):
            path = convert_heic(path)
            name += " → (已转JPG)"
        r = receipt_mcp.recognize_receipt(path)
        results.append({"file": name, "result": r})
        if "error" in r:
            print(f"❌ {name}: {r['error']}")
            continue
        c = r.get("confidence", 0)
        flag = "✅" if c >= 0.7 else ("⚠️" if c >= 0.4 else "❌")
        print(f"{flag} {name}")
        print(f"    金额: {r.get('amount', '?')} | 日期: {r.get('date', '?')} | "
              f"商户: {r.get('merchant', '?')} | 分类: {r.get('category', '?')} | "
              f"置信度: {c}")
        if r.get("note"):
            print(f"    备注: {r['note'][:80]}")
        print()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    # 汇总统计
    ok = sum(1 for x in results if x["result"].get("confidence", 0) >= 0.7)
    mid = sum(1 for x in results if 0.4 <= x["result"].get("confidence", 0) < 0.7)
    bad = len(results) - ok - mid
    print(f"═══ 汇总（{receipt_mcp.MODEL}）═══")
    print(f"✅ 高置信度(≥0.7): {ok} | ⚠️ 中置信度(0.4-0.7): {mid} | ❌ 低/失败: {bad}")


if __name__ == "__main__":
    main()
