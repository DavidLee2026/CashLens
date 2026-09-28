"""决策引擎最小版（确定性计算 + 可溯源理由）

对应设计与 golden case：`00-总览/决策引擎最小版-问题分析与golden-case-20260912.md`

四条纪律（不接受任何"看起来像"的实现）：
1. **纯函数 + 确定性**：同一批事件 + 同一 as_of → 完全一致的结果（可审计、可复现）。
2. **每个结论可溯源**：涉及账本的理由都带 `evidence.event_ids`；系数标注出处。
3. **缺数据就说缺**：算不出历史时薪时返回 `insufficient`，**绝不**拿默认值或估算值假装算得出
   （本项目真踩过：默认值被当成结论说出口，见 `llm_skill.py` 红线第 10 条）。
4. **不 spawn 子代理、LLM 不参与数值**：多视角专家审议属**引擎层**能力（opencode），
   留痕见 `08-实测/多智能体子代理并行审议-实测留痕-20260928.md`。产品运行时走确定性计算，
   两条路互补，**不许混着说**。
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from . import calc
from .ledger import _parse_ts
from .spec import HEALTH_POSITIVE_TYPES

# 四态（沿用状态引擎规格，不新造）
DECISION_STATUSES = ("satisfied", "uncertain", "missing", "misconception")

HISTORY_MONTHS = 3          # 历史时薪回看窗口（可配）
BELOW_MARKET_RATIO = 0.8    # 低于历史时薪的 80% → 判「不接」（golden case B）
RECOMMEND_MARKUP = 1.10     # 推荐报价 = 下限 × 1.10（**经验系数，无文献出处，可调**）
CEILING_MARKUP = 1.25       # 上限 = 下限 × 1.25（同上）

# 余额口径的免责标注：账本里没有「当前余额」这个量（需要用户自己填），
# 预测用的是「近 90 天累计净流」代理 —— 凡引用它就必须写明，否则违反 R39 数据溯源。
_BALANCE_PROXY_NOTE = "（口径：账本里没有「当前余额」，这里用近 90 天累计净流作代理）"

DISCLAIMER = "以上是依据你账本的建议，决定权在你。"


def _yuan(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"¥{cents / 100:,.2f}"


def _as_of(value=None) -> datetime:
    if value is None:
        value = datetime.now()
    if isinstance(value, str):
        value = _parse_ts(value)
    return value.replace(tzinfo=None)


def _hours_of(order: dict) -> float:
    try:
        hours = float(order.get("estimated_hours") or 0)
    except (TypeError, ValueError):
        return 0.0
    return hours if hours > 0 else 0.0


def _income_with_hours(events, cutoff: datetime) -> list[tuple[dict, float]]:
    """近 N 月内**带工时记录**的收入事件。

    两条过滤是刻意的：
    - `self_report`（权重 0.10）只作先验、不直接进事实层，**不得**参与历史时薪分子；
    - 没有 `hours` 的收入单也无法参与（缺工时就算不出时薪，这是本模块唯一的真缺口）。
    """
    out = []
    for ev in events or []:
        if ev.get("type") not in HEALTH_POSITIVE_TYPES:
            continue
        if _parse_ts(ev.get("ts")) < cutoff:
            continue
        if (ev.get("evidence") or {}).get("kind") == "self_report":
            continue
        try:
            hours = float(ev.get("hours"))
        except (TypeError, ValueError):
            continue
        if hours > 0:
            out.append((ev, hours))
    return out


def historical_hourly(events, as_of=None, months: int = HISTORY_MONTHS) -> dict:
    """从账本反推个人历史时薪 = 近 N 月收入**中位数** ÷ 近 N 月总投入工时。

    取中位数而非均值：避免被一单大额拉偏（出处见设计文档 §五·5.1）。
    没有带工时的收入记录时**不编默认时薪**，如实返回 `basis="insufficient"`。
    """
    at = _as_of(as_of)
    cutoff = at - timedelta(days=30 * months)
    pairs = _income_with_hours(events, cutoff)
    amounts = [ev.get("amount_cents", 0) for ev, _ in pairs]
    hours_total = sum(h for _, h in pairs)
    event_ids = [ev.get("event_id", "") for ev, _ in pairs]

    if not pairs or hours_total <= 0:
        return {
            "hourly_cents": None,
            "income_total_cents": sum(amounts),
            "hours_total": hours_total,
            "sample_orders": len(pairs),
            "basis": "insufficient",
            "event_ids": event_ids,
            "reason": (f"近 {months} 个月没有「带工时」的收入记录，算不出你的历史时薪。"
                       "记账时补一句这笔投入了多少小时，攒几笔就能算出来。"),
        }

    median_income = int(statistics.median(amounts))
    hourly = int(round(median_income / hours_total))
    return {
        "hourly_cents": hourly,
        "income_total_cents": sum(amounts),
        "hours_total": hours_total,
        "sample_orders": len(pairs),
        "basis": "ledger",
        "event_ids": event_ids,
        "reason": (f"近 {months} 个月 {len(pairs)} 笔带工时的收入"
                   f"（中位数 {_yuan(median_income)}）÷ {hours_total:g} 小时"),
    }


def _receivable_deviation(events) -> dict | None:
    """假设检验：`assumption`（回款周期）与 `resolution` 的实际值不一致 → 返回偏差。"""
    expected: dict[str, dict] = {}
    for ev in events or []:
        if ev.get("type") == "assumption":
            a = ev.get("assumption") if isinstance(ev.get("assumption"), dict) else {}
            if str(a.get("subject") or "") == "receivable_cycle":
                expected[str(ev.get("event_id"))] = {
                    "expected": a.get("expected"),
                    "ids": [ev.get("event_id")],
                }
        elif ev.get("type") == "resolution":
            ref = str(ev.get("assumption_ref") or "")
            if ref in expected and expected[ref]["expected"] != ev.get("actual"):
                return {
                    "expected": expected[ref]["expected"],
                    "actual": ev.get("actual"),
                    "event_ids": expected[ref]["ids"] + [ev.get("event_id")],
                }
    return None


def decision_check(events, order: dict, as_of=None, historical: dict | None = None) -> list[dict]:
    """三道闸门：`cash_runway` / `receivable_cycle` / `after_tax`，各带 status + reason + action。"""
    at = _as_of(as_of)
    amount = int(order.get("amount_cents") or 0)
    hours = _hours_of(order)
    historical = historical if historical is not None else historical_hourly(events, at)
    hist = historical.get("hourly_cents")
    checks: list[dict] = []

    # ── 闸门一：现金流垫底（能否撑到回款）──────────────────────
    fc = calc.forecast_cashflow(events, at)
    low = int(fc.get("band90_low_cents") or 0)
    fc_evidence = {"kind": "computed", "source": "calc.forecast_cashflow"}
    if fc.get("insufficient"):
        checks.append({
            "name": "cash_runway", "status": "uncertain",
            "reason": f"现金流不明：{fc.get('reason')}",
            "action": "先补几笔真实流水，让现金流区间能算出来",
            "evidence": fc_evidence,
        })
    elif low < 0:
        checks.append({
            "name": "cash_runway", "status": "missing",
            "reason": (f"未来 30 天区间下沿 {_yuan(low)}，存在缺口；"
                       f"若这单还要垫资、回款又拖，现金压力会放大{_BALANCE_PROXY_NOTE}"),
            "action": "优先要预付，或先催历史应收；这单至少不能低于垫资周期",
            "evidence": fc_evidence,
        })
    else:
        checks.append({
            "name": "cash_runway", "status": "satisfied",
            "reason": f"未来 30 天区间下沿 {_yuan(low)}，未出现缺口{_BALANCE_PROXY_NOTE}",
            "action": "",
            "evidence": fc_evidence,
        })

    # ── 闸门二：回款周期（该客户历史 vs 这次的假设）────────────
    dev = _receivable_deviation(events)
    cp = str(order.get("counterparty") or "").strip()
    if dev:
        checks.append({
            "name": "receivable_cycle", "status": "misconception",
            "reason": f"你之前预计回款 {dev['expected']}，实际是 {dev['actual']} —— 回款周期被低估过",
            "action": "下次报价把回款周期的资金占用算进成本",
            "evidence": {"kind": "ledger", "event_ids": dev["event_ids"]},
        })
    elif cp:
        hist_orders = [ev for ev in (events or [])
                       if ev.get("type") in HEALTH_POSITIVE_TYPES
                       and str(ev.get("counterparty") or "").strip() == cp]
        if hist_orders:
            checks.append({
                "name": "receivable_cycle", "status": "satisfied",
                "reason": f"账本里有该客户的 {len(hist_orders)} 笔历史成交，可作回款节奏参考",
                "action": "这单记下预计回款日，回款后我帮你核对偏差",
                "evidence": {"kind": "ledger", "event_ids": [ev.get("event_id") for ev in hist_orders]},
            })
        else:
            checks.append({
                "name": "receivable_cycle", "status": "uncertain",
                "reason": f"账本里没有「{cp}」的历史回款记录，这次的回款周期只是假设",
                "action": "回款到账时记一笔，下次就有据可依",
                "evidence": {"kind": "computed", "source": "ledger_lookup"},
            })
    else:
        checks.append({
            "name": "receivable_cycle", "status": "uncertain",
            "reason": "没提客户，账本里也没有可比对的历史回款记录",
            "action": "补上客户名或预计回款日，我能帮你盯偏差",
            "evidence": {"kind": "computed", "source": "ledger_lookup"},
        })

    # ── 闸门三：税后真实到手（对齐「以为 $65/时其实 $31/时」那个坑）──
    tax = order.get("tax_rate")
    if hours <= 0:
        checks.append({
            "name": "after_tax", "status": "uncertain",
            "reason": "没给预估工时，算不出这笔的等效时薪",
            "action": "补一句预计投入多少小时",
            "evidence": {"kind": "computed", "source": "order"},
        })
    elif hist is None:
        checks.append({
            "name": "after_tax", "status": "uncertain",
            "reason": "历史时薪算不出来（缺带工时的收入记录），没有可对照的基准",
            "action": "记账时补上投入小时数",
            "evidence": {"kind": "computed", "source": "historical_hourly"},
        })
    else:
        rate = 0.0 if tax is None else float(tax)
        pre_tax_hourly = int(round(amount / hours))
        after_tax_hourly = int(round(amount * (1 - rate) / hours))
        note = "未配税负率，按税前口径算" if tax is None else f"税负率 {rate:.0%}"
        # 这句话只在「税前确实达标、被税负拉下来」时才成立，否则是误导（自查发现）
        tax_trap = "—— 税前看着达标，税后并没有" if (pre_tax_hourly >= hist > after_tax_hourly) else ""
        if after_tax_hourly >= hist:
            checks.append({
                "name": "after_tax", "status": "satisfied",
                "reason": f"税后等效时薪 {_yuan(after_tax_hourly)}/时 ≥ 你的历史时薪 {_yuan(hist)}/时（{note}）",
                "action": "",
                "evidence": {"kind": "computed", "source": "after_tax_hourly"},
            })
        else:
            checks.append({
                "name": "after_tax", "status": "missing",
                "reason": (f"税后等效时薪 {_yuan(after_tax_hourly)}/时 < 你的历史时薪 {_yuan(hist)}/时（{note}）"
                           f"{tax_trap}"),
                "action": "要么提价，要么把税负作为成本计入报价",
                "evidence": {"kind": "computed", "source": "after_tax_hourly"},
            })
    return checks


def quote_range(order: dict, historical: dict, as_of=None) -> dict:
    """报价下限 / 推荐 / 上限。

    下限 = 历史时薪 × 预估工时 × (1 + 税负率 + 平台抽成)（公式出处见设计文档 §五·5.2）
    推荐 / 上限的 1.10 / 1.25 是**无文献出处的经验系数**，如实标注、可调。
    """
    hours = _hours_of(order)
    hist = historical.get("hourly_cents")
    tax = 0.0 if order.get("tax_rate") is None else float(order["tax_rate"])
    platform = 0.0 if order.get("platform_rate") is None else float(order["platform_rate"])
    breakdown = {
        "historical_hourly_cents": hist,
        "estimated_hours": hours,
        "tax_rate": tax,
        "platform_rate": platform,
        "formula": "历史时薪 × 预估工时 × (1 + 税负率 + 平台抽成)",
        "markup_note": (f"推荐 = 下限 × {RECOMMEND_MARKUP}、上限 = 下限 × {CEILING_MARKUP}"
                        "（经验系数，无文献出处，可调）"),
    }
    if hist is None or hours <= 0:
        return {"floor_cents": None, "recommend_cents": None, "ceiling_cents": None,
                "breakdown": breakdown, "reason": "缺历史时薪或预估工时，算不出报价下限"}
    floor = int(round(hist * hours * (1 + tax + platform)))
    return {
        "floor_cents": floor,
        "recommend_cents": int(round(floor * RECOMMEND_MARKUP)),
        "ceiling_cents": int(round(floor * CEILING_MARKUP)),
        "breakdown": breakdown,
        "reason": "",
    }


def decide(events: list[dict], order: dict, as_of=None) -> dict:
    """组合历史时薪 + 三道闸门 + 报价区间 → 对外返回体（对应设计文档 §4.2）。"""
    at = _as_of(as_of)
    amount = int(order.get("amount_cents") or 0)
    hours = _hours_of(order)

    historical = historical_hourly(events, at)
    checks = decision_check(events, order, at, historical=historical)
    quote = quote_range(order, historical, at)
    by_name = {c["name"]: c for c in checks}
    hist = historical.get("hourly_cents")
    equivalent = int(round(amount / hours)) if hours > 0 else None

    reasons: list[dict] = []

    # ── 基准判定（设计文档 §5.5）────────────────────────────
    if hist is None:
        decision = "再考虑"
        headline = "账本里还没有「带工时」的收入记录，算不出你的历史时薪 —— 这单我给不了有底的价格判断"
        reasons.append({"text": historical["reason"],
                        "evidence": {"kind": "computed", "source": "historical_hourly"}})
    else:
        ratio = (equivalent or 0) / hist
        if ratio >= 1.0:
            decision = "接"
        elif ratio < BELOW_MARKET_RATIO:
            decision = "不接"
        else:
            decision = "再考虑"
        headline = (f"这单等效时薪 {_yuan(equivalent)}/时，你的历史时薪 {_yuan(hist)}/时"
                    f"（{historical['reason']}）")
        reasons.append({"text": headline,
                        "evidence": {"kind": "ledger", "event_ids": historical["event_ids"]}})
        if decision == "不接":
            reasons.append({
                "text": (f"低于你历史时薪的 {BELOW_MARKET_RATIO:.0%}；要接的话报价下限是 "
                         f"{_yuan(quote['floor_cents'])}（{quote['breakdown']['formula']}）"),
                "evidence": {"kind": "computed", "source": "quote_range"},
            })
        elif decision == "再考虑":
            reasons.append({
                "text": (f"介于历史时薪的 {BELOW_MARKET_RATIO:.0%} 到 100% 之间：能接，但偏低。"
                         f"可参考报价下限 {_yuan(quote['floor_cents'])}"),
                "evidence": {"kind": "computed", "source": "quote_range"},
            })

    # ── 闸门降级（只降不升；`uncertain` 不降级，避免数据少时一律变成「再考虑」）──
    def _downgrade(check_name: str, extra: str = "") -> None:
        nonlocal decision
        if decision == "接":
            decision = "再考虑"
        checks_item = by_name.get(check_name) or {}
        reasons.append({
            "text": (checks_item.get("reason", "") + extra).strip(),
            "evidence": checks_item.get("evidence") or {"kind": "computed", "source": check_name},
        })

    if by_name.get("cash_runway", {}).get("status") == "missing":
        _downgrade("cash_runway")
    if by_name.get("after_tax", {}).get("status") == "missing":
        _downgrade("after_tax")
    if by_name.get("receivable_cycle", {}).get("status") == "misconception":
        _downgrade("receivable_cycle")

    # 现金流不明时如实提示（不改变判定，但必须说出来）
    if by_name.get("cash_runway", {}).get("status") == "uncertain":
        reasons.append({"text": by_name["cash_runway"]["reason"],
                        "evidence": by_name["cash_runway"]["evidence"]})

    # ── 主要风险：取第一个没过的闸门；全过就如实说没有识别到 ──
    risk = ""
    for name in ("cash_runway", "after_tax", "receivable_cycle"):
        if by_name.get(name, {}).get("status") in ("missing", "misconception"):
            risk = by_name[name]["reason"]
            break
    if not risk:
        risk = "账本里没有识别到明显风险；主要不确定性来自「预估工时」本身是否靠谱"

    return {
        "decision": decision,
        "headline": headline,
        "reasons": reasons,
        "min_price_cents": quote["floor_cents"],
        "quote": {"floor_cents": quote["floor_cents"],
                  "recommend_cents": quote["recommend_cents"],
                  "ceiling_cents": quote["ceiling_cents"],
                  "breakdown": quote["breakdown"]},
        "hourly": {"historical_cents": hist,
                   "this_order_cents": equivalent,
                   "basis": historical["reason"],
                   "sample_orders": historical["sample_orders"]},
        "checks": checks,
        "risk": risk,
        "cashflow_confidence": calc.compute(events, at)["cashflow_confidence"],
        "order": {"amount_cents": amount, "estimated_hours": hours,
                  "deliver_days": order.get("deliver_days"),
                  "counterparty": str(order.get("counterparty") or "")},
        "as_of": at.isoformat(timespec="seconds"),
        "disclaimer": DISCLAIMER,
    }
