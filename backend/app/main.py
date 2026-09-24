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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .services import (invoice_tax, llm_skill, model_config, parser_rule, pending,
                       query_tools, session_mem, timesheet)
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


class ModelSelectIn(BaseModel):
    """切换模型档位。未传的字段保持原值；api_key 只入不出。"""

    tier: str
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    preset_id: str | None = None
    declared_vision: bool | None = None


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
    view = model_config.public_view()
    act = view["active"]
    return {"llm": act["usable"], "model": (act["model"] if act["usable"] else None),
            "tier": act["tier"], "vision": act["vision"]}


@app.get("/api/models")
def models():
    """可选模型档位与当前选择。密钥只回传「是否已配置」，绝不回传原值。"""
    return model_config.public_view()


@app.post("/api/models/select")
def models_select(body: ModelSelectIn):
    """切换模型档位。运行中立即生效，无需重启后端。"""
    try:
        return model_config.select(tier=body.tier, base_url=body.base_url, model=body.model,
                                   api_key=body.api_key, preset_id=body.preset_id,
                                   declared_vision=body.declared_vision)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/models/test")
def models_test():
    """按当前档位做一次连通性自检。只发一句固定文本，不发送任何用户数据。"""
    return model_config.probe()


@app.post("/api/timesheet/summary")
async def timesheet_summary(request: Request,
                            name_col: int | None = None,
                            hours_col: int | None = None,
                            rate_col: int | None = None,
                            project_col: int | None = None,
                            header_row: int | None = None):
    """工时 / 工分表 → 列映射建议 + 按人汇总的工资参考表。

    请求体直接是 .xlsx 二进制（前端拖入文件后原样 POST 即可，无需 multipart）。
    纯本机解析，无网络调用；金额以分返回。列名认不出时如实标注 needs_confirm，不猜列。
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="请求体为空：请以 xlsx 二进制作为 body 提交")
    overrides: dict[str, int] = {}
    for role, idx in (("name", name_col), ("hours", hours_col),
                      ("rate", rate_col), ("project", project_col)):
        if idx is not None:
            overrides[role] = idx
    try:
        out = timesheet.analyze(body, overrides=overrides or None, header_row=header_row)
    except Exception as e:  # noqa: BLE001 交给调用方看 400，不把栈打到响应里
        raise HTTPException(status_code=400, detail=f"xlsx 解析失败：{type(e).__name__}")
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out.get("error", "解析失败"))
    return out


@app.get("/api/invoice/draft")
def invoice_draft(since: str | None = None, until: str | None = None,
                  tax_rate: float | None = None):
    """开票信息生成：按客户聚合账本收入明细。

    只做归集与呈现，不代开发票、不核定税目、不计算应缴税额；
    开票状态与金额是否含税账本均无字段，一律标 unknown 交用户确认。
    """
    return invoice_tax.invoice_draft(_events(), since=since, until=until, tax_rate=tax_rate)


@app.get("/api/tax/quarterly")
def tax_quarterly(quarter: str | None = None, recent: int = 4):
    """报税归集：按季度聚合账本收入（只陈述事实，不计算应纳税额、不给筹划建议）。"""
    return invoice_tax.tax_quarterly(_events(), quarter=quarter, recent=recent)


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
