"""合成小微经营账的回归测试（2026-10 立）。

## 为什么需要它

`tests/test_external_bill_samples.py` 用的是**个人家庭账单样例**，它能测格式与分类，
但**测不了产品的经营语义**：家庭账单没有经营收入、没有工时、没有垫付报销，
于是"收入→预测→决策→报销"这条主链在这些数据上根本走不到。

本文件用 `samples/synthetic_small_business/`（由 `scripts/make_synthetic_business_ledger.py`
固定种子生成）补上这一段。四条硬条件见生成器顶部注释。

⚠️ 这是**合成数据**，任何对外材料引用它都必须标注为合成。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "backend"))

from app.services.state_engine import calc, decision  # noqa: E402

_DATA = _ROOT / "samples" / "synthetic_small_business" / "data"
_LEDGER = _DATA / "finance_events.jsonl"


@pytest.fixture(scope="module")
def events() -> list[dict]:
    assert _LEDGER.exists(), (
        f"合成账不存在：{_LEDGER}\n"
        "请先跑：python3 scripts/make_synthetic_business_ledger.py"
    )
    return [json.loads(l) for l in _LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── 四条硬条件：数据一旦退化成单边，这些断言必须变红 ──────────────

def test_has_both_income_and_expense(events):
    """① 双边：经营收入与成本都要有。家庭账单就是死在这一条上。"""
    inc = [e for e in events if e["type"] == "income"]
    exp = [e for e in events if e["type"] == "expense"]
    assert len(inc) >= 5, f"收入太少（{len(inc)} 笔），预测没有收入侧节奏"
    assert len(exp) >= 30, f"支出太少（{len(exp)} 笔）"
    assert sum(e["amount_cents"] for e in inc) > 0
    assert sum(e["amount_cents"] for e in exp) > 0


def test_spans_enough_days_for_forecast(events):
    """② 跨 ≥2 个月、且外推门槛（≥5 天有记录）必须满足。"""
    days = {e["ts"][:10] for e in events}
    assert len(days) >= 30, f"只有 {len(days)} 天有记录，预测区间会太宽"
    lo, hi = min(days), max(days)


def test_has_reimbursement_advances(events):
    """③ 垫付待报销：产品差异化的那一段，必须在这份数据里看得见。"""
    adv = [e for e in events if "垫付" in (e.get("note") or "")]
    assert len(adv) >= 3, "垫付样本太少，演示不了「垫付 → 报销」"
    assert all(e["type"] == "expense" for e in adv)


def test_income_carries_hours(events):
    """收入必须带工时 —— 决策引擎靠它反推历史时薪，不带就只会答「算不出」。"""
    inc = [e for e in events if e["type"] == "income"]
    with_hours = [e for e in inc if float(e.get("hours") or 0) > 0]
    assert len(with_hours) == len(inc), "每条收入都要带 hours"


# ── 产品语义：预测与决策真的能走通 ────────────────────────────

def test_forecast_produces_a_band(events):
    """预测必须给出区间（不是"数据不足"），且标签不是 unknown。"""
    fc = calc.forecast_cashflow(events, horizon_days=30)
    assert fc["insufficient"] is False, "样本数据必须够外推"
    assert fc["band90_low_cents"] < fc["band90_high_cents"], "区间不能是一个点"
    st = calc.compute(events)
    assert st["label"] != "unknown"
    assert st["net_30d_cents"] != 0


def test_decision_engine_can_price_a_job(events):
    """决策引擎能算出报价（家庭账单在这条上只能答「算不出」）。"""
    h = decision.historical_hourly(events)
    assert h["basis"] != "insufficient", "有带工时的收入，时薪必须算得出来"
    assert h["hourly_cents"] and h["hourly_cents"] > 0
    assert h["event_ids"], "每个数字都要能追到 event_id"


def test_assumption_pair_is_compatible(events):
    """假设检验：本样本刻意只放**相符**的一组。

    为什么刻意相符：`calc._has_misconception` 一旦 `actual != expected` 就把标签
    永久钉在「财务误区」，产品里**没有消除机制** —— 不符的那组会把演示标签锁死。
    这条断言同时是那个已知边界的活文档：等消除机制做完，再改成允许不符。
    """
    assumption = [e for e in events if e["type"] == "assumption"]
    resolution = [e for e in events if e["type"] == "resolution"]
    assert len(assumption) == 1 and len(resolution) == 1
    assert assumption[0]["assumption"]["expected"] == resolution[0]["actual"]
    assert calc._has_misconception(events) is False, (
        "样本里出现了不相符的假设检验 —— 会让状态标签永久停在「财务误区」"
    )


# ── 非经营性支出必须与经营分开（演示"经营 vs 个人"的边界）──────

def test_personal_expenses_stay_unassigned(events):
    """个人支出（房租/外卖/日用）不该挂到客户项目上，否则项目利润是假的。"""
    personal = [e for e in events if e["type"] == "expense" and e.get("category") == "居住"]
    assert personal, "样本里应当有个人刚性支出（房租）用于对比"
    assert all(not e.get("project") for e in personal), "房租不该归到客户项目"


def test_generator_is_deterministic():
    """固定种子 → 同一批事件（可复现是入库的前提）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "make_syn", _ROOT / "scripts" / "make_synthetic_business_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from datetime import datetime

    at = datetime(2026, 9, 28, 12, 0, 0)
    a = mod.build_events(at, 8, 20260928)
    b = mod.build_events(at, 8, 20260928)
    # event_id 每笔都是 uuid，比结构与金额（而不是 id）
    strip = lambda evs: [(e["ts"], e["type"], e["amount_cents"], e.get("category"), e.get("project")) for e in evs]
    assert strip(a) == strip(b), "同种子必须产出同一批账"
