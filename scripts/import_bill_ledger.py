#!/usr/bin/env python3
"""把 parse_bill_csv.py 产出的账单草稿（data/bill_out/ledger.jsonl）回灌进事件账本。

用途（2026-09-28 立）：演示与录屏需要一段**有跨度的真实数据**，否则
`/api/forecast` 会因「近 90 天只有 1 天有记录」如实返回 insufficient，
头牌功能（CashPulse 30/90 天区间）在屏幕上就是空的。

为什么必须单独一个脚本、而不是直接跑 parse_bill_csv.py：
  parse_bill_csv.py 的产物是**草稿**（字段 date/direction/channel/category/nature…），
  应用内入账要的是**规范事件**（schema_version=3，ts/type/amount_cents/evidence/dedupe_key）。
  两者的字段名与语义都不同，中间这一层转换必须显式、可审计、可回滚。

纪律（与本项目账本一致）：
  1. **只追加**，绝不重写 finance_events.jsonl；
  2. 写入前必须 `--backup`（默认开启），备份目录 data/_backup_before_billimport_<ts>/；
  3. `dedupe_unique=True`：账单里的**同一天同金额同分类**是真实的多笔
     （一天买两杯 9.9 的咖啡是两笔），不能像票据那样被折叠掉一笔；
  4. `evidence_kind="flow"`：数据来自**平台导出的流水文件**，不是用户口述，
     按 spec 的证据分级（flow 1.00 > receipt 0.85 > voice 0.75）如实标为最高档；
  5. 中性行（不计收支：转账/还款/理财）**不导入** —— 它们不是收支事件。

用法：
  # 干跑，先看会写入什么
  python3 scripts/import_bill_ledger.py --dry-run

  # 正式回灌（自动备份）
  python3 scripts/import_bill_ledger.py

  # 回滚：把备份覆盖回去
  python3 scripts/import_bill_ledger.py --restore data/_backup_before_billimport_20260928-185531
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 09-代码/
sys.path.insert(0, str(ROOT / "backend"))

from app.services import categories as _cat            # noqa: E402
from app.services import pending as _pending           # noqa: E402
from app.services.state_engine import EventLedger, make_event  # noqa: E402

SRC_DEFAULT = ROOT / "data" / "bill_out" / "ledger.jsonl"
LEDGER_DEFAULT = ROOT / "data" / "finance_events.jsonl"

# 草稿方向 → 规范事件类型
_DIR_MAP = {"expense": "expense", "income": "income"}


def _to_events(rows: list[dict]) -> tuple[list[dict], Counter]:
    """草稿行 → 规范事件。返回 (事件列表, 统计)。"""
    events: list[dict] = []
    stat: Counter = Counter()
    for r in rows:
        direction = str(r.get("direction") or "")
        if direction == "neutral":
            stat["跳过·不计收支"] += 1
            continue
        etype = _DIR_MAP.get(direction)
        if not etype:
            stat[f"跳过·未知方向({direction})"] += 1
            continue
        cents = int(r.get("amount_cents") or 0)
        if cents <= 0:
            stat["跳过·金额为0"] += 1
            continue
        ts = str(r.get("date") or "").strip()
        if not ts:
            stat["跳过·无日期"] += 1
            continue

        raw_cat = str(r.get("category") or "")
        # 分类归一化统一走唯一权威表；再走 pending._ledger_category 收口占位符。
        # 为什么必须收：「待确认」是"要人来确认的池子"，不是一个类别（categories.py §1）。
        # 它一旦被原样写进账本，就会出现在支出构成统计里 —— 2026-09-28 真机就这样落过 2 条，
        # 所以入账口径统一收成「其他」/「其他收入」，与新造分类名相比，这才是本流水线原有的桶。
        if etype == "income":
            cat = _cat.normalize_category(raw_cat, r.get("counterparty", ""), r.get("description", ""))
            if cat not in _cat.INCOME_CATEGORIES:
                cat = "其他收入"
        else:
            cat = _cat.normalize_category(raw_cat, r.get("counterparty", ""), r.get("description", ""))
        cat = _pending._ledger_category(cat, etype)

        note = " ｜ ".join(
            x for x in (
                str(r.get("description") or "").strip(),
                f"订单号 {r.get('order_id')}" if r.get("order_id") else "",
                f"来源 {r.get('source_file', '')[:24]}…" if r.get("source_file") else "",
            ) if x
        )
        events.append(
            make_event(
                ts=ts,
                event_type=etype,
                amount_cents=cents,
                evidence_kind="flow",          # 平台导出流水：最高证据档，如实标注
                channel=str(r.get("channel") or "bill"),
                category=cat,
                counterparty=str(r.get("counterparty") or ""),
                note=note,
                confirmed=True,                 # 平台流水是既成事实，非用户自报
                project="",                     # 未归项目：演示数据不冒充任何项目
                dedupe_unique=True,             # 同天同额同分类的多笔是真实多笔
            )
        )
        stat[f"导入·{etype}"] += 1
    return events, stat


def _backup(ledger: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ledger.parent / f"_backup_before_billimport_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("finance_events.jsonl", "projects.json", "proj.db"):
        src = ledger.parent / name
        if src.exists():
            shutil.copy2(src, dest / name)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="账单草稿回灌进事件账本")
    ap.add_argument("--src", default=str(SRC_DEFAULT), help="账单草稿 jsonl")
    ap.add_argument("--ledger", default=str(LEDGER_DEFAULT), help="目标事件账本")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    ap.add_argument("--restore", metavar="BACKUP_DIR", help="从备份目录回滚账本")
    args = ap.parse_args()

    ledger = Path(args.ledger)
    if args.restore:
        src = Path(args.restore)
        if not (src / "finance_events.jsonl").exists():
            print(f"❌ 备份目录里没有 finance_events.jsonl：{src}")
            return 1
        for name in ("finance_events.jsonl", "projects.json", "proj.db"):
            if (src / name).exists():
                shutil.copy2(src / name, ledger.parent / name)
                print(f"  ↩️  已还原 {name}")
        print(f"✅ 回滚完成（来自 {src.name}）")
        return 0

    rows = [json.loads(l) for l in Path(args.src).read_text(encoding="utf-8").splitlines() if l.strip()]
    events, stat = _to_events(rows)

    print(f"源草稿：{args.src}")
    print(f"  行数 {len(rows)}")
    for k in sorted(stat):
        print(f"  {k}: {stat[k]}")
    print(f"  待写入事件：{len(events)} 笔")
    if events:
        ts = sorted(e["ts"] for e in events)
        inc = sum(e["amount_cents"] for e in events if e["type"] == "income") / 100
        exp = sum(e["amount_cents"] for e in events if e["type"] == "expense") / 100
        days = len({e["ts"][:10] for e in events})
        print(f"  区间 {ts[0][:10]} → {ts[-1][:10]}（{days} 个有记录的日子）")
        print(f"  收入 ¥{inc:,.2f} ｜ 支出 ¥{exp:,.2f}")
        print(f"  分类：{dict(Counter(e['category'] for e in events))}")

    if args.dry_run:
        print("\n（dry-run，未写入任何文件）")
        return 0

    if not args.no_backup:
        dest = _backup(ledger)
        print(f"\n已备份原账本 → {dest}")

    book = EventLedger(ledger)
    written = skipped = 0
    for e in events:
        res = book.append(e, strict_dedupe=False)   # 已用 dedupe_unique 保证键唯一
        written += 1 if res.get("appended") else 0
        skipped += 0 if res.get("appended") else 1
    print(f"✅ 写入 {written} 笔（未写入 {skipped} 笔）")

    after = EventLedger(ledger).load()
    print(f"账本现有活事件：{len(after)} 笔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
