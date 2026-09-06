"""真实查询工具测试 —— 数值全部来自样本事件，纯本地。"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import query_tools  # noqa: E402
from app.services.state_engine import make_event  # noqa: E402
from app.services.state_engine.calc import forecast_cashflow  # noqa: E402

NOW = datetime(2026, 9, 6, 12, 0)


def _events():
    return [
        make_event(ts=NOW - timedelta(days=3), event_type="income", amount_cents=300000,
                   evidence_kind="flow", channel="wechat", category="接单", counterparty="客户A"),
        make_event(ts=NOW - timedelta(days=2), event_type="expense", amount_cents=320000,
                   evidence_kind="voice", channel="manual", category="房租"),
        make_event(ts=NOW - timedelta(days=1), event_type="expense", amount_cents=2800,
                   evidence_kind="voice", channel="manual", category="交通", note="打车报销垫付"),
        make_event(ts=NOW, event_type="income", amount_cents=500000,
                   evidence_kind="voice", channel="manual", category="接单", note="设计尾款"),
    ]


def test_latest_event_text():
    evs = _events()
    assert "¥5,000.00" in query_tools.latest_event_text(evs, "income")
    assert "¥28.00" in query_tools.latest_event_text(evs, "expense")
    assert "还没有收入记录" in query_tools.latest_event_text([], "income")


def test_spending_month_text():
    evs = _events()
    out = query_tools.spending_month_text(evs, as_of=NOW)
    assert "支出合计 ¥3,228.00" in out  # 3200 + 28
    assert "房租" in out and "交通" in out


def test_recent_events_text():
    evs = _events()
    out = query_tools.recent_events_text(evs, limit=3)
    assert "设计尾款" not in out  # 只列最近 3 笔
    assert "¥5,000.00" in out


def test_cashflow_text_insufficient_marked():
    evs = _events()
    out = query_tools.cashflow_text(evs)
    assert "数据不足" in out["text"] or "未见缺口" in out["text"]
    fc = forecast_cashflow(evs, horizon_days=30)
    assert fc["insufficient"] is True  # 4 个不同日期 < 5 → 如实标注
