"""CashLens 后端 API（本地优先 · 真数据链路）

职责：给「真数据对话式工作台」前端提供账本写入与状态/预测读取，全部接 state_engine 真计算。
启动：cd backend && python3 -m uvicorn app.main:app --port 8001
说明：金额一律以分（整数）传输；前端做语义解析（规则/语音），后端做记账与状态（真引擎）。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .services import llm_skill, parser_rule, pending, query_tools, session_mem
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
    """用户的一句话（记账/查询/闲聊）+ 可选会话 id（多轮自由对话）。"""

    text: str
    session_id: str | None = None


def _yuan(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


def _events() -> list[dict]:
    return EventLedger(LEDGER_PATH).load()


def _cashflow_text() -> dict:
    """真计算现金流回复（LLM 与规则共用，杜绝模型编数字）。"""
    return query_tools.cashflow_text(_events())


def _append_event(direction: str, amount_cents: int, category: str, channel: str, note: str, counterparty: str = ""):
    """真正写入事件账本（确认后调用）。返回 (event, appended)。"""
    ev = make_event(
        ts=datetime.now(), event_type=direction, amount_cents=amount_cents,
        evidence_kind="voice", channel=channel or "manual", category=category or "其他",
        counterparty=counterparty or "", note=note or "", confirmed=True,
    )
    book = EventLedger(LEDGER_PATH)
    res = book.append(ev, strict_dedupe=True)
    return ev, res["appended"]


def _draft_one(direction: str, amount_cents: int, category: str, channel: str, note: str, counterparty: str = "") -> dict:
    """识别 → 只生成待确认草稿（不直接入账）。"""
    return pending.create(
        DATA_DIR, direction=direction, amount_cents=amount_cents,
        category=category, channel=channel, note=note, counterparty=counterparty,
    )


def _rule_reply(text: str) -> dict:
    """规则兜底（无 LLM 时）：记账走待确认 / 问现金流 / 接不上。"""
    r = parser_rule.analyze(text)
    if r["kind"] == "fallback":
        return {"ok": False, "text": "这句我先接不上：说一句带金额的记账（如「昨天微信收了 3000 尾款」），或问「最近一笔收入 / 这个月花了多少 / 现金流怎么样」。配置 LLM API Key 后可自由对话。"}
    if r["kind"] == "ask_cashflow":
        out = _cashflow_text()
        return {"ok": True, "text": out["text"], "state": out.get("state"), "forecast": out.get("forecast")}
    d = _draft_one(r["direction"], r["amount_cents"], r["category"], r["channel"], r["text"])
    dir_cn = "收入" if r["direction"] == "income" else "支出"
    return {
        "ok": True,
        "text": f"识别到{dir_cn} {_yuan(r['amount_cents'])}（{r['category']} · {r['channel']}）——请确认后入账。",
        "pending": [d],
    }


def _run_actions(parsed: dict, text: str) -> dict:
    """执行 LLM 选出的动作：记账=生成待确认草稿；查询=真数据。返回文案+结构化结果。"""
    events = _events()

    def h_cashflow(evs, act):
        out = query_tools.cashflow_text(evs)
        return {"text": out["text"], "payload": {"state": out.get("state"), "forecast": out.get("forecast")}}

    def h_latest_income(evs, act):
        return {"text": query_tools.latest_event_text(evs, "income"),
                "payload": {"event": query_tools.latest_event(evs, "income")}}

    def h_latest_expense(evs, act):
        return {"text": query_tools.latest_event_text(evs, "expense"),
                "payload": {"event": query_tools.latest_event(evs, "expense")}}

    def h_spending(evs, act):
        m = act.get("month")
        return {"text": query_tools.spending_month_text(evs, month=m),
                "payload": query_tools.spending_month(evs, month=m)}

    def h_recent(evs, act):
        limit = int(act.get("limit") or 5)
        return {"text": query_tools.recent_events_text(evs, limit=limit),
                "payload": {"recent": list(reversed(evs))[:limit]}}

    handlers = {
        "ask_cashflow": h_cashflow,
        "ask_latest_income": h_latest_income,
        "ask_latest_expense": h_latest_expense,
        "ask_spending": h_spending,
        "ask_recent": h_recent,
    }
    parts: list[str] = []
    results: list[dict] = []
    drafts: list[dict] = []
    executed = False
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
            executed = True
            d = _draft_one(direction, amount, a.get("category"), a.get("channel"),
                           a.get("note") or text, a.get("counterparty", ""))
            drafts.append(d)
            dir_cn = "收入" if direction == "income" else "支出"
            parts.append(f"识别到{dir_cn} {_yuan(amount)}（{d['category']} · {d['channel']}）——请确认后入账。")
            results.append({"action": "record_draft", "status": "pending_confirm", "id": d["id"],
                            "direction": direction, "amount_cents": amount,
                            "category": d["category"], "channel": d["channel"]})
        elif kind in handlers:
            executed = True
            out = handlers[kind](events, a)
            parts.append(out["text"])
            results.append({"action": kind, "data": out["payload"]})
    return {"parts": parts, "executed": executed, "results": results, "drafts": drafts}


@app.get("/api/capabilities")
def capabilities():
    on = llm_skill.is_configured()
    return {"llm": on, "model": (llm_skill._model() if on else None)}


@app.get("/api/pending")
def pending_list():
    """待确认草稿列表（识别→确认→入账）。"""
    return {"pending": pending.list_all(DATA_DIR)}


@app.post("/api/pending/{pid}/accept")
def pending_accept(pid: str):
    """确认入账：写入事件账本。"""
    out = pending.accept(DATA_DIR, pid, _append_event)
    if out is None:
        raise HTTPException(status_code=404, detail="待确认草稿不存在")
    state = run_state(LEDGER_PATH, DB_PATH)["state"]
    return {"ok": True, "appended": out["appended"], "draft": out["draft"], "state": state}


@app.post("/api/pending/{pid}/decline")
def pending_decline(pid: str):
    """不要这笔：丢弃草稿，不入账。"""
    if not pending.decline(DATA_DIR, pid):
        raise HTTPException(status_code=404, detail="待确认草稿不存在")
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn):
    """多轮自由对话：会话记忆 + LLM 选动作 + 真执行 + 结果合成（记录走待确认）。"""
    sid = body.session_id or uuid.uuid4().hex
    session_mem.touch(sid)
    memory = session_mem.context_of(sid)
    prompt = f"[对话上下文]\n{memory}\n[用户现在说]\n{body.text}" if memory else body.text

    if llm_skill.is_configured():
        parsed = llm_skill.parse_accounting(prompt)
        if parsed is not None:
            run = _run_actions(parsed, body.text)
            if run["executed"]:
                results_json = json.dumps(run["results"], ensure_ascii=False, default=str)[:2200]
                final = ""
                try:
                    final = llm_skill.compose_reply(body.text, results_json)
                except Exception:
                    final = ""
                if not final or len(final) < 8:
                    final = "\n".join(run["parts"])
            else:
                final = str(parsed.get("reply", "")).strip() or "我还在学习这句怎么接——可以记一笔账，或问我最近流水 / 现金流。"
            session_mem.add_turn(sid, body.text, final, result_note=final[:120])
            return {"ok": True, "text": final, "session_id": sid,
                    "pending": [{k: d[k] for k in ("id", "direction", "amount_cents", "category", "channel")} for d in run["drafts"]]}

    r = _rule_reply(body.text)
    session_mem.add_turn(sid, body.text, r.get("text", ""))
    r["session_id"] = sid
    return r
