#!/usr/bin/env python3
"""邀请码管理：建码 / 列码 / 停码 / 换码（2026-10-19）

## 用法

    cd 09-代码
    python3 scripts/manage_tenants.py create --label "客户A"      # 生成一个码（只打印这一次）
    python3 scripts/manage_tenants.py list                        # 看有哪些租户（不含码）
    python3 scripts/manage_tenants.py rotate <slug>               # 换码（旧码立即失效，账本不动）
    python3 scripts/manage_tenants.py revoke <slug>               # 停用（会话立刻失效）

数据落在 `$CASH_DATA_DIR`（默认 `09-代码/data`）：
    tenants.json        # slug / label / salt / code_hash / active（**没有明文码**）
    session.key         # 会话签名密钥（600）
    tenants/<slug>/     # 这个租户自己的账本、草稿、项目、proj.db、_intake
    model_config.json   # 模型配置留在根目录（设备级，不属于任何客户）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services import tenancy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="CashLens 邀请码管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("create", help="建一个邀请码（明文只打印这一次）")
    p_new.add_argument("--label", default="", help="给谁用，比如「客户A」")

    sub.add_parser("list", help="列出租户（不含码）")

    for name, text in (("rotate", "换码（旧码立即失效）"), ("revoke", "停用该租户")):
        p = sub.add_parser(name, help=text)
        p.add_argument("slug")

    args = ap.parse_args()

    if args.cmd == "create":
        code, slug = tenancy.create_code(args.label)
        print(f"✓ 已创建：{args.label or '未命名'}（slug={slug}）")
        print(f"  邀请码：{code}")
        print("  只显示这一次。丢了就用 rotate 换一个。")
        print(f"  数据目录：{tenancy.tenants_dir() / slug}")
        return 0

    if args.cmd == "list":
        rows = tenancy.list_tenants()
        if not rows:
            print("（还没有租户）")
            return 0
        print(f"{'slug':<10}{'状态':<6}{'最近使用':<22}{'备注'}")
        for r in rows:
            print(f"{r['slug']:<10}{'在用' if r['active'] else '停用':<6}"
                  f"{(r['last_seen'] or '—'):<22}{r['label']}")
        return 0

    if args.cmd == "rotate":
        code = tenancy.rotate(args.slug)
        if not code:
            print(f"✗ 没有这个租户：{args.slug}", file=sys.stderr)
            return 1
        print(f"✓ 已换码（{args.slug}）：{code}")
        print("  旧码与旧会话立即失效；账本一个字没动。")
        return 0

    if args.cmd == "revoke":
        if not tenancy.revoke(args.slug):
            print(f"✗ 没有这个租户：{args.slug}", file=sys.stderr)
            return 1
        print(f"✓ 已停用 {args.slug}：现有会话立刻失效，数据仍留在原地（不删）")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
