"""CashLens 后端 API（本地优先 · 真数据链路）

职责：给「真数据对话式工作台」前端提供账本写入与状态/预测读取，全部接 state_engine 真计算。
启动：cd backend && python3 -m uvicorn app.main:app --port 8001
说明：金额一律以分（整数）传输；前端做语义解析（规则/语音），后端做记账与状态（真引擎）。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .services import parser_rule
from .services.state_engine import EventLedger, calc, make_event, run_state

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CASH_DATA_DIR", str(REPO_ROOT / "data")))
LEDGER_PATH = DATA_DIR / "finance_events.jsonl"
DB_PATH = DATA_DIR / "proj.db"

app = FastAPI(title="CashLens API", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class EventIn(BaseModel):
    """一笔入账（amount_cents 为正整数；方向由 type 表达）。"""

    event_type: str
    amount_cents: int = Field(gt=0)
    evidence_kind: str = "voice"
    ts: str | None = None
    channel: str = "voice"
    category: str = ""
    counterparty: str = ""
    note: str = ""
    confirmed: bool = False


@app.get("/api/health")
def health():
    return {"ok": True, "api": "cashlens", "ledger": str(LEDGER_PATH)}


@app.post("/api/events")
def add_event(ev: EventIn):
    """写一笔到事件账本（dedupe 幂等），返回写结果 + 最新状态。"""
    ts = datetime.fromisoformat(ev.ts) if ev.ts else datetime.now()
    try:
        event = make_event(
            ts=ts, event_type=ev.event_type, amount_cents=ev.amount_cents,
            evidence_kind=ev.evidence_kind, channel=ev.channel, category=ev.category,
            counterparty=ev.counterparty, note=ev.note, confirmed=ev.confirmed,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    book = EventLedger(LEDGER_PATH)
    result = book.append(event, strict_dedupe=True)
    state = run_state(LEDGER_PATH, DB_PATH)["state"]
    return {"appended": result["appended"], "event": event, "state": state}


@app.get("/api/events")
def list_events(limit: int = 50):
    """最近事件（倒序展示用）。"""
    events = EventLedger(LEDGER_PATH).load()
    return {"total": len(events), "events": list(reversed(events[-limit:]))}


@app.get("/api/state")
def get_state():
    """当前财务状态（真计算：asOf 重放）。"""
    return run_state(LEDGER_PATH, DB_PATH)


@app.get("/api/forecast")
def get_forecast(horizon_days: int = 30):
    """未来现金流区间（90% 置信带，真计算）。"""
    events = EventLedger(LEDGER_PATH).load()
    state = calc.compute(events)
    fc = calc.forecast_cashflow(events, horizon_days=horizon_days)
    fc["confidence"] = state["cashflow_confidence"]
    fc["label"] = state["label"]
    return fc


class ChatIn(BaseModel):
    """用户的一句话（记账或问现金流）。"""

    text: str


def _yuan(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


@app.post("/api/chat")
def chat(body: ChatIn):
    """对话真链路：规则解析 → 事件账本 → 状态引擎真计算 → 回复文案。"""
    r = parser_rule.analyze(body.text)
    if r["kind"] == "fallback":
        return {"ok": False, "text": "这句我先接不上：说一句带金额的记账（如「昨天微信收了 3000 尾款」），或问「现金流怎么样 / 下个月会不会缺钱」。"}
    if r["kind"] == "ask_cashflow":
        events = EventLedger(LEDGER_PATH).load()
        st = calc.compute(events)
        fc = calc.forecast_cashflow(events, horizon_days=30)
        fc["confidence"] = st["cashflow_confidence"]
        if st["events_count"] == 0:
            return {"ok": True, "text": "账本还是空的——先记几笔（如「昨天微信收了 3000 尾款」），我才能照见你的现金流。", "state": st, "forecast": fc}
        low = fc["band90_low_cents"]
        gap_txt = f"最坏情形 {_yuan(-low)} 缺口" if low < 0 else "未见缺口"
        reply = (
            f"当前状态：{st['label']} · 健康度 {st['financial_health']:.2f} · 现金流可信度 {st['cashflow_confidence']:.2f}。"
            f"未来 30 天期末预计 {_yuan(fc['median_balance_cents'])}，区间 {_yuan(fc['band90_low_cents'])} ~ {_yuan(fc['band90_high_cents'])}（{gap_txt}）。"
        )
        return {"ok": True, "text": reply, "state": st, "forecast": fc}
    # kind == record：写入账本（真引擎记账）
    try:
        event = make_event(
            ts=datetime.now(), event_type=r["direction"], amount_cents=r["amount_cents"],
            evidence_kind="voice", channel=r["channel"], category=r["category"],
            counterparty="", note=r["text"], confirmed=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    book = EventLedger(LEDGER_PATH)
    result = book.append(event, strict_dedupe=True)
    state = run_state(LEDGER_PATH, DB_PATH)["state"]
    dir_cn = "收入" if r["direction"] == "income" else "支出"
    if not result["appended"]:
        return {"ok": True, "text": "这笔和账本里已有的记录重复了（dedupe 拦截），未重复入账。", "event": event, "state": state}
    reply = (
        f"好的，已记{dir_cn} {_yuan(r['amount_cents'])}（{r['category']} · {r['channel']}），"
        f"证据 0.75 主动记录 → 事件账本。当前状态：{state['label']}。"
    )
    return {"ok": True, "text": reply, "event": event, "state": state}
