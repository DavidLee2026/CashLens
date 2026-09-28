"""把被 `void` 作废的事件**救回来**（误删可救）—— 追加式账本的正确用法。

背景（2026-09-28 真机）：在界面上用「删除项目（连账单一起删）」把 17 笔一并作废后，
统计里就一分钱都没有了。但账本是**追加式**的：原始事件一条都没被改写，
只是多了一条 `type=void` 指向它们。所以"恢复"＝**再追加一份活的事件**，
而不是去改文件（本脚本绝不重写已有行）。

⚠️ 本脚本只做一件事：把指定的"被作废支出事件"照原样（金额 / 渠道 / 分类 / 商户 /
备注 / 项目）重新追加为 **confirmed 事件**，`ts` 用当前时间（账本记录的是"何时记的"）。
去重键含 `project|channel|amount|日期|category|counterparty`，与被作废的那条**不同**
（作废记录没有 dedupe_key，不在去重集合里），所以不会被 strict_dedupe 挡下。

用法：
  python3 scripts/restore_voided_events.py <账本路径>                # 干跑，只看不改
  python3 scripts/restore_voided_events.py <账本路径> --apply        # 真写
  ... --reason "删除项目「X」时一并作废"                              # 只恢复某条 void 波及的事件
  ... --project project_e                                           # 换一个项目归属
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.state_engine.ledger import EventLedger, make_event  # noqa: E402


def _arg(flag: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    apply = "--apply" in sys.argv
    only_reason = _arg("--reason")
    project = _arg("--project")

    raw = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    by_id = {e["event_id"]: e for e in raw}

    # 找出"被作废的支出事件"：只认 void 记录 targets 里点到的，且本体确实是 expense
    targets: dict[str, str] = {}
    for rec in raw:
        if rec.get("type") != "void":
            continue
        if only_reason and only_reason not in str(rec.get("reason") or ""):
            continue
        for t in rec.get("targets") or []:
            targets[str(t)] = str(rec.get("reason") or "")

    todo = [by_id[t] for t in targets if t in by_id and by_id[t].get("type") == "expense"]
    todo.sort(key=lambda e: (str(e.get("project")), e.get("amount_cents", 0)))

    total = sum(e.get("amount_cents", 0) for e in todo)
    print(f"账本 {path}")
    print(f"  总行数 {len(raw)} · 待恢复支出事件 {len(todo)} 笔 · 合计 ¥{total / 100:,.2f}")
    if not todo:
        print("\n（没有匹配的待恢复事件。）")
        return 0

    print("\n  明细：")
    for e in todo:
        note = str(e.get("note") or "")[:32]
        print(f"    ¥{e['amount_cents'] / 100:>9.2f}  {str(e.get('category') or '(空)'):<6}"
              f"  项目={e.get('project') or '(未归)':<10}  {note}")

    if not apply:
        print("\n（干跑：什么都没写。加 --apply 才真写。）")
        return 0

    book = EventLedger(path)
    ok = 0
    for e in todo:
        new = make_event(
            ts=datetime.now(), event_type=e["type"], amount_cents=e["amount_cents"],
            evidence_kind=(e.get("evidence") or {}).get("kind", "voice"),
            channel=e.get("channel", "manual"), category=e.get("category", ""),
            counterparty=e.get("counterparty", ""), note=e.get("note", ""),
            confirmed=(e.get("evidence") or {}).get("confirmed", True),
            project=(project or e.get("project", "")),
        )
        r = book.append(new, strict_dedupe=True)
        ok += 1 if r.get("appended") else 0
    live = EventLedger(path).load()
    print(f"\n  已重记 {ok} 笔；当前有效事件 {len(live)} 笔，"
          f"合计支出 ¥{sum(x['amount_cents'] for x in live if x['type'] == 'expense') / 100:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
