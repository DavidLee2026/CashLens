"""决策引擎最小版 —— golden case 与回归测试

依据：`00-总览/决策引擎最小版-问题分析与golden-case-20260912.md` §六（用例 A-F）与 §六·6.7（确定性）。
纪律：先写断言、再让实现满足；**缺数据必须如实说 insufficient，不许造默认时薪**。
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.state_engine import make_event  # noqa: E402
from app.services.state_engine.decision import (  # noqa: E402
    decide,
    decision_check,
    historical_hourly,
    quote_range,
)

AS_OF = datetime(2026, 9, 12, 12, 0, 0)


def _income(amount_cents, days_ago, hours=None, counterparty="", evidence="flow"):
    extra = {"hours": hours} if hours is not None else None
    return make_event(
        ts=AS_OF - timedelta(days=days_ago), event_type="income", amount_cents=amount_cents,
        evidence_kind=evidence, channel="wechat", category="接单",
        counterparty=counterparty, extra=extra,
    )


def _expense(amount_cents, days_ago):
    return make_event(
        ts=AS_OF - timedelta(days=days_ago), event_type="expense", amount_cents=amount_cents,
        evidence_kind="receipt", channel="receipt", category="房租", confirmed=True,
    )


def _ledger_a():
    """用例 A 的账本：两笔带工时的接单收入 + 一笔房租。"""
    return [
        _income(660000, 40, hours=30, counterparty="某教育公司"),
        _income(440000, 20, hours=22, counterparty="某教育公司"),
        _expense(150000, 10),
    ]


# ─── 用例 A：正常接单（应判「接」）───────────────────────────
def test_golden_a_should_accept():
    events = _ledger_a()
    out = decide(events, {"amount_cents": 300000, "estimated_hours": 20}, as_of=AS_OF)
    hourly = out["hourly"]
    assert hourly["historical_cents"] == 10577, "历史时薪 = 中位数 5500 ÷ 52 小时 ≈ ¥105.77/时"
    assert hourly["this_order_cents"] == 15000
    assert hourly["sample_orders"] == 2
    assert out["decision"] == "接", "等效时薪 150 ≥ 历史 105.77，且现金流只是 uncertain（不降级）"
    assert {c["name"]: c["status"] for c in out["checks"]}["after_tax"] == "satisfied"
    assert len(out["reasons"]) >= 2
    assert all(r.get("evidence") for r in out["reasons"]), "每条理由都要能溯源"
    ledger_reasons = [r for r in out["reasons"] if r["evidence"].get("event_ids")]
    assert ledger_reasons and all(
        eid for r in ledger_reasons for eid in r["evidence"]["event_ids"]
    ), "账本类理由必须带 event_id"


# ─── 用例 B：低于时薪（应判「不接」或提价）────────────────────
def test_golden_b_should_reject_and_quote_floor():
    out = decide(_ledger_a(), {"amount_cents": 120000, "estimated_hours": 20}, as_of=AS_OF)
    assert out["hourly"]["this_order_cents"] == 6000
    assert out["decision"] == "不接", "¥60/时 < 105.77 × 0.8 = 84.6"
    assert out["min_price_cents"] > 120000, "报价下限必须高于客户报价"
    assert out["quote"]["floor_cents"] == 10577 * 20
    assert out["quote"]["recommend_cents"] > out["quote"]["floor_cents"]
    assert out["quote"]["ceiling_cents"] > out["quote"]["recommend_cents"]
    assert "等效时薪" in out["headline"]
    assert any("报价下限" in r["text"] for r in out["reasons"])


# ─── 用例 C：税后口径（验收 after_tax 真的生效）───────────────
def test_golden_c_after_tax_blocks_accept():
    out = decide(_ledger_a(), {"amount_cents": 240000, "estimated_hours": 20, "tax_rate": 0.20},
                 as_of=AS_OF)
    assert out["hourly"]["this_order_cents"] == 12000, "税前 ¥120/时，看起来达标"
    checks = {c["name"]: c["status"] for c in out["checks"]}
    assert checks["after_tax"] == "missing", "税后 ¥96/时 < 历史 ¥105.77/时"
    assert out["decision"] != "接", "税前达标也不能判「接」——这是防「假装达标」的关键断言"


# ─── 用例 D：现金流垫底（验收 cash_runway）────────────────────
def test_golden_d_cash_runway_downgrades():
    events = [_expense(200000, d) for d in (50, 44, 38, 30, 22, 15, 8)]
    out = decide(events, {"amount_cents": 900000, "estimated_hours": 40}, as_of=AS_OF)
    checks = {c["name"]: c["status"] for c in out["checks"]}
    assert checks["cash_runway"] in ("missing", "uncertain")
    assert out["decision"] == "再考虑"
    cash_reason = {c["name"]: c["reason"] for c in out["checks"]}["cash_runway"]
    assert any(cash_reason in r["text"] for r in out["reasons"]), \
        "现金流闸门的结论必须出现在理由里（不能只躺在结构化字段里，用户看不到）"
    assert "当前余额" in json.dumps(out["checks"], ensure_ascii=False), "引用预测必须标注代理口径"


# ─── 用例 E：回款周期纠偏（验收 misconception）────────────────
def test_golden_e_receivable_misconception_downgrades():
    events = _ledger_a()
    assumption = make_event(
        ts=AS_OF - timedelta(days=12), event_type="assumption", amount_cents=0,
        evidence_kind="self_report",
        extra={"assumption": {"subject": "receivable_cycle", "expected": "3d"}},
    )
    resolution = make_event(
        ts=AS_OF - timedelta(days=6), event_type="resolution", amount_cents=0,
        evidence_kind="self_report",
        extra={"assumption_ref": assumption["event_id"], "actual": "21d"},
    )
    events += [assumption, resolution]
    out = decide(events, {"amount_cents": 300000, "estimated_hours": 20,
                          "counterparty": "某教育公司"}, as_of=AS_OF)
    checks = {c["name"]: c for c in out["checks"]}
    assert checks["receivable_cycle"]["status"] == "misconception"
    assert "21d" in checks["receivable_cycle"]["reason"] and "3d" in checks["receivable_cycle"]["reason"]
    assert checks["receivable_cycle"]["evidence"]["event_ids"], "偏差理由要能追到那两条事件"
    assert out["decision"] != "接", "有回款周期偏差证据时至少降一档"


# ─── 用例 F：数据不足（如实标注，不硬编）──────────────────────
def test_golden_f_empty_ledger_is_honest():
    out = decide([], {"amount_cents": 300000, "estimated_hours": 20}, as_of=AS_OF)
    assert out["hourly"]["historical_cents"] is None
    assert out["hourly"]["basis"].find("算不出") >= 0
    assert out["decision"] == "再考虑"
    assert "工时" in out["headline"], "要明说缺的是工时记录，并引导补数据"
    assert out["min_price_cents"] is None, "没有历史时薪就不许给出报价下限"
    assert out["quote"]["floor_cents"] is None


def test_income_without_hours_does_not_count():
    """只有金额、没有工时的收入单，不能参与历史时薪（否则等于偷偷用估算值）。"""
    events = [_income(300000, 10), _income(600000, 20)]
    hh = historical_hourly(events, AS_OF)
    assert hh["hourly_cents"] is None
    assert hh["basis"] == "insufficient"


def test_self_report_income_never_enters_history():
    """自报（权重 0.10）只作先验，不得进历史时薪分子。"""
    events = [_income(900000, 10, hours=10, evidence="self_report")]
    hh = historical_hourly(events, AS_OF)
    assert hh["hourly_cents"] is None
    assert hh["sample_orders"] == 0


# ─── 用例 G：确定性（同事件 + 同 asOf → 同结果）────────────────
def test_determinism_same_input_same_output():
    events = _ledger_a()
    order = {"amount_cents": 300000, "estimated_hours": 20}
    a = json.dumps(decide(events, order, as_of=AS_OF), ensure_ascii=False, sort_keys=True)
    b = json.dumps(decide(events, order, as_of=AS_OF), ensure_ascii=False, sort_keys=True)
    assert a == b


def test_order_before_as_of_window_is_ignored():
    """回看窗口外的收入不参与历史时薪（3 个月窗口）。"""
    old = _income(900000, 200, hours=10)      # 200 天前，在 90 天窗口外
    recent = _income(300000, 10, hours=10)
    hh = historical_hourly([old, recent], AS_OF)
    assert hh["sample_orders"] == 1
    assert hh["hourly_cents"] == 30000, "只算窗口内那一笔：30 万分 ÷ 10 小时 = ¥300/时"


def test_missing_hours_reported_by_gate_not_by_guessing():
    """没给预估工时时，after_tax 如实报 uncertain，而不是拿别的东西凑。"""
    checks = decision_check(_ledger_a(), {"amount_cents": 300000}, as_of=AS_OF)
    after_tax = {c["name"]: c for c in checks}["after_tax"]
    assert after_tax["status"] == "uncertain"
    assert "工时" in after_tax["reason"]


def test_after_tax_wording_only_when_pretax_actually_passed():
    """「税前看着达标，税后并没有」只在税前真达标时才说 —— 否则是误导（自查发现的措辞 bug）。"""
    # 税前 ¥120 达标（≥105.77），税后 ¥96 不达标 → 这句话成立
    trap = decision_check(_ledger_a(), {"amount_cents": 240000, "estimated_hours": 20,
                                        "tax_rate": 0.20}, as_of=AS_OF)
    assert "税前看着达标" in {c["name"]: c for c in trap}["after_tax"]["reason"]
    # 税前 ¥60 本来就不达标 → 不许说这句话
    plain = decision_check(_ledger_a(), {"amount_cents": 120000, "estimated_hours": 20},
                           as_of=AS_OF)
    assert "税前看着达标" not in {c["name"]: c for c in plain}["after_tax"]["reason"]


def test_quote_range_always_labels_markup_as_experience_coefficient():
    """推荐/上限的系数必须如实标注为经验系数（无文献出处）。"""
    q = quote_range({"amount_cents": 300000, "estimated_hours": 20},
                    {"hourly_cents": 10000}, as_of=AS_OF)
    assert q["floor_cents"] == 200000
    assert "经验系数" in q["breakdown"]["markup_note"]
    assert "无文献出处" in q["breakdown"]["markup_note"]
