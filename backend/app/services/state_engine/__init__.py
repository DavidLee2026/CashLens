"""状态引擎（State Engine）——记忆层核心包

入口：run_state() 一把跑完「账本 → 投影 → asOf 计算」。
实现依据：02-脑暴/财务状态引擎设计-v0.2定稿.md（2026-09-05 施工定稿，推荐值施工、待实测校准）。
"""

from datetime import datetime

from .calc import compute, forecast_cashflow
from .ledger import EventLedger, make_event
from .store import Projection

__all__ = ["EventLedger", "Projection", "make_event", "run_state", "compute", "forecast_cashflow"]


def run_state(ledger_path: str, db_path: str, as_of=None):
    """端到端：读账本（去重、按 ts 排序）→ 重建 SQLite 投影 → 计算状态。

    返回 {state, events, assumptions, rebuilt}。
    """
    book = EventLedger(ledger_path)
    events = book.load()
    proj = Projection(db_path)
    rebuilt = proj.needs_rebuild()
    if rebuilt:
        proj.rebuild(events)
    as_of = as_of or datetime.now()
    state = compute(events, as_of)
    as_of_str = as_of.isoformat(timespec="seconds") if not isinstance(as_of, str) else as_of
    proj.upsert_state(as_of_str, state)
    assumptions = proj.assumptions_status()
    proj.close()
    return {"state": state, "events": events, "assumptions": assumptions, "rebuilt": rebuilt}
