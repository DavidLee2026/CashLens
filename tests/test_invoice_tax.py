"""开票信息生成与报税归集单测"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import invoice_tax as it  # noqa: E402


def ev(ts, amount_cents, category="接单", counterparty="", confirmed=True, type_="income"):
    return {
        "event_id": f"e-{ts}-{amount_cents}",
        "ts": ts,
        "type": type_,
        "amount_cents": amount_cents,
        "category": category,
        "counterparty": counterparty,
        "note": "",
        "evidence": {"kind": "voice", "weight": 0.75, "confirmed": confirmed},
    }


# ─── 季度工具 ───────────────────────────────────────────
def test_quarter_of():
    assert it.quarter_of("2026-08-26T10:00:00") == "2026Q3"
    assert it.quarter_of("2026-01-01T00:00:00") == "2026Q1"
    assert it.quarter_of("2026-12-31T23:59:59") == "2026Q4"
    assert it.quarter_of("坏数据") == ""


def test_quarter_period():
    assert it.quarter_period("2026Q3") == {"start": "2026-07-01", "end": "2026-09-30"}
    assert it.quarter_period("2026Q1") == {"start": "2026-01-01", "end": "2026-03-31"}
    # 闰年 2 月
    assert it.quarter_period("2028Q1") == {"start": "2028-01-01", "end": "2028-03-31"}
    assert it.quarter_period("2026Q4") == {"start": "2026-10-01", "end": "2026-12-31"}


# ─── 收入筛选 ───────────────────────────────────────────
def test_income_events_filters_and_sorts():
    events = [
        ev("2026-08-20T10:00:00", 100),
        ev("2026-08-10T10:00:00", 200, type_="expense"),
        ev("2026-08-01T10:00:00", 300),
    ]
    rows = it.income_events(events)
    assert [r["amount_cents"] for r in rows] == [300, 100]


def test_income_events_date_range():
    events = [ev(f"2026-0{m}-15T10:00:00", m * 100) for m in range(1, 10)]
    rows = it.income_events(events, since="2026-07-01", until="2026-08-31")
    assert [r["amount_cents"] for r in rows] == [700, 800]


# ─── 开票清单 ───────────────────────────────────────────
def test_invoice_draft_groups_by_counterparty():
    events = [
        ev("2026-08-01T10:00:00", 500000, category="接单", counterparty="甲公司"),
        ev("2026-08-05T10:00:00", 300000, category="服务", counterparty="甲公司"),
        ev("2026-08-06T10:00:00", 120000, category="设计", counterparty="乙工作室"),
    ]
    out = it.invoice_draft(events)
    assert out["client_count"] == 2
    assert out["income_total_cents"] == 920000
    by = {c["counterparty"]: c for c in out["clients"]}
    assert by["甲公司"]["amount_cents"] == 800000
    assert by["甲公司"]["count"] == 2
    assert by["乙工作室"]["amount_cents"] == 120000


def test_invoice_draft_unknown_counterparty_bucketed():
    events = [ev("2026-08-01T10:00:00", 1000, counterparty="")]
    out = it.invoice_draft(events)
    assert out["clients"][0]["counterparty"] == "未标注客户"


def test_invoice_draft_tax_item_candidates_not_confirmed():
    """税目只给候选，必须标未确认，不得冒充核定结论。"""
    events = [ev("2026-08-01T10:00:00", 1000, category="设计", counterparty="甲")]
    c = it.invoice_draft(events)["clients"][0]
    assert "文化创意服务" in c["tax_item_candidates"]
    assert c["tax_item_confirmed"] is False


def test_invoice_draft_invoice_status_unknown():
    """账本无开票状态字段，必须标 unknown，不得推断为已开票。"""
    events = [ev("2026-08-01T10:00:00", 1000, counterparty="甲")]
    assert it.invoice_draft(events)["clients"][0]["invoice_status"] == "unknown"


def test_invoice_draft_no_tax_split_without_rate():
    """未给征收率时不得做价税拆分，只能回账本原值。"""
    events = [ev("2026-08-01T10:00:00", 103000, counterparty="甲")]
    c = it.invoice_draft(events)["clients"][0]
    assert c["tax_rate"] is None
    assert c["amount_excl_tax_cents"] is None
    assert c["tax_amount_cents"] is None
    assert it.invoice_draft(events)["tax_basis"] == "unknown"


def test_invoice_draft_tax_split_with_rate():
    events = [ev("2026-08-01T10:00:00", 103000, counterparty="甲")]
    c = it.invoice_draft(events, tax_rate=0.03)["clients"][0]
    assert c["amount_incl_tax_cents"] == 103000
    assert c["amount_excl_tax_cents"] == 100000
    assert c["tax_amount_cents"] == 3000


def test_invoice_draft_counts_unconfirmed():
    events = [
        ev("2026-08-01T10:00:00", 1000, counterparty="甲", confirmed=True),
        ev("2026-08-02T10:00:00", 2000, counterparty="甲", confirmed=False),
    ]
    out = it.invoice_draft(events)
    assert out["unconfirmed_count"] == 1
    assert out["income_total_cents"] == 3000


def test_invoice_draft_ignores_expenses():
    events = [
        ev("2026-08-01T10:00:00", 1000, counterparty="甲"),
        ev("2026-08-02T10:00:00", 9999, counterparty="甲", type_="expense"),
    ]
    assert it.invoice_draft(events)["income_total_cents"] == 1000


# ─── 报税归集 ───────────────────────────────────────────
def test_tax_quarterly_groups():
    events = [
        ev("2026-07-10T10:00:00", 100000, category="接单"),
        ev("2026-08-10T10:00:00", 200000, category="接单"),
        ev("2026-08-20T10:00:00", 50000, category="设计"),
        ev("2026-04-10T10:00:00", 70000, category="接单"),
    ]
    out = it.tax_quarterly(events)
    q3 = out["quarters"][0]
    assert q3["quarter"] == "2026Q3"
    assert q3["income_total_cents"] == 350000
    assert q3["income_count"] == 3
    assert q3["period"] == {"start": "2026-07-01", "end": "2026-09-30"}
    assert out["available_quarters"] == ["2026Q3", "2026Q2"]
    # 按月分解
    assert {m["month"]: m["amount_cents"] for m in q3["by_month"]} == {
        "2026-07": 100000, "2026-08": 250000}


def test_tax_quarterly_specific_quarter():
    events = [
        ev("2026-07-10T10:00:00", 100000),
        ev("2026-04-10T10:00:00", 70000),
    ]
    out = it.tax_quarterly(events, quarter="2026Q2")
    assert len(out["quarters"]) == 1
    assert out["quarters"][0]["income_total_cents"] == 70000


def test_tax_quarterly_empty_ledger():
    out = it.tax_quarterly([])
    assert out["quarters"] == []
    assert out["available_quarters"] == []
    assert out["ok"] is True


def test_tax_quarterly_disclaims_tax_liability():
    """不得给应纳税额结论，免责声明必须在。"""
    out = it.tax_quarterly([ev("2026-07-10T10:00:00", 100000)])
    assert "不代表应纳税额" in out["disclaimer"]
    assert "不提供税务筹划建议" in out["disclaimer"]
