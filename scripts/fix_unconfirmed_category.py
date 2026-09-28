"""分类订正：把账本里 2 条把占位符「待确认」当分类的事件，按事件溯源规矩纠正。

做法（**不重写历史**）：
  1. 追加一条 `type=void`，targets 指向原事件，reason 写明为什么作废；
  2. 用正确的分类**重新追加一条**新事件（其余字段原样照抄）。
账本是追加式的，所以历史留痕、可回溯；`load()` 会把被作废的那条在读取时滤掉，
因此界面与统计看到的就是纠正后的结果。

⚠️ `dedupe_key = sha256(project|channel|amount|日期|category|counterparty)` **含 category**，
所以分类变了键就变了，不会和原事件撞去重。

用法：
  python3 scripts/fix_unconfirmed_category.py <账本路径>          # 先干跑（只看不改）
  python3 scripts/fix_unconfirmed_category.py <账本路径> --apply  # 真写
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.state_engine.ledger import EventLedger, make_event  # noqa: E402

# 占位符 → 真分类。两笔的口径来自用户 + `categories.py` 的关键词表
# （「经营」里明确列着 打印/复印/办公/耗材，所以打印费进「经营」而不是「其他」）。
# ⚠️ 这里的 event_id 来自某一次具体的账本，换个数据目录就失效（脚本会打印"跳过"）。
# 仓库是 Public，所以注释里不写商户名。
FIXES: dict[str, str] = {
    "15a9e9be231741db8dea7340569e11b6": "住宿",  # ¥962.00 数电票
    "436c420d125843cdb9cd38bfb40ad4af": "经营",  # ¥78.00 打印费
}
REASON = "分类订正：「待确认」是等人确认的池子、不是一个类别，不能当分类落账本（见 categories.py §1）"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    apply = "--apply" in sys.argv

    raw = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    live = EventLedger(path).load()
    by_id = {e["event_id"]: e for e in live}

    todo = []
    for eid, new_cat in FIXES.items():
        ev = by_id.get(eid)
        if ev is None:
            print(f"  跳过 {eid[:8]}：不在账本里（或已被作废）")
            continue
        todo.append((ev, new_cat))

    print(f"账本 {path}")
    print(f"  总行数 {len(raw)} · 有效事件 {len(live)} · 待订正 {len(todo)}")
    for ev, new_cat in todo:
        print(f"  · {ev['amount_cents'] / 100:>9.2f}  {ev.get('category')!r} → {new_cat!r}  ({ev['event_id'][:8]})")

    before = sum(e["amount_cents"] for e in live if e["type"] == "expense")
    print(f"  订正前支出合计 ¥{before / 100:,.2f}")

    if not apply:
        print("\n（干跑：什么都没写。加 --apply 才真写。）")
        return 0

    book = EventLedger(path)
    for ev, new_cat in todo:
        v = book.append_void([ev["event_id"]], reason=REASON, project=ev.get("project", ""))
        new_ev = make_event(
            ts=datetime.now(), event_type=ev["type"], amount_cents=ev["amount_cents"],
            evidence_kind=(ev.get("evidence") or {}).get("kind", "voice"),
            channel=ev.get("channel", "manual"), category=new_cat,
            counterparty=ev.get("counterparty", ""), note=ev.get("note", ""),
            confirmed=(ev.get("evidence") or {}).get("confirmed", True),
            project=ev.get("project", ""),
        )
        r = book.append(new_ev, strict_dedupe=True)
        print(f"  作废 {v['void_event_id'][:8]} → 重记 {r['event_id'][:8]} "
              f"({new_cat} {'已写入' if r['appended'] else '⚠️ 被去重挡下'})")

    after_live = EventLedger(path).load()
    after = sum(e["amount_cents"] for e in after_live if e["type"] == "expense")
    cats = sorted({e.get("category") for e in after_live if e["type"] == "expense"})
    print(f"\n  订正后支出合计 ¥{after / 100:,.2f}  {'✅ 未变' if after == before else '❌ 变了！'}")
    print(f"  现有支出分类: {cats}")
    print(f"  「待确认」还在吗: {'❌ 在' if '待确认' in cats else '✅ 不在'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
