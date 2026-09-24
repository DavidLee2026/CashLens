"""开票信息生成与报税归集（账本同源聚合，纯本机计算）

输入：事件账本里的收入事件（type == "income"）。
输出：
  1. 开票清单：按客户聚合的收入明细，供用户整理开票信息。
  2. 报税归集：按季度聚合的收入合计与分类分解。

合规边界（重要，与 02-脑暴/合规与边界-责任切分与红线-v1.0.md 一致）：
- 只做「归集与呈现」，不做代开发票、不做税目核定、不计算应缴税额、不给筹划建议。
- 税目只给「候选提示」，且必须由用户确认；征收率与起征点适用一律以主管税务机关口径为准。
- 账本没有「是否已开票」字段，也没有「金额是否含税」字段，因此这两项一律标 unknown，
  交用户确认，绝不推断（2026-09-13 教训：凡不确定，问用户或看数据，不推断）。
"""
from __future__ import annotations

from datetime import datetime

# 按分类给出税目候选提示。这不是核定结论，只帮用户少翻一次资料，必须人工确认。
_TAX_ITEM_HINTS: dict[str, list[str]] = {
    "接单": ["信息技术服务", "现代服务"],
    "服务": ["现代服务"],
    "设计": ["文化创意服务", "设计服务"],
    "咨询": ["鉴证咨询服务"],
    "销售": ["货物销售"],
    "餐饮": ["生活服务"],
    "房租": ["不动产经营租赁"],
    "广告": ["文化创意服务", "广告服务"],
}

_TAX_BASIS_NOTE = ("账本未记录金额是否含税，因此不做价税拆分；"
                   "如需价税合计，请提供适用征收率后由本接口换算，或按实际票面金额确认。")


def _parse_ts(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts)[:19])
    except (TypeError, ValueError):
        return None


def quarter_of(ts) -> str:
    """时间戳 → 季度标签，如 2026Q3。无法解析返回空串。"""
    dt = _parse_ts(ts)
    if dt is None:
        return ""
    return f"{dt.year}Q{(dt.month - 1) // 3 + 1}"


def quarter_period(quarter: str) -> dict:
    """季度标签 → 起止日期。"""
    year = int(quarter[:4])
    qn = int(quarter[-1])
    start_month = (qn - 1) * 3 + 1
    end_month = start_month + 2
    last_day = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][end_month - 1]
    return {"start": f"{year}-{start_month:02d}-01",
            "end": f"{year}-{end_month:02d}-{last_day:02d}"}


def income_events(events: list[dict], since: str | None = None,
                  until: str | None = None) -> list[dict]:
    """筛出收入事件，按时间正序；可按日期区间过滤（闭区间，比较 ts 的前 10 位）。"""
    out = []
    for ev in events or []:
        if str(ev.get("type")) != "income":
            continue
        day = str(ev.get("ts") or "")[:10]
        if since and day < since:
            continue
        if until and day > until:
            continue
        out.append(ev)
    out.sort(key=lambda e: str(e.get("ts") or ""))
    return out


