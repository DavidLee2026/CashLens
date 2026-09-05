"""状态引擎 v0.2 最小版 —— golden case 与回归测试

依据：02-脑暴/财务状态引擎设计-v0.2定稿.md §九（golden case）与 v0.1 纪律：
- 相同事件 + 相同 asOf → 相同结果（可审计）
- 去重写入幂等；自报语句不直接改变财务状态
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.state_engine import EventLedger, compute, make_event, run_state  # noqa: E402
from app.services.state_engine.ledger import _dedupe_key_of  # noqa: E402

AS_OF = datetime(2026, 9, 5, 12, 0, 0)


def _sample_events():
    """§九 golden case 的 3 条事件（¥5000 flow 收入 / ¥1280 receipt 支出 / 自报语句）。"""
    expense = make_event(
        ts=AS_OF - timedelta(days=2), event_type="expense", amount_cents=128000,
        evidence_kind="receipt", channel="receipt", category="餐饮", confirmed=True,
    )
    income = make_event(
        ts=AS_OF - timedelta(days=1), event_type="income", amount_cents=500000,
        evidence_kind="flow", channel="wechat", category="接单",
    )
    statement = make_event(
        ts=AS_OF, event_type="adjustment", amount_cents=0,
        evidence_kind="self_report", note="我这月没问题",
    )
    return [expense, income, statement]


def test_golden_case_v02_section9():
    """§九 golden case：置信度>0、健康度被 flow 强化/receipt 弱化、自报几乎不改变、标签非 unknown 且非 stable。"""
    events = _sample_events()
    st = compute(events, as_of=AS_OF)

    assert st["cashflow_confidence"] > 0                       # 有 flow 收入证据
    assert st["financial_health"] > 0.5                        # 收入 flow 收敛后强于初值
    # 自报语句不参与更新：去掉自报后健康度不变（|Δ| = 0 < 0.05）
    without_stmt = compute([e for e in events if e["amount_cents"] > 0], as_of=AS_OF)
    assert abs(st["financial_health"] - without_stmt["financial_health"]) < 0.05
    assert st["label"] not in ("unknown", "stable")            # 有证据但收入次数 < 3 → learning
    assert st["events_count"] == 3


def test_health_replay_order_and_learning_rate():
    """健康度按 ts 顺序收敛重放：先支出(receipt)后收入(flow)。"""
    expense = make_event(ts=AS_OF - timedelta(days=2), event_type="expense", amount_cents=128000,
                         evidence_kind="receipt", channel="receipt", category="餐饮", confirmed=True)
    income = make_event(ts=AS_OF - timedelta(days=1), event_type="income", amount_cents=500000,
                        evidence_kind="flow", channel="wechat", category="接单")
    # 手算中间值：0.5 + 0.35*0.85*(0-0.5) = 0.35125；再 +0.35*1.0*(1-0.35125) ≈ 0.578
    st = compute([expense, income], as_of=AS_OF)
    assert st["financial_health"] == pytest.approx(0.5783, abs=0.002)
    # 只收入不支出 → 健康度更高
    st_income_only = compute([income], as_of=AS_OF)
    assert st_income_only["financial_health"] > st["financial_health"]


def test_unknown_and_stable_labels():
    """无收入证据 → unknown；多次 flow 且近期 → stable。"""
    no_income = compute([], as_of=AS_OF)
    assert no_income["label"] == "unknown"
    assert "diagnose" in no_income["actions"]

    stable_events = [
        make_event(ts=AS_OF - timedelta(days=30), event_type="income", amount_cents=300000,
                   evidence_kind="flow", channel="wechat"),
        make_event(ts=AS_OF - timedelta(days=20), event_type="income", amount_cents=250000,
                   evidence_kind="flow", channel="wechat"),
        make_event(ts=AS_OF - timedelta(days=2), event_type="income", amount_cents=200000,
                   evidence_kind="flow", channel="alipay"),
    ]
    st = compute(stable_events, as_of=AS_OF)
    assert st["label"] == "stable"
    assert st["flow_count"] == 3
    assert st["cashflow_confidence"] >= 0.7


def test_misconception_label_from_assumption_mismatch():
    """假设检验：期望 3 天回款、实际 21 天 → mismatch → 状态标签 misconception。"""
    asm = make_event(ts=AS_OF - timedelta(days=30), event_type="assumption", amount_cents=0,
                     evidence_kind="guess", extra={"assumption": {"subject": "receivable_cycle", "expected": "3d"}})
    res = make_event(ts=AS_OF, event_type="resolution", amount_cents=0, evidence_kind="guess",
                     extra={"assumption_ref": asm["event_id"], "actual": "21d"})
    st = compute([asm, res], as_of=AS_OF)
    assert st["label"] == "misconception"
    assert st["actions"] == ["repair"]


def test_dedupe_append_and_ledger_load():
    """同 dedupe_key 幂等：第二次 append 不写入；账本加载按 ts 排序。"""
    tmp = Path(__file__).parent / "_tmp_ledger_test.jsonl"
    if tmp.exists():
        tmp.unlink()
    book = EventLedger(tmp)
    ev1 = make_event(ts=AS_OF - timedelta(days=1), event_type="income", amount_cents=1000,
                     evidence_kind="voice", channel="wechat", category="接单")
    ev2 = make_event(ts=AS_OF - timedelta(days=1), event_type="income", amount_cents=1000,
                     evidence_kind="voice", channel="wechat", category="接单")  # 相同内容 → 相同 dedupe_key
    assert _dedupe_key_of(ev1) == _dedupe_key_of(ev2)
    assert book.append(ev1)["appended"] is True
    assert book.append(ev2, strict_dedupe=True)["appended"] is False
    loaded = book.load()
    assert len(loaded) == 1
    tmp.unlink(missing_ok=True)


def test_run_state_end_to_end_and_rebuild_idempotent(tmp_path):
    """端到端 run_state：首次重建 + 二次幂等（状态一致、无需重建）。"""
    ledger_path = tmp_path / "events.jsonl"
    db_path = tmp_path / "proj.db"
    book = EventLedger(ledger_path)
    for ev in _sample_events():
        book.append(ev)

    r1 = run_state(str(ledger_path), str(db_path), as_of=AS_OF)
    r2 = run_state(str(ledger_path), str(db_path), as_of=AS_OF)
    assert r1["rebuilt"] is True
    assert r2["rebuilt"] is False
    assert r1["state"] == r2["state"]
    assert len(r2["events"]) == 3
