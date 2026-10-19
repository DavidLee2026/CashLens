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
    # 近 30 天的收入/支出分开算（2026-10-19）：左栏原来摆「记账证据 17%」「推算靠不靠谱 100%」，
    # 客户看了不知道要做什么；换成「进来多少、出去多少、净多少」才是对账要看的数。
    cutoff_30 = as_of - timedelta(days=30)
    income_30d = sum(ev["amount_cents"] for ev in events
                     if ev["type"] in HEALTH_POSITIVE_TYPES and _parse_ts(ev.get("ts")) >= cutoff_30)
    expense_30d = sum(ev["amount_cents"] for ev in events
                      if ev["type"] not in HEALTH_POSITIVE_TYPES and _parse_ts(ev.get("ts")) >= cutoff_30)
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
        "income_30d_cents": int(income_30d),
        "expense_30d_cents": int(expense_30d),
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

    daily: dict = {}
    for ev in events:
        ts = _parse_ts(ev.get("ts"))
        if ts < cutoff:
            continue
        d = ts.date()
        delta = ev["amount_cents"] if ev["type"] in HEALTH_POSITIVE_TYPES else -ev["amount_cents"]
        daily[d] = daily.get(d, 0) + delta

    if not daily:
        # 一笔都没有时**必须**带上 extrapolated=False（2026-10-19 修）：
        # 原来这里漏了这个字段，前端判断的是 `extrapolated === false`，
        # 于是空账本会走进「外推」分支，把三个 0 当成预测结果显示出来 ——
        # 看起来像「预测你 30 天后剩 0 元」，其实是根本没数据。空账上说数字就是编。
        return {
            "median_balance_cents": 0,
            "band90_low_cents": 0,
            "band90_high_cents": 0,
            "confidence": 0.0,
            "insufficient": True,
            "extrapolated": False,
            "reason": f"近 {FORECAST_WINDOW_DAYS} 天还没有记账，先记几笔才能推算",
            "days_with_records": 0,
            "active_weeks": 0,
            "need_days": 5,
            "need_weeks": 4,
        }
    # ─── 按「周」聚合，再往后推（2026-10-19 修）────────────────────────
    # 旧算法：拿「日净流入」的均值 ± 1.65×标准差，**再乘 30 天**。三个问题叠在一起：
    #   ① 只统计「有记录的日子」，没收入的日子被当成不存在 → 均值被抬高；
    #   ② 日粒度噪音极大（一天收 3 万、隔天花 2 万），标准差被放大；
    #   ③ 标准差乘 30 天而不是乘 √30 —— 独立增量的方差才是相加的，这一步又多放大 5.5 倍。
    # 实测后果：106 笔的演示样本算出 −¥134,915 ~ ¥260,622（宽 ¥39.5 万），不可用。
    # 现在改成：90 天切成整周桶（**没有记录的周也计入，记 0**），周净流入当增量，
    # 30 天 ≈ 4.29 周，中心 = 当前净额 + 周均值 × 周数，带宽 = z × 周标准差 × √周数。
    vals = list(daily.values())
    current_balance = sum(vals)  # MVP 代理：近 90 天累计净流当作当前可确认余额

    weeks_total = max(1, FORECAST_WINDOW_DAYS // 7)
    buckets = [0] * weeks_total
    day0 = cutoff.date()
    for d, v in daily.items():
        idx = min(weeks_total - 1, max(0, (d - day0).days // 7))
        buckets[idx] += v
    active_weeks = sum(1 for b in buckets if b != 0)
    mean_week = sum(buckets) / weeks_total
    std_week = statistics.pstdev(buckets) if weeks_total > 1 else 0.0

    # 数据点太少时不做外推，只报「当前已知净额」。
    # 依据对外口径「数据不足时如实标注不可信，不做假装精确的点预测」。
    # 曾出现的错：只记 1 笔 100 元收入时，把当日值当成日均节奏外推 30 天，显示成 3100 元。
    insufficient = len(daily) < 5 or active_weeks < 4
    if insufficient:
        median = low = high = current_balance
        # 文案按「认知负担」重写（2026-10-19）：先说事实（几天有记录），再说下一步（再记几笔）。
        # 旧版把「暂不外推 N 天」「上面显示的是当前已知净额」塞进同一句，客户读不动。
        reason = (f"近 {FORECAST_WINDOW_DAYS} 天只有 {len(daily)} 天有记录，"
                  f"再记几笔就能推算 {horizon_days} 天")
        extrapolated = False
    else:
        weeks_ahead = horizon_days / 7.0
        median = current_balance + mean_week * weeks_ahead
        band = BAND_Z * std_week * (weeks_ahead ** 0.5)   # 方差相加 → 标准差乘 √周数
        low = median - band
        high = median + band
        reason = (f"基于过去 {FORECAST_WINDOW_DAYS} 天每周净流入节奏"
                  f"（{weeks_total} 周里 {active_weeks} 周有记录）")
        extrapolated = True
    return {
        "median_balance_cents": int(median),
        "band90_low_cents": int(low),
        "band90_high_cents": int(high),
        "confidence": None,  # 由调用方用 compute() 的 cashflow_confidence 填充
        "insufficient": insufficient,
        "extrapolated": extrapolated,
        "reason": reason,
        # 「还差多少」要说得出具体数字（界面用）：差天数优先，天数够了就报还差几周。
        "days_with_records": len(daily),
        "active_weeks": active_weeks,
        "need_days": max(0, 5 - len(daily)),
        "need_weeks": max(0, 4 - active_weeks),
    }
