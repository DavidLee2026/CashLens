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


# ─── 用例 A：报价低于自己的历史时薪（2026-10-29 由「接」改判「不接」）────
def test_golden_a_quote_below_own_history_should_reject():
    """¥3,000 / 20h = ¥150/时，低于他自己 ¥210/时 的历史 → 不接。

    **这个用例原来断言「接」**，因为旧算式把历史时薪算成 ¥105.77/时
    （`(6600+4400)/2 ÷ 52h` —— 分子是「一单多少钱」、分母是「所有单的小时之和」，
    两个量不在同一维度）。按逐单时薪的中位数，这两单分别是 ¥220/时 与 ¥200/时，
    历史是 ¥210/时；¥150 是降薪 29%。**判「不接」才是产品该做的事**。

    这不是为了让新代码通过而改断言 —— 改判之后用例覆盖的是更值钱的那条路：
    「报价被自己压低，还被判成赚」。原断言恰好把这个坑挡住了。
    """
    events = _ledger_a()
    out = decide(events, {"amount_cents": 300000, "estimated_hours": 20}, as_of=AS_OF)
    hourly = out["hourly"]
    assert hourly["historical_cents"] == 21000, (
        "历史时薪 = 逐单时薪 ¥220 / ¥200 的中位数 = ¥210/时"
        "（旧式「中位数 5500 ÷ 52 小时」= ¥105.77 把两个量混在一起算，n 越多越低）")
    assert hourly["this_order_cents"] == 15000
    assert hourly["sample_orders"] == 2
    assert out["decision"] == "不接", "等效时薪 150 < 历史 210 × 0.8 = 168"
    assert any("等效时薪" in r["text"] for r in out["reasons"]), "理由里要说清是谁在跟谁比"
    assert all(r.get("evidence") for r in out["reasons"]), "每条理由都要能溯源"


# ─── 用例 A2：正常接单（应判「接」）──────────────────────────
def test_golden_a2_normal_order_above_history_should_accept():
    """¥5,000 / 20h = ¥250/时 > 历史 ¥210/时 → 接。保留「接」这条路径的覆盖。"""
    out = decide(_ledger_a(), {"amount_cents": 500000, "estimated_hours": 20}, as_of=AS_OF)
    assert out["hourly"]["historical_cents"] == 21000
    assert out["hourly"]["this_order_cents"] == 25000
    assert out["decision"] == "接"
    assert {c["name"]: c["status"] for c in out["checks"]}["after_tax"] == "satisfied"
    assert len(out["reasons"]) >= 2
    ledger_reasons = [r for r in out["reasons"] if r["evidence"].get("event_ids")]
    assert ledger_reasons and all(
        eid for r in ledger_reasons for eid in r["evidence"]["event_ids"]
    ), "账本类理由必须带 event_id"


# ─── 用例 B：低于时薪（应判「不接」或提价）────────────────────
def test_golden_b_should_reject_and_quote_floor():
    out = decide(_ledger_a(), {"amount_cents": 120000, "estimated_hours": 20}, as_of=AS_OF)
    assert out["hourly"]["this_order_cents"] == 6000
    assert out["decision"] == "不接", "¥60/时 < 210 × 0.8 = 168"
    assert out["min_price_cents"] > 120000, "报价下限必须高于客户报价"
    assert out["quote"]["floor_cents"] == 21000 * 20
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
    assert checks["after_tax"] == "missing", "税后 ¥96/时 < 历史 ¥210/时"
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


# ─── 历史时薪的口径（2026-10-29 回归：必须是「逐单时薪」的中位数）──────
def test_hourly_is_median_of_per_order_rates_when_n_gt_1():
    """n>1 时取「逐单时薪」的中位数，不是「各单金额中位数 ÷ 总工时」。

    三单：¥600/16h（¥37.50）· ¥800/56h（¥14.29）· ¥2000/40h（¥50.00）
      旧式：中位数 ¥800 ÷ 112h = **¥7.14/时**（错；真实混合口径 ¥30.36，偏小 4.25 倍）
      正解：逐单中位数 = **¥37.50/时**

    为什么必须咬死这个数：历史时薪一旦被算小，`decision_check` 的第三道闸门
    会把任何报价都判「达标」，报价下限也会低到没有意义 —— 产品反过来鼓励接亏本单。
    """
    events = [
        _income(60000, 30, hours=16),
        _income(80000, 20, hours=56),
        _income(200000, 10, hours=40),
    ]
    hh = historical_hourly(events, AS_OF)
    assert hh["sample_orders"] == 3
    assert hh["hours_total"] == 112
    assert hh["hourly_cents"] == 3750, "¥37.50/时 —— 逐单时薪的中位数，不是 ¥7.14"
    assert "37.50" in hh["reason"] and "中位数" in hh["reason"], "口径要写在理由里，用户能看见"


def test_one_huge_order_does_not_drag_hourly():
    """抗离群：一单天价不该把历史时薪拉飞（这才是「取中位数」要防的事）。"""
    events = [
        _income(50000, 30, hours=10),     # ¥50/时
        _income(60000, 20, hours=12),     # ¥50/时
        _income(1000000, 10, hours=10),   # ¥1000/时，离群
    ]
    hh = historical_hourly(events, AS_OF)
    assert hh["hourly_cents"] == 5000, "中位数仍是 ¥50/时，没被那单 ¥1000/时 带走"


