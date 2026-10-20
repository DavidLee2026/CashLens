#!/usr/bin/env python3
"""按事件溯源订正一条已入账事件的金额（void + 重记），不重写历史。

背景（2026-10-20）：一张数电票的「价税合计」是 962.00（未税 907.55 + 6% 税额 54.45），
服务器上的 PDF 文本层（PyMuPDF）把「合计」栏的 907.55 读成了总价，整批因此少了 54.45。
解析器已修；这条已经入账的错误事件也要按账本纪律订正：

  1. 追加一条 `type=void`，targets 指向原事件，reason 写明为什么作废；
  2. 用正确金额**重新追加**一条事件（其余字段原样照抄，ts 保留原事件的记录时间）。

账本始终是追加式的：原始行不改，界面与统计经过 `load()` 自动滤掉被作废的那条。
⚠️ 金额变了，dedupe_key 也变（键里含 amount_cents），不会被去重挡下。

用法：
  python3 scripts/fix_event_amount.py <账本路径> --event-id <id> --new-amount-cents 96200
  ... --apply                         # 真写；不加只干跑
  ... --reason "..."                  # 作废原因（写进 void 记录）
  ... --note-suffix "..."             # 追加到新事件 note 末尾的说明
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.state_engine.ledger import EventLedger, make_event  # noqa: E402


def _expense_total(book: EventLedger) -> int:
    return sum(e["amount_cents"] for e in book.load() if e.get("type") == "expense")


def main() -> int:
    ap = argparse.ArgumentParser(description="订正一条事件的金额（void + 重记）")
    ap.add_argument("ledger", help="finance_events.jsonl 路径")
    ap.add_argument("--event-id", required=True, help="要订正的事件 id")
    ap.add_argument("--new-amount-cents", type=int, required=True, help="正确金额（分）")
    ap.add_argument("--reason", default="金额订正", help="写进 void 记录的原因")
    ap.add_argument("--note-suffix", default="", help="追加到新事件 note 末尾的说明")
    ap.add_argument("--apply", action="store_true", help="真写；不加只干跑")
    args = ap.parse_args()

    path = Path(args.ledger)
    book = EventLedger(path)
    live = book.load()
    ev = next((e for e in live if str(e.get("event_id")) == args.event_id), None)
    if ev is None:
        print(f"× 找不到有效事件 {args.event_id}（可能已被作废或 id 不对）")
        return 2
    old = int(ev.get("amount_cents") or 0)
    new = int(args.new_amount_cents)
    if old == new:
        print("× 新旧金额相同，不需要订正")
        return 2
    if new <= 0:
        print("× 新金额必须大于 0")
        return 2

    before = _expense_total(book)
    print(f"账本 {path}")
    print(f"  事件 {args.event_id[:8]} · {ev.get('type')} · {ev.get('category')} · "
          f"{ev.get('counterparty') or '(无商户)'}")
    print(f"  金额 ¥{old / 100:,.2f} → ¥{new / 100:,.2f}  (差额 {(new - old) / 100:+,.2f})")
    print(f"  支出合计 ¥{before / 100:,.2f} → ¥{(before - old + new) / 100:,.2f}")

    if not args.apply:
        print("\n（干跑：什么都没写。加 --apply 才真写。）")
        return 0

    # 1) 作废原事件
    voided = book.append_void([args.event_id], reason=args.reason,
                              project=ev.get("project", ""))
    # 2) 用正确金额重记（其余字段原样照抄）
    note = str(ev.get("note") or "")
    if args.note_suffix:
        note = f"{note}；{args.note_suffix}" if note else args.note_suffix
    extra = {}
    if isinstance(ev.get("extra"), dict):
        extra.update(ev["extra"])
    if ev.get("invoice_no"):
        extra["invoice_no"] = ev["invoice_no"]
    new_ev = make_event(
        ts=ev.get("ts"),
        event_type=ev["type"],
        amount_cents=new,
        evidence_kind=(ev.get("evidence") or {}).get("kind", "voice"),
        channel=ev.get("channel", "manual"),
        category=ev.get("category", ""),
        counterparty=ev.get("counterparty", ""),
        note=note,
        confirmed=(ev.get("evidence") or {}).get("confirmed", True),
        project=ev.get("project", ""),
        extra=extra or None,
    )
    out = book.append(new_ev, strict_dedupe=True)
    if not out.get("appended"):
        print("⚠️ 新事件被去重挡下（同键事件已存在），未写入")
        return 3

    after = _expense_total(book)
    print(f"\n  void {voided['void_event_id'][:8]} → 重记 {out['event_id'][:8]}")
    print(f"  支出合计 ¥{after / 100:,.2f}  "
          f"{'✅' if after == before - old + new else '❌ 与预期不符'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
