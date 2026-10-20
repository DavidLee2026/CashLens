"""项目对账（2026-10-20 立，A 档 ②）

## 它回答什么

「这个项目的账，对不对得上？」——三个数摆在一起，差额说人话：

| 栏 | 含义 | 什么时候有 |
|:--|:--|:--|
| 账面 | 账本里挂在这个项目上的收入/支出/净额 | 永远有 |
| 有账单凭证的收入 | 收入里来自微信/支付宝/银行账单的那部分 | 导入过账单才有 |
| 票据 | 有多少笔能追到票（原文件 / 发票号 / 票据类通道） | 传过票才有 |

## 三条纪律

1. **只读**：不改账本、不改项目，连"关账"都不在这个模块里做（见 `projects.close_project`）。
2. **有几栏出几栏**：没有账单凭证就说 `available: false`，**不拿 0 冒充"到账 0 元"**。
3. **口径复用 `projects.detail`**：绝不另算一套 —— 同一笔在总览/详情/对账里必须是一个数。
"""
from __future__ import annotations

from pathlib import Path

from . import projects as projects_svc

# 这些通道的收入算「有账单凭证」：钱是从微信/支付宝/银行卡流水里来的，可回原账单查
_BILL_CHANNELS = {"wechat", "alipay", "bank"}
# 这些证据类型算「有票」：票据/文件类
_DOC_KINDS = {"receipt", "pdf", "excel", "csv", "invoice"}
_CN = {"wechat": "微信", "alipay": "支付宝", "bank": "银行"}


def _invoice_no(ev: dict) -> str:
    no = ev.get("invoice_no")
    if not no:
        extra = ev.get("extra")
        if isinstance(extra, dict):
            no = extra.get("invoice_no")
    return str(no or "").strip()


def _has_doc(ev: dict, evidence_map: dict[str, str]) -> bool:
    if str(ev.get("event_id") or "") in evidence_map:
        return True
    if _invoice_no(ev):
        return True
    evd = ev.get("evidence") if isinstance(ev.get("evidence"), dict) else {}
    return str(evd.get("kind") or "") in _DOC_KINDS


def reconcile(data_dir: str | Path, events: list[dict] | None, ref: str,
              evidence_map: dict[str, str] | None = None) -> dict:
    """算一个项目的对账三栏。ref 可以是项目 id / 显示名 / 「未归项目」。"""
    evidence_map = evidence_map or {}
    d = projects_svc.detail(data_dir, events, ref)
    if not d.get("ok"):
        return d

    proj = d.get("project") or {}
    pid = str(proj.get("id") or "")
    totals = d.get("totals") or {}
    book = {
        "income_cents": int(totals.get("income_cents") or 0),
        "expense_cents": int(totals.get("expense_cents") or 0),
        "net_cents": int(totals.get("net_cents") or 0),
        "count": int(totals.get("count") or 0),
    }

    # 该项目名下的事件（已删项目名下的账回落到未归项目，与 summary 一致）
    rows = projects_svc._sync(data_dir, events)          # noqa: SLF001 - 同一模块族，复用它的回退口径
    deleted = {str(r.get("id")) for r in rows if projects_svc.is_deleted(r)}
    mine: list[dict] = []
    for ev in events or []:
        ep = str(ev.get("project") or "")
        if ep in deleted:
            ep = projects_svc.UNASSIGNED_PROJECT
        if ep == pid:
            mine.append(ev)

    # ── 有账单凭证的收入 ──
    bill_income = 0
    bill_count = 0
    bill_channels: set[str] = set()
    for ev in mine:
        if str(ev.get("type")) not in ("income", "refund"):
            continue
        ch = str(ev.get("channel") or "")
        if ch in _BILL_CHANNELS:
            bill_income += int(ev.get("amount_cents") or 0)
            bill_count += 1
            bill_channels.add(ch)
    bill = {
        "available": bill_count > 0,
        "income_cents": bill_income if bill_count else 0,
        "count": bill_count,
        "channels": [_CN.get(c, c) for c in sorted(bill_channels)],
    }

    # ── 票据 ──
    with_doc = 0
    no_doc: list[dict] = []
    for ev in mine:
        if _has_doc(ev, evidence_map):
            with_doc += 1
        else:
            no_doc.append({
                "event_id": str(ev.get("event_id") or ""),
                "ts": str(ev.get("ts") or "")[:10],
                "amount_cents": int(ev.get("amount_cents") or 0),
                "category": str(ev.get("category") or ""),
            })
    docs = {"total": len(mine), "with_doc": with_doc,
            "missing": len(mine) - with_doc, "missing_items": no_doc[:20]}

    # ── 差额（有几栏才几栏）──
    diff = {
        "book_vs_bill_cents": (book["income_cents"] - bill["income_cents"])
                              if bill["available"] else None,
        "missing_docs": docs["missing"],
    }

    notes: list[str] = []
    if diff["book_vs_bill_cents"] not in (None, 0):
        v = diff["book_vs_bill_cents"]
        notes.append(
            f"账面收入比账单收入 {'多' if v > 0 else '少'} ¥{abs(v) / 100:.2f}"
            "——常见原因：平台手续费没记、退款没记、或有一笔只说了没进流水。"
        )
    if docs["missing"]:
        notes.append(f"有 {docs['missing']} 笔还追不到票（可在待确认/明细里补传，不影响已入账的数字）。")
    if not bill["available"]:
        notes.append("没导入过微信/支付宝账单，所以这一栏不显示——不是「到账 0 元」。")
    if book["count"] == 0:
        notes.append("这个项目还没有一笔账。")

    return {
        "ok": True,
        "project": {"id": pid, "name": proj.get("name") or "",
                    "closed_at": str(proj.get("closed_at") or "")},
        "book": book,
        "bill": bill,
        "docs": docs,
        "diff": diff,
        "notes": notes,
        "disclaimer": "对账只呈现「账本、账单、票据」三者能不能对上，不判断钱该不该花。",
    }