def invoice_draft(events: list[dict], since: str | None = None, until: str | None = None,
                  tax_rate: float | None = None) -> dict:
    """按客户聚合收入明细 → 开票清单。

    tax_rate 为适用征收率（如 0.03）。仅在用户给出时才换算价税，否则只给账本原值。
    """
    rows = income_events(events, since, until)

    groups: dict[str, dict] = {}
    unconfirmed = 0
    for ev in rows:
        if not (ev.get("evidence") or {}).get("confirmed", False):
            unconfirmed += 1
        party = str(ev.get("counterparty") or "").strip() or "未标注客户"
        amount = int(ev.get("amount_cents") or 0)
        g = groups.setdefault(party, {"counterparty": party, "amount_cents": 0,
                                      "count": 0, "items": []})
        g["amount_cents"] += amount
        g["count"] += 1
        g["items"].append({
            "event_id": ev.get("event_id", ""),
            "date": str(ev.get("ts") or "")[:10],
            "amount_cents": amount,
            "category": ev.get("category", ""),
            "note": ev.get("note", ""),
        })

    clients = []
    for g in groups.values():
        entry = dict(g)
        # 税目候选：取该客户各笔分类的去重候选，供用户确认，不是核定结论
        hint: list[str] = []
        for item in g["items"]:
            for cand in _TAX_ITEM_HINTS.get(str(item.get("category") or "").strip(), []):
                if cand not in hint:
                    hint.append(cand)
        entry["tax_item_candidates"] = hint
        entry["tax_item_confirmed"] = False
        entry["invoice_status"] = "unknown"  # 账本无开票状态字段，不推断
        if tax_rate is not None:
            rate = float(tax_rate)
            entry["tax_rate"] = rate
            entry["amount_incl_tax_cents"] = g["amount_cents"]
            entry["amount_excl_tax_cents"] = int(round(g["amount_cents"] / (1 + rate)))
            entry["tax_amount_cents"] = g["amount_cents"] - entry["amount_excl_tax_cents"]
        else:
            entry["tax_rate"] = None
            entry["amount_incl_tax_cents"] = None
            entry["amount_excl_tax_cents"] = None
            entry["tax_amount_cents"] = None
        clients.append(entry)

    clients.sort(key=lambda c: c["amount_cents"], reverse=True)

    return {
        "ok": True,
        "period": {"since": since or "", "until": until or ""},
        "client_count": len(clients),
        "income_count": len(rows),
        "income_total_cents": sum(c["amount_cents"] for c in clients),
        "unconfirmed_count": unconfirmed,
        "tax_basis": "unknown" if tax_rate is None else f"按用户给定征收率 {tax_rate} 换算",
        "clients": clients,
        "disclaimer": "开票信息由账本收入记录聚合得出，仅供参考。开票状态与税目须经用户确认；"
                      "本产品不代开发票、不核定税目、不计算应缴税额。",
    }


def tax_quarterly(events: list[dict], quarter: str | None = None,
                  recent: int = 4) -> dict:
    """按季度归集收入。

    只算事实：季度收入合计、分类分解、客户分解、按月分解、笔数。
    不算应缴税额、不判起征点、不给筹划建议。
    """
    rows = income_events(events)

    by_q: dict[str, list[dict]] = {}
    for ev in rows:
        q = quarter_of(ev.get("ts"))
        if q:
            by_q.setdefault(q, []).append(ev)

    if quarter:
        quarters = [quarter]
    else:
        quarters = sorted(by_q.keys(), reverse=True)[:max(1, int(recent))]

    result = []
    for q in quarters:
        evs = by_q.get(q, [])
        cats: dict[str, dict] = {}
        parties: dict[str, int] = {}
        months: dict[str, int] = {}
        unconfirmed = 0
        for ev in evs:
            amount = int(ev.get("amount_cents") or 0)
            cat = str(ev.get("category") or "").strip() or "未分类"
            c = cats.setdefault(cat, {"category": cat, "amount_cents": 0, "count": 0})
            c["amount_cents"] += amount
            c["count"] += 1
            party = str(ev.get("counterparty") or "").strip() or "未标注客户"
            parties[party] = parties.get(party, 0) + amount
            month = str(ev.get("ts") or "")[:7]
            months[month] = months.get(month, 0) + amount
            if not (ev.get("evidence") or {}).get("confirmed", False):
                unconfirmed += 1
        result.append({
            "quarter": q,
            "period": quarter_period(q) if len(q) == 6 else {"start": "", "end": ""},
            "income_total_cents": sum(c["amount_cents"] for c in cats.values()),
            "income_count": len(evs),
            "unconfirmed_count": unconfirmed,
            "by_category": sorted(cats.values(), key=lambda c: -c["amount_cents"]),
            "by_counterparty": [{"counterparty": k, "amount_cents": v}
                                for k, v in sorted(parties.items(), key=lambda kv: -kv[1])],
            "by_month": [{"month": k, "amount_cents": v} for k, v in sorted(months.items())],
        })

    return {
        "ok": True,
        "quarters": result,
        "available_quarters": sorted(by_q.keys(), reverse=True),
        "disclaimer": "季度收入由账本收入记录归集得出，仅为事实陈述，不代表应纳税额。"
                      "是否达到起征点、适用何种征收率，请以主管税务机关口径为准；"
                      "本产品不提供税务筹划建议。",
    }
