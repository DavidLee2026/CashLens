"""CashLens 后端 API（本地优先 · 真数据链路）

职责：给「真数据对话式工作台」前端提供账本写入与状态/预测读取，全部接 state_engine 真计算。
启动：cd backend && python3 -m uvicorn app.main:app --port 8001
说明：金额一律以分（整数）传输；前端做语义解析（规则/语音），后端做记账与状态（真引擎）。
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .services import (categories, intake, invoice_tax, llm_skill, model_config,
                       parser_rule, pending, projects, query_tools, session_mem,
                       timesheet)
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
    """一笔入账（amount_cents 为正整数；方向由 type 表达）。

    project 可传稳定 id（project_a）或项目显示名；留空则归入当前默认项目。
    invoice_no 是发票号码，用于报销口径去重（同一项目内相同号码只计一次）；没有就留空，不推断。
    """

    event_type: str
    amount_cents: int = Field(gt=0)
    evidence_kind: str = "voice"
    ts: str | None = None
    channel: str = "voice"
    category: str = ""
    counterparty: str = ""
    note: str = ""
    confirmed: bool = False
    project: str = ""
    invoice_no: str = ""


@app.get("/api/health")
def health():
    return {"ok": True, "api": "cashlens", "ledger": str(LEDGER_PATH)}


@app.post("/api/events")
def add_event(ev: EventIn):
    """写一笔到事件账本（dedupe 幂等），返回写结果 + 最新状态。

    project 留空时归入当前默认项目（没有就建 Project A），并在返回里附一句名称提醒。
    """
    ts = datetime.fromisoformat(ev.ts) if ev.ts else datetime.now()
    pid, project_hint = projects.resolve_incoming(DATA_DIR, ev.project)
    invoice_no = str(ev.invoice_no or "").strip()
    try:
        event = make_event(
            ts=ts, event_type=ev.event_type, amount_cents=ev.amount_cents,
            evidence_kind=ev.evidence_kind, channel=ev.channel, category=ev.category,
            counterparty=ev.counterparty, note=ev.note, confirmed=ev.confirmed,
            project=pid,
            extra={"invoice_no": invoice_no} if invoice_no else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    book = EventLedger(LEDGER_PATH)
    result = book.append(event, strict_dedupe=True)
    state = run_state(LEDGER_PATH, DB_PATH)["state"]
    out = {"appended": result["appended"], "event": event, "state": state}
    if project_hint:
        out["project_hint"] = project_hint
    return out


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


def _append_event(direction: str, amount_cents: int, category: str, channel: str, note: str,
                  counterparty: str = "", project: str = ""):
    """真正写入事件账本（确认后调用）。返回 (event, appended)。project 在草稿生成时已解析好。"""
    ev = make_event(
        ts=datetime.now(), event_type=direction, amount_cents=amount_cents,
        evidence_kind="voice", channel=channel or "manual", category=category or "其他",
        counterparty=counterparty or "", note=note or "", confirmed=True,
        project=project or "",
    )
    book = EventLedger(LEDGER_PATH)
    res = book.append(ev, strict_dedupe=True)
    return ev, res["appended"]


def _project_label(pid: str) -> str:
    """project id → 显示名；找不到或为空时回落成「未归项目」。"""
    row = projects.get(DATA_DIR, pid) if pid else None
    return str((row or {}).get("name") or projects.UNASSIGNED_LABEL)


def _draft_one(direction: str, amount_cents: int, category: str, channel: str, note: str,
               counterparty: str = "", project: str | None = None) -> dict:
    """识别 → 只生成待确认草稿（不直接入账）。

    project 可传 id 或显示名；留空则归入当前默认项目，并在草稿上附一句项目名提醒。
    """
    pid, hint = projects.resolve_incoming(DATA_DIR, project)
    d = pending.create(
        DATA_DIR, direction=direction, amount_cents=amount_cents,
        category=category, channel=channel, note=note, counterparty=counterparty,
        project=pid,
    )
    if hint:
        d["project_hint"] = hint
    return d


def _project_line(d: dict) -> str:
    """草稿落哪个项目的那句话（优先用 resolve 时给的提醒原文）。"""
    return str(d.get("project_hint") or f"归入项目「{_project_label(d.get('project', ''))}」。")


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
        "text": f"识别到{dir_cn} {_yuan(r['amount_cents'])}（{r['category']} · {r['channel']}）——请确认后入账。{_project_line(d)}",
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
                           a.get("note") or text, a.get("counterparty", ""),
                           a.get("project") or None)
            drafts.append(d)
            dir_cn = "收入" if direction == "income" else "支出"
            parts.append(f"识别到{dir_cn} {_yuan(amount)}（{d['category']} · {d['channel']}）——请确认后入账。{_project_line(d)}")
            results.append({"action": "record_draft", "status": "pending_confirm", "id": d["id"],
                            "direction": direction, "amount_cents": amount,
                            "category": d["category"], "channel": d["channel"],
                            "project": d.get("project", ""),
                            "project_name": _project_label(d.get("project", ""))})
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


class ProjectIn(BaseModel):
    """建项目 / 改项目名。id 留空 = 新建；带 id（或显示名）+ name = 重命名。"""

    name: str = ""
    id: str = ""


@app.get("/api/projects")
def projects_list():
    """项目清单（含每个项目的收支汇总）+ 未归项目一行。

    项目是账本事件的属性维度：真实项目（如 919 昆明项目），不是费用类别（打车/吃饭）。
    """
    return projects.list_projects(DATA_DIR, _events())


@app.post("/api/projects")
def projects_upsert(body: ProjectIn):
    """建项目或重命名。

    账本只存稳定 id，显示名存在 data/projects.json 映射表里；
    因此重命名只改映射，账本一字不动（守住账本不可变）。
    """
    if body.id:
        pid = projects.resolve(DATA_DIR, body.id)
        if pid is None:
            raise HTTPException(status_code=404, detail=f"项目不存在：{body.id}")
        try:
            row = projects.rename(DATA_DIR, pid, body.name)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "action": "renamed", "project": row}
    return {"ok": True, "action": "created", "project": projects.create(DATA_DIR, body.name)}


@app.get("/api/projects/summary")
def projects_summary(project: str | None = None):
    """按项目归集账本：收入 / 支出 / 净额 / 分类分解。

    支出侧即报销口径：同一项目内发票号码相同的事件只计一次，剔除的如实列在 duplicates 里；
    账本未记录发票号码的事件不参与去重（不推断）。
    """
    out = projects.summary(DATA_DIR, _events(), project=project)
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail=out.get("error", "项目不存在"))
    return out


@app.get("/api/categories")
def categories_view():
    """分类体系（唯一权威表）：支出 / 收入一级分类 + 报销别名 + 口语别名。

    分类口径：按用途判，不按商户；判不出进「待确认」，不当类别统计。
    """
    return categories.public_view()


class IntakeFileIn(BaseModel):
    """一个待导入文件。content_b64 是文件内容的 base64（前端拖入后原样编码）。"""

    name: str
    rel_path: str = ""
    content_b64: str


class IntakeIn(BaseModel):
    """批量导入：图片 / PDF / Excel / CSV，可按文件夹整批投喂。"""

    files: list[IntakeFileIn]
    project: str = ""


# 单文件与整批的体量上限（防误拖整个目录把内存打满；超限如实报错，不静默截断）
_INTAKE_MAX_FILE_BYTES = 30 * 1024 * 1024
_INTAKE_MAX_FILES = 80


@app.post("/api/intake")
def intake_upload(body: IntakeIn):
    """把用户拖进来的东西变成待确认草稿。

    归属规则：显式指定 project > 文件夹名建项目 > 当前默认项目。
    一律只生成草稿，需人工确认后才入账（合规红线：确认环节不得为体验取消）。
    报销表以表内数字为准，嵌入图识别结果只用于核对。
    """
    if not body.files:
        raise HTTPException(status_code=400, detail="没有文件：请拖入图片、PDF、Excel 或 CSV")
    if len(body.files) > _INTAKE_MAX_FILES:
        raise HTTPException(status_code=413,
                            detail=f"一次最多 {_INTAKE_MAX_FILES} 个文件，本次 {len(body.files)} 个")

    files: list[dict] = []
    for f in body.files:
        try:
            blob = base64.b64decode(f.content_b64, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail=f"文件内容不是合法 base64：{f.name}")
        if len(blob) > _INTAKE_MAX_FILE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"文件过大（上限 30MB）：{f.name}")
        files.append({"name": Path(f.name).name, "rel_path": f.rel_path or f.name,
                      "content": blob})

    try:
        out = intake.ingest(DATA_DIR, files, project_ref=body.project or None)
    except Exception as e:  # noqa: BLE001 失败要报出来，不吞
        raise HTTPException(status_code=500, detail=f"导入失败：{type(e).__name__}: {str(e)[:200]}")
    out["pending"] = [{"id": d["id"], "direction": d["direction"],
                       "amount_cents": d["amount_cents"], "category": d["category"],
                       "project": d.get("project", ""),
                       "project_name": _project_label(d.get("project", ""))}
                      for d in pending.list_all(DATA_DIR)]
    return out


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
                    "pending": [{"id": d["id"], "direction": d["direction"],
                                 "amount_cents": d["amount_cents"], "category": d["category"],
                                 "channel": d["channel"], "project": d.get("project", ""),
                                 "project_name": _project_label(d.get("project", ""))}
                                for d in run["drafts"]]}

    r = _rule_reply(body.text)
    session_mem.add_turn(sid, body.text, r.get("text", ""))
    r["session_id"] = sid
    return r