# ─── A 档口径（2026-10-29）：工时挂在项目上，时薪按项目算 ────────────
def _income_p(amount_cents, days_ago, project):
    return make_event(
        ts=AS_OF - timedelta(days=days_ago), event_type="income", amount_cents=amount_cents,
        evidence_kind="flow", channel="wechat", category="接单", project=project,
    )


def test_project_hours_win_over_event_hours():
    """项目填了工时 → 按「项目收入 ÷ 项目工时」算，不看事件上的 hours。"""
    events = [_income_p(80000, 10, "project_a"), _income_p(60000, 40, "project_b")]
    rows = [{"id": "project_a", "name": "详情页", "hours": 80},
            {"id": "project_b", "name": "海报", "hours": 16}]
    hh = historical_hourly(events, AS_OF, projects=rows)
    assert hh["basis"] == "projects"
    assert hh["sample_orders"] == 2
    # ¥800/80h = ¥10/时；¥600/16h = ¥37.50/时 → 中位数 ¥23.75/时
    assert hh["hourly_cents"] == 2375


def test_deposit_and_final_payment_count_as_one_project():
    """定金 + 尾款属于同一个活：收入合并后 ÷ 一次工时，不是各算各的。"""
    events = [_income_p(240000, 40, "p1"), _income_p(760000, 10, "p1")]
    hh = historical_hourly(events, AS_OF, projects=[{"id": "p1", "hours": 100}])
    assert hh["sample_orders"] == 1
    assert hh["income_total_cents"] == 1000000
    assert hh["hourly_cents"] == 10000, "¥10,000 ÷ 100h = ¥100/时"


def test_project_window_judged_by_last_event():
    """定金在 3 个月窗口外、尾款在窗口内 → 项目仍算在窗口内，收入按**全额**。

    否则分母是整单工时、分子只剩一半，时薪被削掉一半 —— 比不算更糟。
    """
    events = [_income_p(200000, 120, "p1"), _income_p(800000, 10, "p1")]
    hh = historical_hourly(events, AS_OF, projects=[{"id": "p1", "hours": 100}])
    assert hh["sample_orders"] == 1
    assert hh["income_total_cents"] == 1000000
    assert hh["hourly_cents"] == 10000


def test_no_project_hours_falls_back_to_event_hours():
    """项目没填工时就回退事件口径 —— 新口径不能把老数据变成「算不出」。"""
    events = [_income(60000, 30, hours=16)]
    hh = historical_hourly(events, AS_OF, projects=[{"id": "project_a"}])
    assert hh["basis"] == "ledger"
    assert hh["hourly_cents"] == 3750


def test_decide_uses_project_hours():
    """整条链路：决定用项目工时算出的历史时薪，¥10/时 判「不接」。"""
    events = [_income_p(80000, 10, "p1"), _income_p(60000, 40, "p2")]
    rows = [{"id": "p1", "hours": 80}, {"id": "p2", "hours": 16}]
    out = decide(events, {"amount_cents": 80000, "estimated_hours": 80},
                 as_of=AS_OF, projects=rows)
    assert out["hourly"]["historical_cents"] == 2375
    assert "个填了工时的项目" in out["hourly"]["basis"], "理由里要说清这个基准是按项目算的"
    assert out["decision"] == "不接", "这单 ¥10/时，只有历史 ¥23.75/时 的 42%"


def test_low_history_makes_cheap_order_look_bad_not_good():
    """反向断言：历史时薪算对之后，¥10/时 的报价必须判「不接」，而不是「接」。

    同一组历史（逐单 ¥37.50 / ¥14.29 / ¥50.00）：
      旧式 历史 = ¥7.14/时 → ¥800 / 80h = ¥10/时 看着像比基准高一截 → 判「接」
      正解 历史 = ¥37.50/时 → ¥10/时 是基准的 27% → 判「不接」
    「80 张详情页收 ¥800、做了 10 天」那一单就是 ¥10/时。
    """
    events = [
        _income(60000, 30, hours=16),
        _income(80000, 20, hours=56),
        _income(200000, 10, hours=40),
    ]
    out = decide(events, {"amount_cents": 80000, "estimated_hours": 80}, as_of=AS_OF)
    assert out["hourly"]["historical_cents"] == 3750, "历史 ¥37.50/时"
    assert out["hourly"]["this_order_cents"] == 1000, "这单 ¥10/时"
    assert out["decision"] == "不接", "¥10/时 只有基准的 27%，必须判不接"


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
    """「税前看着达标，税后并没有」只在税前真达标时才说 —— 否则是误导（自查发现的措辞 bug）。

    2026-10-29 随口径订正：历史时薪由 ¥105.77 改为 ¥210/时，原来那笔「税前 ¥120」已经
    不再算达标，所以「陷阱句」的用例改用 ¥240/时（税后 ¥192，仍低于 ¥210）。
    """
    # 税前 ¥240 达标（≥210），税后 ¥192 不达标 → 这句话成立
    trap = decision_check(_ledger_a(), {"amount_cents": 480000, "estimated_hours": 20,
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
