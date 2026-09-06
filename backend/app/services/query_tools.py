"""真实数据查询工具（事件查询层，供 LLM 意图落地与兜底）

原则：数值永远来自本地事件账本/状态引擎；LLM 只负责意图，不负责数值。
单元测试：tests/test_query_tools.py
"""

from __future__ import annotations

from datetime import datetime

from .state_engine.calc import compute, forecast_cashflow


def _yuan(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def _ch_cn(ch: str) -> str:
    return {"wechat": "微信", "alipay": "支付宝", "cash": "现金", "bank": "银行卡", "manual": "手动", "voice": "语音", "receipt": "票据"}.get(ch, ch or "手动")


def latest_event_text(events: list[dict], kind: str) -> str:
    """最近一笔收入/支出。kind ∈ {income, expense}。"""
    cand = [e for e in events if e["type"] == kind]
    if not cand:
        name = "收入" if kind == "income" else "支出"
        return f"账本里还没有{name}记录——先说一笔试试（如「昨天微信收了 3000 尾款」）。"
    last = cand[-1]  # ledger.load() 已按 ts 升序
    name = "收入" if kind == "income" else "支出"
    return (
        f"你最近一笔{name}是 {_yuan(last['amount_cents'])}"
        f"（{last.get('category') or '其他'} · {_ch_cn(last.get('channel'))} · {str(last.get('ts'))[:10]}）。"
    )


def spending_month_text(events: list[dict], as_of=None) -> str:
    """本月支出合计 + 分类 Top3（真聚合）。"""
    now = as_of or datetime.now()
    month = now.strftime("%Y-%m")
    by_cat: dict[str, int] = {}
    total = 0
    for e in events:
        if e["type"] != "expense":
            continue
        if str(e.get("ts"))[:7] != month:
            continue
        total += e["amount_cents"]
        by_cat[e.get("category") or "其他"] = by_cat.get(e.get("category") or "其他", 0) + e["amount_cents"]
    if not by_cat:
        return f"{month} 还没有支出记录。"
    top = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)[:3]
    detail = "，".join(f"{cat} {_yuan(v)}" for cat, v in top)
    return f"{month} 支出合计 {_yuan(total)}：{detail}。" + ("（其余归入其他）" if len(by_cat) > 3 else "")


def recent_events_text(events: list[dict], limit: int = 5) -> str:
    """最近几笔流水摘要。"""
    if not events:
        return "账本还是空的。"
    rows = []
    for e in list(reversed(events))[:limit]:
        name = "收入" if e["type"] in ("income", "refund") else "支出" if e["type"] == "expense" else e["type"]
        rows.append(f"{name} {_yuan(e['amount_cents'])}（{e.get('category') or '其他'} · {_ch_cn(e.get('channel'))} · {str(e.get('ts'))[:10]}）")
    return "最近流水：" + "；".join(rows) + "。"


def cashflow_text(events: list[dict]) -> dict:
    """现金流状态 + 30 天区间（90% 置信带）；数据不足时如实标注不可信。"""
    st = compute(events)
    fc = forecast_cashflow(events, horizon_days=30)
    fc["confidence"] = st["cashflow_confidence"]
    if st["events_count"] == 0:
        return {"text": "账本还是空的——先记几笔，我才能照见你的现金流。", "state": st, "forecast": fc}
    low, med, high = fc["band90_low_cents"], fc["median_balance_cents"], fc["band90_high_cents"]
    gap = "未见缺口"
    if low < 0:
        gap = f"最坏情形 {_yuan(-low)} 缺口"
    if fc.get("insufficient"):
        band = f"期末预计 {_yuan(med)}（数据不足，区间暂不可信——多记或导入几笔后自动变宽）"
    else:
        band = f"期末预计 {_yuan(med)}，区间 {_yuan(low)} ~ {_yuan(high)}（{gap}）"
    text = (
        f"当前状态：{st['label']} · 健康度 {st['financial_health']:.2f} · 现金流可信度 {st['cashflow_confidence']:.2f}。"
        f"未来 30 天{band}。"
    )
    return {"text": text, "state": st, "forecast": fc}
