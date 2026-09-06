"""CashLens 后端 API（本地优先 · 真数据链路）

职责：给「真数据对话式工作台」前端提供账本写入与状态/预测读取，全部接 state_engine 真计算。
启动：cd backend && python3 -m uvicorn app.main:app --port 8001
说明：金额一律以分（整数）传输；前端做语义解析（规则/语音），后端做记账与状态（真引擎）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .services import llm_skill, parser_rule, query_tools
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


def _ctx_summary() -> str:
    """给 LLM 的当前系统上下文（只参考不照抄数值）。"""
    try:
        events = EventLedger(LEDGER_PATH).load()
        st = calc.compute(events)
        fc = calc.forecast_cashflow(events, horizon_days=30)
        return json.dumps({"state": st, "forecast": fc, "events_count": len(events)}, ensure_ascii=False)
    except Exception:
        return "{}"


def _cashflow_text() -> dict:
    """真计算现金流回复（LLM 与规则共用，杜绝模型编数字）。"""
    events = EventLedger(LEDGER_PATH).load()
    return query_tools.cashflow_text(events)


def _record_one(direction: str, amount_cents: int, channel: str, category: str, note: str, counterparty: str = ""):
    """写一笔到事件账本；返回 (event, appended)。"""
    ev = make_event(
        ts=datetime.now(), event_type=direction, amount_cents=amount_cents,
        evidence_kind="voice", channel=channel or "manual", category=category or "其他",
        counterparty=counterparty or "", note=note or "", confirmed=False,
    )
    book = EventLedger(LEDGER_PATH)
    res = book.append(ev, strict_dedupe=True)
    return ev, res["appended"]


def _rule_reply(text: str) -> dict:
    """规则兜底（原逻辑）：记账 / 问现金流 / 接不上。"""
    r = parser_rule.analyze(text)
    if r["kind"] == "fallback":
        return {"ok": False, "text": "这句我先接不上：说一句带金额的记账（如「昨天微信收了 3000 尾款」），或问「最近一笔收入 / 这个月花了多少 / 现金流怎么样」。配置 LLM API Key 后可自由对话。"}
    if r["kind"] == "ask_cashflow":
        out = _cashflow_text()
        return {"ok": True, "text": out["text"], "state": out.get("state"), "forecast": out.get("forecast")}
    try:
        ev, appended = _record_one(r["direction"], r["amount_cents"], r["channel"], r["category"], r["text"])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    state = run_state(LEDGER_PATH, DB_PATH)["state"]
    dir_cn = "收入" if r["direction"] == "income" else "支出"
    if not appended:
        return {"ok": True, "text": "这笔和账本里已有的记录重复了（dedupe 拦截），未重复入账。", "event": ev, "state": state}
    return {"ok": True, "text": f"好的，已记{dir_cn} {_yuan(r['amount_cents'])}（{r['category']} · {r['channel']}），证据 0.75 主动记录 → 事件账本。当前状态：{state['label']}。", "event": ev, "state": state}


def _apply_llm(parsed: dict, text: str) -> dict:
    """LLM 主解析结果落地：记账 / 真实数据查询 / 纯聊天。

    纪律：凡执行了 record 或 ask_*（真数据动作），回复只采用后端真数据文案
    （不拼接 LLM 的 reply，防止它编造数值或承诺不存在的功能）；纯聊天才用 LLM reply。
    """
    events = EventLedger(LEDGER_PATH).load()
    handlers = {
        "ask_cashflow": lambda: query_tools.cashflow_text(events),
        "ask_latest_income": lambda: {"text": query_tools.latest_event_text(events, "income")},
        "ask_latest_expense": lambda: {"text": query_tools.latest_event_text(events, "expense")},
        "ask_spending": lambda: {"text": query_tools.spending_month_text(events)},
        "ask_recent": lambda: {"text": query_tools.recent_events_text(events)},
    }
    parts: list[str] = []
    any_record = False
    any_data = False
    for a in parsed.get("actions", []):
        kind = a.get("kind")
        if kind == "record":
            try:
                amount = int(a.get("amount_cents") or 0)
                direction = a.get("direction")
                if amount <= 0 or direction not in ("income", "expense"):
                    continue
            except (TypeError, ValueError):
                continue
            any_record = True
            try:
                ev, appended = _record_one(
                    direction, amount, a.get("channel"), a.get("category"),
                    a.get("note") or text, a.get("counterparty", ""),
                )
            except ValueError:
                continue
            dir_cn = "收入" if direction == "income" else "支出"
            ch = ev.get("channel") or "manual"
            parts.append(
                f"好的，已记{dir_cn} {_yuan(amount)}（{ev.get('category') or '其他'} · {ch}）"
                if appended else f"{_yuan(amount)} 与账本重复（dedupe 拦截），未重复入账。"
            )
        elif kind in handlers:
            any_data = True
            parts.append(handlers[kind]()["text"])
    body: dict = {"ok": True}
    if any_record or any_data:
        body["text"] = "\n".join(parts)
    else:
        extra = str(parsed.get("reply", "")).strip()
        body["text"] = extra or "我还在学习这句怎么接——可以记一笔账，或问「最近一笔收入 / 这个月花了多少 / 现金流怎么样」。"
    if any_record:
        try:
            body["state"] = run_state(LEDGER_PATH, DB_PATH)["state"]
        except Exception:
            pass
    return body


@app.get("/api/capabilities")
def capabilities():
    on = llm_skill.is_configured()
    return {"llm": on, "model": (llm_skill._model() if on else None)}


@app.post("/api/chat")
def chat(body: ChatIn):
    """对话真链路：LLM 主解析（如配置）→ 规则兜底；金额与状态永远本地真算。"""
    text = body.text.strip()
    if llm_skill.is_configured():
        parsed = llm_skill.parse_accounting(text, context=_ctx_summary())
        if parsed is not None:
            return _apply_llm(parsed, text)
    return _rule_reply(text)
