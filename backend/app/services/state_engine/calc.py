"""读取时计算（asOf）——状态、区间、前置检查

对应 v0.2 规格 §五/§六/§七/§八。全部是纯函数/确定性计算：
同一批事件 + 同一 model_version + 同一 asOf → 同一结果（可审计）。
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from .ledger import _parse_ts
from .spec import (
    BAND_Z,
    CONFIDENCE_FLOOR,
    DECAY_BASE,
    DEFAULT_STABILITY_DAYS,
    FORECAST_WINDOW_DAYS,
    HEALTH_POSITIVE_TYPES,
    INITIAL_HEALTH,
    LABEL_FRAGILE,
    LABEL_LEARNING,
    LABEL_MISCONCEPTION,
    LABEL_REVIEW_DUE,
    LABEL_STABLE,
    LABEL_UNKNOWN,
    LEARNING_RATE,
)

_clamp01 = lambda v: max(0.0, min(1.0, v))


def _net_30d(events, as_of: datetime) -> int:
    cutoff = as_of - timedelta(days=30)
    total = 0
    for ev in events:
        if _parse_ts(ev.get("ts")) < cutoff:
            continue
        if ev["type"] in HEALTH_POSITIVE_TYPES:
            total += ev["amount_cents"]
        elif ev["type"] == "expense":
            total -= ev["amount_cents"]
    return total


def _income_stability_days(events, as_of: datetime) -> int:
    """过去 90 天内收入事件间隔的中位数（保守口径）。"""
    cutoff = as_of - timedelta(days=FORECAST_WINDOW_DAYS)
    ts_list = [
        _parse_ts(ev.get("ts"))
        for ev in events
        if ev["type"] in HEALTH_POSITIVE_TYPES and _parse_ts(ev.get("ts")) >= cutoff
    ]
    ts_list.sort()
    if len(ts_list) < 2:
        return DEFAULT_STABILITY_DAYS
    gaps = [(b - a).days for a, b in zip(ts_list, ts_list[1:]) if (b - a).days >= 0]
    if not gaps:
        return DEFAULT_STABILITY_DAYS
    return int(statistics.median(gaps)) or DEFAULT_STABILITY_DAYS


def _has_misconception(events) -> bool:
    """假设检验：resolution 的实际值 ≠ assumption 期望 → misconception。"""
    expected_by_ref = {}
    for ev in events:
        if ev["type"] == "assumption" and isinstance(ev.get("assumption"), dict):
            expected_by_ref[ev["event_id"]] = ev["assumption"].get("expected")
        if ev["type"] == "resolution":
            ref = ev.get("assumption_ref")
            actual = ev.get("actual")
            if ref in expected_by_ref and expected_by_ref[ref] != actual:
                return True
    return False


def compute(events: list[dict], as_of=None) -> dict:
    """重放全部事件 → 输出当前财务状态（读取时算，可审计）。"""
    as_of = as_of or datetime.now()
    if isinstance(as_of, str):
        as_of = _parse_ts(as_of)
    as_of = as_of.replace(tzinfo=None)

    incomes = [ev for ev in events if ev["type"] in HEALTH_POSITIVE_TYPES]
    latest_income_ts = max((_parse_ts(ev.get("ts")) for ev in incomes), default=None)
    stability_days = _income_stability_days(events, as_of)
    if latest_income_ts is not None:
        elapsed_days = max(0, (as_of - latest_income_ts).days)
        confidence = DECAY_BASE ** (elapsed_days / max(stability_days, 1))
    else:
        elapsed_days = None
        confidence = CONFIDENCE_FLOOR

    # 财务健康度：收敛式重放（初值 0.5，事件按 ts 顺序迭代）
    # 语句事件（amount=0 的自报/推测，如『我这月没问题』）不参与更新——自报只作先验，不直接改变状态
    health = INITIAL_HEALTH
    for ev in events:
        w = (ev.get("evidence") or {}).get("weight", 0.0)
        if w <= 0 or ev.get("amount_cents", 0) <= 0:
            continue
        observed = 1.0 if ev["type"] in HEALTH_POSITIVE_TYPES else 0.0
        health = _clamp01(health + LEARNING_RATE * w * (observed - health))

    net_30d = _net_30d(events, as_of)
    flow_count = sum(1 for ev in events if (ev.get("evidence") or {}).get("kind") == "flow")

    # 状态标签（MVP 判定表：misconception > unknown > learning > fragile > review_due > stable）
    if _has_misconception(events):
        label = LABEL_MISCONCEPTION
    elif latest_income_ts is None:
        label = LABEL_UNKNOWN
    elif len(events) < 3:
        label = LABEL_LEARNING
    elif net_30d < 0:
        label = LABEL_FRAGILE
    elif confidence < 0.5:
        label = LABEL_REVIEW_DUE
    elif flow_count >= 3 and confidence >= 0.7:
        label = LABEL_STABLE
    else:
        label = LABEL_LEARNING

    actions = _actions_of(label, confidence)
    return {
        "as_of": as_of.isoformat(timespec="seconds"),
        "financial_health": round(health, 4),
        "cashflow_confidence": round(confidence, 4),
        "stability_days": stability_days,
        "label": label,
        "net_30d_cents": net_30d,
        "elapsed_days_since_last_income": elapsed_days,
        "events_count": len(events),
        "flow_count": flow_count,
        "actions": actions,
    }


def _actions_of(label: str, confidence: float) -> list[str]:
    if label in (LABEL_UNKNOWN, LABEL_LEARNING):
        return ["diagnose"]          # 先补几笔真实流水
    if label == LABEL_FRAGILE:
        return ["diagnose", "retrieve"]
    if label == LABEL_REVIEW_DUE:
        return ["retrieve"]
    if label == LABEL_MISCONCEPTION:
        return ["repair"]
    return []


def forecast_cashflow(events: list[dict], as_of=None, horizon_days: int = 30) -> dict:
    """未来 horizon 天现金流区间（90% 置信带近似）。

    MVP 用「过去 90 天日净流入」的历史节奏法；Chronos/MOMENT 等重模型留 Phase 2。
    """
    as_of = as_of or datetime.now()
    if isinstance(as_of, str):
        as_of = _parse_ts(as_of)
    as_of = as_of.replace(tzinfo=None)
    cutoff = as_of - timedelta(days=FORECAST_WINDOW_DAYS)

    daily = {}
    for ev in events:
        d = _parse_ts(ev.get("ts")).date()
        if _parse_ts(ev.get("ts")) < cutoff:
            continue
        delta = ev["amount_cents"] if ev["type"] in HEALTH_POSITIVE_TYPES else -ev["amount_cents"]
        daily[d] = daily.get(d, 0) + delta

    if not daily:
        return {
            "median_balance_cents": 0,
            "band90_low_cents": 0,
            "band90_high_cents": 0,
            "confidence": 0.0,
            "reason": "近 90 天无事件，现金流不明",
        }
    vals = list(daily.values())
    mean = sum(vals) / len(vals)
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    current_balance = sum(vals)  # MVP 代理：近 90 天累计净流当作当前可确认余额

    median = current_balance + mean * horizon_days
    low = current_balance + (mean - BAND_Z * std) * horizon_days
    high = current_balance + (mean + BAND_Z * std) * horizon_days
    return {
        "median_balance_cents": int(median),
        "band90_low_cents": int(low),
        "band90_high_cents": int(high),
        "confidence": None,  # 由调用方用 compute() 的 cashflow_confidence 填充
        "reason": f"基于过去 {FORECAST_WINDOW_DAYS} 天日净流入节奏（{len(vals)} 天有记录）",
    }
