"""CashLens 后端 API（本地优先 · 真数据链路）

职责：给「真数据对话式工作台」前端提供账本写入与状态/预测读取，全部接 state_engine 真计算。
启动：cd backend && python3 -m uvicorn app.main:app --port 8001
说明：金额一律以分（整数）传输；前端做语义解析（规则/语音），后端做记账与状态（真引擎）。
"""

from __future__ import annotations

import base64
import functools
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .services import (categories, intake, invoice_tax, llm_skill, model_config,
                       parser_rule, pending, projects, query_tools, session_mem,
                       timesheet)
from .services.state_engine import (EventLedger, Projection, calc, decision as decision_engine,
                                    make_event, run_state)

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
    hours: float | None = Field(default=None, gt=0)
    """这笔投入了多少小时（可选）。**决策引擎靠它反推你的历史时薪**：不填就永远算不出时薪，
    缺数据时决策会如实说 insufficient，而不是拿默认值凑（2026-09-28 加）。"""


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
    # hours 与 invoice_no 一样走 extra 透传（账本 schema 不动，向后兼容 v3 旧事件）
    extra: dict = {}
    if invoice_no:
        extra["invoice_no"] = invoice_no
    if ev.hours:
        extra["hours"] = float(ev.hours)
    try:
        event = make_event(
            ts=ts, event_type=ev.event_type, amount_cents=ev.amount_cents,
            evidence_kind=ev.evidence_kind, channel=ev.channel, category=ev.category,
            counterparty=ev.counterparty, note=ev.note, confirmed=ev.confirmed,
            project=pid,
            extra=extra or None,
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
    """用户的一句话（记账/查询/闲聊）+ 可选会话 id + 当前选中项目。

    `project`（2026-09-26 新增，欠账 E 组 18）：前端左栏当前选中的项目（id 或完整名），
    只作为**新账的默认归属**；查询口径始终是全部项目（总账户），见 `_scope_note`。
    留空 ＝ 未归项目（总账户），不再落到"表里第一个项目"。
    """

    text: str
    session_id: str | None = None
    project: str = ""
    user: str = ""


def _user_context(name: str) -> str:
    """把「本机登录名」拼成给模型/规则看的上下文。

    这是用户**在本机填的一个名字**（存在浏览器本地，无账号体系、无密码、不联网验证）。
    ⚠️ 措辞上不要往回答里塞免责话术：用户 2026-09-28 明确要求「不是账号、也没有联网验证过」
    这类说明**不要写进回复**（在对话里像免责声明，很出戏）。约束留在系统侧即可。
    """
    s = str(name or "").strip()
    if not s:
        return ""
    return f"当前用户在本机填的登录名：{s}（只有涉及称呼时才用它）"


class PendingAcceptIn(BaseModel):
    """确认草稿时的可选覆盖：用户在确认那一刻挑的项目与分类（空字符串 ＝ 未归项目 / 未分类）。"""

    project: str | None = None
    category: str | None = None


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


# 只有**文件导入**产生的草稿才开严格去重（防的是"同一份票据被导入两次"）。
# 口述/一句话记的草稿是用户逐笔确认的：他说两遍就是两笔，不该被去重悄悄吃掉。
_FILE_DRAFT_SOURCES = ("识别", "报销表", "账单")


def _append_event(direction: str, amount_cents: int, category: str, channel: str, note: str,
                  counterparty: str = "", project: str = "", *, strict_dedupe: bool = True):
    """真正写入事件账本（确认后调用）。返回 (event, appended)。project 在草稿生成时已解析好。

    `strict_dedupe`：账本里已有同 dedupe_key 的事件时是否跳过不写。默认 True（导入路径用），
    口述草稿由 `pending_accept` 传 False —— 见 `_FILE_DRAFT_SOURCES` 的说明。
    """
    ev = make_event(
        ts=datetime.now(), event_type=direction, amount_cents=amount_cents,
        evidence_kind="voice", channel=channel or "manual", category=category or "其他",
        counterparty=counterparty or "", note=note or "", confirmed=True,
        project=project or "",
        # 口述/手工确认的条目不去重（见 make_event 的 dedupe_unique 说明）
        dedupe_unique=not strict_dedupe,
    )
    book = EventLedger(LEDGER_PATH)
    res = book.append(ev, strict_dedupe=strict_dedupe)
    return ev, res["appended"]


def _project_label(pid: str) -> str:
    """project id → 显示名；找不到、为空、或**项目已删**时回落成「未归项目」。

    已删项目要回落，是为了和账本视图口径一致：账本里已删项目名下的事件在视图里
    也回落成「未归项目」（见 `projects.detail`）。否则同一笔钱在草稿和账本里
    会显示成两个不同的归属（2026-09-28 修：此前草稿挂在已删项目上会照旧显示旧名）。
    """
    row = projects.get(DATA_DIR, pid) if pid else None
    if row and not projects.is_deleted(row):
        return str(row.get("name") or projects.UNASSIGNED_LABEL)
    return projects.UNASSIGNED_LABEL


def _scope_note(project_ref: str) -> str:
    """当前选中了项目时，查询答复要**标注口径范围**（2026-09-26 David 拍板）。

    查询默认全账本（总账户）、不跟随选中项目；选中项目只影响"新账默认记到哪"。
    所以这里有话必须说清楚，免得用户以为"选了 X 就只算 X"。
    """
    s = str(project_ref or "").strip()
    if not s:
        return ""
    pid = projects.resolve(DATA_DIR, s) or s
    return (f"（口径提示：以上数字是全部项目的合计——总账户；"
            f"当前选中只是让新账默认记入「{_project_label(pid)}」，查询不分项目）")


def _draft_one(direction: str, amount_cents: int, category: str, channel: str, note: str,
               counterparty: str = "", project: str | None = None) -> dict:
    """识别 → 只生成待确认草稿（不直接入账）。

    project 可传 id 或显示名（"当前选中的项目"也走这里）；**留空 ＝ 未归项目（总账户）**，
    不再落到"表里第一个项目"（见 `projects.resolve_incoming` 的 2026-09-26 改动）。
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


# 「我是谁」这类问题的触发词（规则兜底用；有 LLM 时由提示词里的规则负责）
_IDENTITY_HINTS = ("我是谁", "我叫什么", "我的名字", "你认识我", "你知道我是谁", "认得我", "知道我叫")
# 打招呼（只在**没配 LLM** 的兜底路上用，且必须很短、不含金额，免得把「你好，打车 28」截走）
_GREETING_HINTS = ("你好", "您好", "hi", "hello", "在吗", "在么")


def _warm_open(name: str) -> str:
    """有情绪价值的开场（用户 2026-09-28 要求）：叫出名字 + 表示乐意服务 + 问一句需要什么。

    ⚠️ 不夹带任何免责话术（「不是账号 / 没联网验证过」这类）—— 同一天他明确要求删掉：
    在对话里像免责声明，很出戏。诚实约束留在系统侧，不念给用户听。
    """
    return (f"你是{name}呀，很高兴为你服务！想记一笔账、看看最近的现金流，"
            f"还是算一下这单报价？有什么需要尽管说。")


def _rule_reply(text: str, current_project: str = "",
                user: str = "") -> dict:
    """规则兜底（无 LLM 时）：打招呼 / 身份问题 / 记账走待确认 / 问现金流 / 接不上。

    `current_project` = 前端当前选中的项目，作为新账默认归属（留空＝未归项目）。
    `user` = 本机登录名；没有 LLM 时也要能如实回答「我是谁」，否则用户填了名字却
    在没配 Key 的档位下问不出来（同一个问题两种档位两种答案，很怪）。
    """
    t = str(text or "")
    if any(h in t for h in _IDENTITY_HINTS):
        if user:
            return {"ok": True, "text": _warm_open(user)}
        return {"ok": True,
                "text": "你还没填登录名，我还不知道该怎么称呼你。点右上角「登录」填一个，"
                        "下次我就能叫你了 —— 想记账或看现金流现在也可以直接说。"}
    # 纯打招呼（很短、不带数字）也给一句有人情味的回应；带金额的照旧走记账，别截走
    if user and len(t) <= 12 and not any(c.isdigit() for c in t) \
            and any(g in t.lower() for g in _GREETING_HINTS):
        return {"ok": True, "text": _warm_open(user)}
    r = parser_rule.analyze(text)
    if r["kind"] == "fallback":
        return {"ok": False, "text": "这句我先接不上：说一句带金额的记账（如「昨天微信收了 3000 尾款」），或问「最近一笔收入 / 这个月花了多少 / 现金流怎么样」。配置 LLM API Key 后可自由对话。"}
    if r["kind"] == "ask_cashflow":
        out = _cashflow_text()
        note = _scope_note(current_project)
        return {"ok": True, "text": out["text"] + (f"\n{note}" if note else ""),
                "state": out.get("state"), "forecast": out.get("forecast")}
    d = _draft_one(r["direction"], r["amount_cents"], r["category"], r["channel"], r["text"],
                   project=current_project or None)
    dir_cn = "收入" if r["direction"] == "income" else "支出"
    return {
        "ok": True,
        "text": f"识别到{dir_cn} {_yuan(r['amount_cents'])}（{r['category']} · {r['channel']}）——请确认后入账。{_project_line(d)}",
        "pending": [d],
    }


def _run_actions(parsed: dict, text: str, current_project: str = "") -> dict:
    """执行 LLM 选出的动作：记账=生成待确认草稿；查询=真数据。返回文案+结构化结果。

    归属优先级（2026-09-26，欠账 E 组 18）：**动作里显式说的项目 > 前端当前选中项目 > 未归项目**。
    当前选中项目只是"默认值"，用户在一句话里说了别的项目要能覆盖它。
    """
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

    def h_decision(evs, act):
        """接单决策：数值与文案都由确定性引擎给出，**不让 LLM 复述或重算数字**。"""
        order = {
            "amount_cents": int(act.get("amount_cents") or 0),
            "estimated_hours": float(act.get("estimated_hours") or 0),
            "deliver_days": act.get("deliver_days"),
            "counterparty": str(act.get("counterparty") or ""),
            "tax_rate": act.get("tax_rate"),
            "platform_rate": act.get("platform_rate"),
        }
        out = decision_engine.decide(evs, order)
        lines = [f"{out['decision']}｜{out['headline']}"]
        lines += [f"· {r['text']}" for r in out["reasons"][:3]]
        if out["min_price_cents"]:
            lines.append(f"· 报价下限 {_yuan(out['min_price_cents'])}；"
                         f"推荐 {_yuan(out['quote']['recommend_cents'])}、"
                         f"上限 {_yuan(out['quote']['ceiling_cents'])}")
        lines.append(f"· 主要风险：{out['risk']}")
        lines.append(out["disclaimer"])
        return {"text": "\n".join(lines), "payload": {"decision": out}}

    handlers = {
        "ask_cashflow": h_cashflow,
        "ask_latest_income": h_latest_income,
        "ask_latest_expense": h_latest_expense,
        "ask_spending": h_spending,
        "ask_recent": h_recent,
        "ask_decision": h_decision,
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
                           a.get("project") or current_project or None)
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
    # 决策类结果单独拎出来给前端渲染决策卡（其余查询结果不额外透出）
    decision_payload = next(
        (r["data"]["decision"] for r in results
         if r.get("action") == "ask_decision"
         and isinstance(r.get("data"), dict) and "decision" in r["data"]),
        None,
    )
    return {"parts": parts, "executed": executed, "results": results, "drafts": drafts,
            "decision": decision_payload}


class DecisionIn(BaseModel):
    """一笔潜在订单（接单决策的输入）。

    `estimated_hours` 是**必填**且不接受默认值：没有工时就算不出等效时薪，我们不猜
    （设计与 golden case 见 `00-总览/决策引擎最小版-问题分析与golden-case-20260912.md`）。
    """

    amount_cents: int = Field(gt=0)
    estimated_hours: float = Field(gt=0)
    deliver_days: int | None = None
    counterparty: str = ""
    tax_rate: float | None = Field(default=None, ge=0, lt=1)
    platform_rate: float | None = Field(default=None, ge=0, lt=1)
    note: str = ""


@app.post("/api/decision")
def decision(body: DecisionIn):
    """接单决策：这单接不接、报价下限是多少（确定性计算 + 可溯源理由）。

    口径纪律：
    - 数值全部来自本地账本与状态引擎，**LLM 不参与任何数值**；
    - 历史时薪由账本反推（中位数 ÷ 总工时），**不需要用户填时薪**；
    - 缺「带工时的收入记录」时如实返回 `hourly.basis=insufficient`，**不给**假装精确的报价下限；
    - 引用预测必标注代理口径（账本里没有「当前余额」这个量）。
    """
    order = body.model_dump()
    return decision_engine.decide(_events(), order)


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


@app.delete("/api/projects/{pid}")
def projects_delete(pid: str, with_data: bool = False):
    """删除项目。两种口径由用户在界面上自己选：

    - `with_data=false`（只删项目）：映射表标记删除，**账本一字不动**；
      该项目名下的账单在视图里回落到「未归项目」，数字不会凭空消失。
    - `with_data=true`（项目与账单一起删）：向账本**追加一条作废记录**，被作废的
      事件从此不再参与任何统计。**不重写历史文件**，守住账本追加式不可变这条红线，
      因此误删仍可从账本里查回。

    已删除的项目不再出现在清单里，也不再参与入账归属（避免新账记进已删项目）。
    """
    # 「未归项目」是**兜底账户**（project 为空）而不是一个项目行 ——
    # 它没有可"删除"的东西：`mark_deleted` 无从下手，而且就算硬把它从清单里去掉，
    # 那些 project 为空的事件下一秒还是显示在「未归项目」下面。所以对它唯一有意义的
    # 动作是**把它名下的账作废**。
    # 起因（2026-09-28 用户两次反馈）：先问「未归项目无法点击修改或者删除」，
    # 补了「点开明细」之后又问「我无法删除」—— 他要的是能把里面的账清掉。
    if str(pid).strip() in ("unassigned", projects.UNASSIGNED_LABEL, "-"):
        uids = projects.event_ids_of(_events(), "")
        if not with_data:
            raise HTTPException(status_code=400, detail=(
                f"「{projects.UNASSIGNED_LABEL}」是兜底账户、不是一个项目，没有可删的项目行；"
                f"它名下现有 {len(uids)} 笔。要清理它名下的账，请用 with_data=true"
                f"（把这 {len(uids)} 笔作废）。"))
        uvoided = 0
        if uids:
            uvoided = EventLedger(LEDGER_PATH).append_void(
                uids, reason=f"清理「{projects.UNASSIGNED_LABEL}」名下的账（用户主动作废）",
                project="")["voided"]
            uproj = Projection(DB_PATH)
            uproj.rebuild(EventLedger(LEDGER_PATH).load())
            uproj.close()
        return {
            "ok": True,
            "action": "voided_unassigned",
            "project": None,
            "bill_count": len(uids),
            "voided_count": uvoided,
            "note": (f"「{projects.UNASSIGNED_LABEL}」名下的 {uvoided} 笔已作废，界面与统计都不再计入"
                     f"（账本里留痕，可追溯）。未归项目本身是兜底账户、删不掉 —— "
                     f"以后没指定项目的新账还会进这里。"),
        }

    target = projects.resolve(DATA_DIR, pid)
    if target is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{pid}")
    row = projects.get(DATA_DIR, target) or {}
    label = str(row.get("name") or target)

    ids = projects.event_ids_of(_events(), target)
    voided = 0
    if with_data:
        voided = EventLedger(LEDGER_PATH).append_void(
            ids, reason=f"删除项目「{label}」时一并作废", project=target)["voided"]
        # SQLite 投影是按 MODEL_VERSION 缓存的，作废不改变模型版本 —— 必须显式重建，
        # 否则投影里的旧行（assumptions/transactions）还会留着已被作废的事件。
        proj = Projection(DB_PATH)
        proj.rebuild(EventLedger(LEDGER_PATH).load())
        proj.close()

    out = projects.mark_deleted(DATA_DIR, target, purged_count=voided, with_data=with_data)
    if out is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{pid}")
    return {
        "ok": True,
        "action": "purged" if with_data else "deleted",
        "project": out,
        "bill_count": len(ids),
        "voided_count": voided,
        "note": (f"「{label}」与它名下的 {voided} 笔账单已一起删除。"
                 if with_data else
                 f"「{label}」已删除；它名下的 {len(ids)} 笔账单仍在账本里，"
                 f"在项目面板里回落到「未归项目」。"),
    }


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


@app.get("/api/projects/{pid}/detail")
def projects_detail(pid: str):
    """单个项目的明细（详情弹窗用）：合计 / 分类构成 / 报销与发票 / 最近事件 / 时间范围。

    口径复用 `/api/projects/summary`（支出侧按发票号去重），所以"总览"与"详情"的数字必然一致
    —— 不另写一套算法，避免同一笔在两处显示不同金额。
    """
    out = projects.detail(DATA_DIR, _events(), pid)
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
    """批量导入：图片 / PDF / Excel / CSV，可按文件夹整批投喂。

    `reconcile`（2026-09-28 新增）：报销表的**双源核对**（逐张识别表内嵌入图，与表内金额对账）
    要不要跑。默认跑 —— 它能发现"表里写 47.13、票上其实 41.73"这类错。但它是最慢的一步
    （10 张图串行跑 3 分钟，实测 18 秒/张），而表内数字本来就是权威、核对不是入账必需，
    所以给用户一个**跳过**的开关（着急时草稿 0 秒就能出）。
    """

    files: list[IntakeFileIn]
    project: str = ""
    reconcile: bool = True


# 单文件与整批的体量上限（防误拖整个目录把内存打满；超限如实报错，不静默截断）
_INTAKE_MAX_FILE_BYTES = 30 * 1024 * 1024
_INTAKE_MAX_FILES = 80


def _intake_decode(body: IntakeIn) -> list[dict]:
    """校验并解出上传的文件。`/api/intake` 与 `/api/intake/stream` 共用，保证两端口径一致。"""
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
    return files


def _first_ok(items: list[dict]) -> dict:
    """取第一条成功的结果（失败条目只用于说明原因）。"""
    for it in items or []:
        if it.get("ok") is not False:
            return it
    return {}


def _file_stages(name: str, items: list[dict], errors: list[dict],
                 draft_count: int, total_cents: int) -> dict:
    """把一个文件的处理过程拆成四段：读取了什么 / 识别了什么 / 处理了什么 / 怎么处理的。

    为什么拆四段（David 2026-09-25）：把结果堆成一坨，用户看不出每一步做了什么，
    更看不出「没识别出来」是卡在哪一段。每段只陈述事实；金额交给前端格式化。
    """
    got = _first_ok(items)
    kind = got.get("kind") or ("image" if "amount_cents" in got else "")
    ext = Path(str(name or "")).suffix.lower()
    made = (f"生成 {draft_count} 条待确认草稿，合计 {total_cents / 100:.2f} 元"
            if draft_count else "未生成草稿")

    # 不支持的类型 / 识别失败：前两段照留空位，把原因写在「方式」里。
    # 注意：不支持的类型只出现在 batch["errors"] 里（不进 files），所以两个来源都要看。
    if not got:
        errs = [str(i.get("error") or "") for i in (items or []) if i.get("ok") is False]
        errs += [str(e.get("error") or "") for e in (errors or [])]
        reason = next((e for e in errs if e), "没有可用的识别结果")
        # 内部值（unknown / unsupported）不甩给用户
        if "暂不支持" in reason:
            reason = "暂不支持这种文件类型"
        return {"read": "—", "recognized": "—", "processed": "未生成草稿",
                "how": f"{reason}——已跳过，不影响其他文件"}

    if kind == "sheet":
        read = f"表内 {got.get('row_count', 0)} 行"
        imgs = int(got.get("embedded_image_count") or 0)
        if imgs:
            read += f"，含 {imgs} 张嵌入图"
        rec = got.get("reconcile") or {}
        if got.get("reconcile_skipped"):
            # ⚠️ 跳过 ≠ 表里没有图：这张表可能嵌了 10 张，只是这次没核。两种情形必须分开说，
            # 否则用户会以为"这表里没有嵌入图"（2026-09-28 默认改成不核对之后尤其要紧）。
            recognized = (f"表内 {imgs} 张嵌入发票本次未核对（开关关着）" if imgs
                          else "表内数据（这张表里没有嵌入图）")
            how = "以表内数字为准（本次未逐张核对嵌入发票）；机器不覆盖你写的数"
        else:
            recognized = (f"嵌入图核对 {rec.get('matched', 0)}/{rec.get('checked', 0)} 张与表内金额一致"
                          if rec.get("checked") else "表内数据（这张表里没有嵌入图）")
            how = "以表内数字为准；嵌入图只用于核对，机器不覆盖你写的数"
        return {"read": read, "recognized": recognized, "processed": made, "how": how}

    if kind == "csv":
        src = str(got.get("source") or "")
        # 内部值 unknown 不直接甩给用户
        label = "账单格式未识别，已按通用规则解析" if src in ("", "unknown") else f"{src} 账单格式"
        drafted = int(got.get("drafted") or 0)
        return {"read": f"读到 {got.get('records', 0)} 行（{label}）",
                "recognized": f"其中 {drafted} 行识别为收支" if drafted else "没有识别到可入账的收支",
                "processed": made,
                "how": "只按通用规则解析；用微信 / 支付宝导出的原始账单文件识别更准"}

    # 图片 / PDF：走票据识别通道
    amt = int(got.get("amount_cents") or 0)
    bits = [f"金额 {amt / 100:.2f} 元"] if amt else []
    for key, label in (("date", "日期"), ("merchant", "商户"), ("invoice_no", "发票号")):
        if got.get(key):
            bits.append(f"{label} {got[key]}")
    if got.get("confidence") is not None:
        bits.append(f"置信度 {got['confidence']}")
    # 「方式」一栏只说用户该知道的两件事：这张票怎么处理、结果由谁点头。
    # **不叙述用哪一档模型、也不叙述供应商**（2026-09-28 David 定）：写出来会让人以为自己的
    # 发票与经营信息被别人看到了，把正常的技术调用讲成了风险；这类说明归「关于」页与合规文件。
    how = "识别结果只进「待确认清单」，你确认后才入账"
    if ext == ".pdf" and not got.get("cloud_uploaded"):
        how = "PDF 本机直读文本层；" + how
    # 聊天/文字截图（不是票面、图里有多笔金额）：如实说清"金额是从文字里读出来的候选"，
    # 别让用户以为系统替他把账算好了 —— 记哪几笔由他在待确认里挑。
    n_txt = int(got.get("text_amounts") or 0)
    n_tr = int(got.get("transfer_amounts") or 0)
    n_tot = int(got.get("total_amounts") or 0)
    recognized = " · ".join(bits) if bits else "没识别到金额"
    # ── 页面类型：待支付 / 订单列表 / 全额退款（2026-09-28 用户口径）──────────
    # 这几种都**不建草稿**，而且不许含糊成「未生成草稿」—— 每一种都要说清为什么，
    # 否则用户看到的是一句无法分辨的话（"没读到"和"读到了但不算"是两件事）。
    reason = str(got.get("no_draft_reason") or "")
    refund = int(got.get("refund_cents") or 0)
    gross = int(got.get("gross_cents") or 0)
    if reason == "unpaid":
        recognized = "待支付页面：这张单**还没付款**"
        made = "未生成草稿——钱还没出去，不算支出"
        how = "等真的付了款、拿到付款凭证再记；这张只当参考"
    elif reason == "list":
        recognized = "订单列表（不是付款凭证）"
        made = "未生成草稿——订单列表只是同一笔钱的另一个视角，记了就会和付款详情重复"
        how = "要记这一笔，请用付款详情/支付成功那张；这张留着核对"
    elif reason == "fully_refunded":
        recognized = (f"实付 {gross / 100:.2f} 元 · 已全额退款"
                      if gross else "已全额退款")
        made = "未生成草稿——全额退款，这笔没有实际支出"
        how = "退款金额以页面上的退款记录为准；确认后仍要记的话请手工记一笔"
    elif refund > 0:
        # 有退款但是部分退：金额已经按净额建了草稿，这里必须把两个数摆出来让人能对账
        recognized = (f"实付 {gross / 100:.2f} 元 · 已退款 {refund / 100:.2f} 元 · "
                      f"净额 {(gross - refund) / 100:.2f} 元" if gross else recognized)
        made = f"生成 1 条待确认草稿（按净额 {max(gross - refund, 0) / 100:.2f} 元，已扣掉退款）"
        how = "实付与退款都从页面上读出，净额由系统相减；你确认后才入账"
    if n_txt:
        # 这一档不要"置信度 0"那种噪音：它不是票面，用户要看的是"读到了几个金额"
        # 「图里自己算的合计」要如实说：读到的金额数比列出来的多，用户得知道多在哪。
        tot_note = f"，其中 {n_tot} 个是图里自己算的合计（没重复列）" if n_tot else ""
        if n_tr:
            # 图里有转账卡片 → 只列转账（用户口径：「主要判断客户付出去了多少钱」）。
            # 文案不能说"逐条列进待确认"——否则读到的 N 个金额会让人以为都记了账。
            recognized = (f"不是票面：图里读到 {n_txt} 个金额{tot_note}，其中 {n_tr} 笔是转账"
                          f"（客户实际付出去的钱）")
            made = ("生成 1 条候选草稿（只列转账这一笔，其余是提交的订单/行程单）"
                    if draft_count else "图里有转账记录，但没能建出草稿，请手工核一下")
            how = "转账金额从图里原文读出来；你确认后才入账"
        elif draft_count:
            recognized = (f"不是票面：图里读到 {n_txt} 个金额{tot_note}（聊天/文字截图），"
                          f"已按原文逐条列进「待确认」")
            made = f"生成 {draft_count} 条候选草稿（金额来自图中文字，请核对该报哪几笔）"
            how = "金额是从图里文字读出来的，记哪几笔由你决定；你确认后才入账"
        else:
            # 读到了、但一条都不该列（全是别人贴的付款卡片/行程单）：不许说成"已列进待确认"。
            recognized = f"不是票面：图里读到 {n_txt} 个金额{tot_note}（聊天/文字截图）"
            made = "但没有一笔是「客户付出去的钱」（多是别人提交的付款卡片/行程单），没建草稿"
            how = "金额是从图里文字读出来的，记哪几笔由你决定；你确认后才入账"
    return {"read": "1 个 PDF" if ext == ".pdf" else "1 张图片",
            "recognized": recognized, "processed": made, "how": how}


def _merge_batches(batches: list[dict]) -> list[dict]:
    """把「逐个文件」跑出来的批次按项目合并，让最终汇总仍是「一个项目一行」。"""
    merged: dict[str, dict] = {}
    for b in batches:
        key = str(b.get("project_id") or "")
        m = merged.get(key)
        if m is None:
            m = dict(b)
            m["files"] = list(b.get("files") or [])
            m["errors"] = list(b.get("errors") or [])
            merged[key] = m
            continue
        m["files"] += list(b.get("files") or [])
        m["errors"] += list(b.get("errors") or [])
        for k in ("draft_count", "identified_total_cents", "declared_total_cents",
                  "reconcile_diff_cents"):
            m[k] = m.get(k, 0) + b.get(k, 0)
        br = b.get("reconcile")
        if br:
            mr = m.get("reconcile") or {"checked": 0, "matched": 0,
                                        "mismatched": [], "unreadable": []}
            m["reconcile"] = {
                "checked": mr.get("checked", 0) + br.get("checked", 0),
                "matched": mr.get("matched", 0) + br.get("matched", 0),
                "mismatched": list(mr.get("mismatched") or []) + list(br.get("mismatched") or []),
                "unreadable": list(mr.get("unreadable") or []) + list(br.get("unreadable") or []),
            }
        if b.get("reconcile_skipped"):
            m["reconcile_skipped"] = True
        if not m.get("project_hint") and b.get("project_hint"):
            m["project_hint"] = b["project_hint"]
    return list(merged.values())


@app.post("/api/intake")
def intake_upload(body: IntakeIn):
    """把用户拖进来的东西变成待确认草稿。

    归属规则：显式指定 project > 文件夹名建项目 > 当前默认项目。
    一律只生成草稿，需人工确认后才入账（合规红线：确认环节不得为体验取消）。
    报销表以表内数字为准，嵌入图识别结果只用于核对。

    注意：这是「整批一次调用」的老口径，用户全程看不到中间进度；
    拖拽导入走 `/api/intake/stream`（逐文件推进度）。
    """
    files = _intake_decode(body)

    try:
        out = intake.ingest(DATA_DIR, files, project_ref=body.project or None,
                          reconcile=body.reconcile)
    except Exception as e:  # noqa: BLE001 失败要报出来，不吞
        raise HTTPException(status_code=500, detail=f"导入失败：{type(e).__name__}: {str(e)[:200]}")
    out["pending"] = [{"id": d["id"], "direction": d["direction"],
                       "amount_cents": d["amount_cents"], "category": d["category"],
                       "project": d.get("project", ""),
                       "project_name": _project_label(d.get("project", ""))}
                      for d in pending.list_all(DATA_DIR)]
    return out


# 心跳间隔：单个文件可能跑好几分钟（2026-09-25 真机实测：一张报销表 239 秒），
# 期间一个字节都不发的话，浏览器或中间代理会按空闲超时把连接掐掉 —— 前端只会看到
# 「导入失败：network error」，而后端其实还在正常跑。所以边等边推心跳。
_INTAKE_TICK_SECONDS = 3


def _tick_note(name: str, prog: dict | None = None) -> str:
    """心跳里那句「现在在干什么」：只描述这个文件类型真实发生的动作，不编造百分比。

    `prog` 是**子步骤**进度（目前只有报销表的嵌入图核对会给）：有就把它拼进去 ——
    否则一张 10 行的报表要跑三分钟而界面只显示一句笼统的话，看着像卡死。
    """
    low = name.lower()
    if low.endswith((".xlsx", ".xls", ".csv")):
        if prog and prog.get("total"):
            return (f"报销表：正在{prog.get('label') or '核对嵌入图'}"
                    f"（{prog.get('done')}/{prog.get('total')} 张）")
        return "报销表：逐行取值，并逐张核对表内嵌入图（这个类型最慢，请稍等）"
    if low.endswith(".pdf"):
        return "PDF：本机直读文本层"
    if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
        return "图片：识别票面信息（金额 / 日期 / 商户）"
    return "正在识别这个文件"


def _intake_stream_events(files: list[dict], project: str | None = None,
                          reconcile: bool = False, data_dir: Path | None = None):
    """逐文件推进度的事件流（NDJSON，一行一个 JSON）。**拉驱动**。

    ⚠️ 抽成独立函数是为了能钉住「取消导入真的会停」这件事：
    这个生成器只有**消费方再要下一行**时才会往前走。用户在界面上点「取消」=
    浏览器 abort 掉这个请求 = Starlette 取消响应任务、不再向它要下一行，
    于是**后面的文件一个都不会开始**（已实测：见 tests 里那条断开测试）。
    但**正在读的那个文件停不下来** —— 它在工作线程里调识别，没有中断点。
    所以界面上的取消文案必须如实写这一句，不许承诺「立刻全停」。
    """
    root = Path(data_dir if data_dir is not None else DATA_DIR)
    total = len(files)
    batches: list[dict] = []
    for i, f in enumerate(files, 1):
        yield json.dumps({"stage": "start", "index": i, "total": total,
                          "file": f["name"], "rel_path": f["rel_path"]},
                         ensure_ascii=False) + "\n"
        t0 = time.monotonic()
        try:
            # 把耗时的 ingest 放进工作线程，主线程每几秒推一行心跳。
            # 这样「进度」是真的（已等秒数），而不是预先算好的假百分比。
            pool = ThreadPoolExecutor(max_workers=1)
            # 子步骤进度：worker 里更新，这里的心跳循环读它（同一个 dict，无需加锁 ——
            # CPython 下 dict.update 是原子的，最坏情况是心跳读到上一拍的数字）
            prog: dict = {}
            try:
                fut = pool.submit(intake.ingest, root, [f],
                                  project_ref=project or None,
                                  reconcile=reconcile,
                                  progress=lambda d, t, label: prog.update(
                                      done=d, total=t, label=label))
                while not fut.done():
                    time.sleep(_INTAKE_TICK_SECONDS)
                    if fut.done():
                        break
                    yield json.dumps({
                        "stage": "tick", "index": i, "total": total,
                        "file": f["name"],
                        "elapsed": round(time.monotonic() - t0, 1),
                        "note": _tick_note(f["name"], prog),
                    }, ensure_ascii=False) + "\n"
                out = fut.result()      # 异常在这里抛出，走下面的 file_failed，语义不变
            finally:
                # wait=False：用户中途关掉页面时，别让这条已经断了的请求把线程拖住
                # （worker 会把当前这个文件跑完，不影响其他请求）
                pool.shutdown(wait=False)
            bs = out.get("batches") or []
            batches += bs
            first = bs[0] if bs else {}
            yield json.dumps({
                "stage": "file_done", "index": i, "total": total, "file": f["name"],
                "project_name": first.get("project_name", ""),
                "draft_count": first.get("draft_count", 0),
                "identified_total_cents": first.get("identified_total_cents", 0),
                "stages": _file_stages(f["name"], first.get("files") or [],
                                         first.get("errors") or [],
                                         first.get("draft_count", 0),
                                         first.get("identified_total_cents", 0)),
                "errors": [e.get("error", "") for e in (first.get("errors") or [])],
            }, ensure_ascii=False) + "\n"
        except Exception as e:  # noqa: BLE001 单个文件失败不能把整批带崩
            yield json.dumps({"stage": "file_failed", "index": i, "total": total,
                              "file": f["name"],
                              "error": f"{type(e).__name__}: {str(e)[:200]}"},
                             ensure_ascii=False) + "\n"
    yield json.dumps({"stage": "all_done", "batches": _merge_batches(batches)},
                     ensure_ascii=False) + "\n"


@app.post("/api/intake/stream")
def intake_stream(body: IntakeIn):
    """与 `/api/intake` 同一套逻辑，但**逐行推送真实进度**（NDJSON，一行一个 JSON）。

    为什么要有它：一次导入十几张票要跑几分钟。若只在最后回一句结果，用户全程干等，
    分不清是在跑还是卡住了。这里每处理一个文件推两行，前端立刻能看到：
    - `{"stage":"start"}`     —— 开始处理第 i 个（前端显示「正在读文件 i/n：xxx」）
    - `{"stage":"tick"}`      —— 这个文件还在跑，附**真实已等秒数**与正在做的动作。
      它的作用是让流别长时间静默：真机实测一张报销表要 239 秒，静默超过一定时间
      会被浏览器 / 代理按空闲超时掐断，用户只看到「network error」。心跳只报真实信息。
    - `{"stage":"file_done"}` —— 这个文件已读完、草稿已生成（含金额与条数）
    - `{"stage":"file_failed"}` —— 单个文件失败，不中断整批，如实报出来
    - `{"stage":"all_done"}`  —— 按项目合并后的汇总（前端把它换成正式结果）

    逐个文件跑而不是整批跑，是因为**报销表的双源对账是在单个表文件内部做的**，
    拆开不会破坏对账口径。进度全部来自真实处理结果，不做假进度条。
    **取消导入**：前端 abort 这个请求即可（见 `_intake_stream_events` 的说明）。
    """
    files = _intake_decode(body)
    return StreamingResponse(_intake_stream_events(files, body.project or None, body.reconcile),
                             media_type="application/x-ndjson")


@app.get("/api/pending")
def pending_list():
    """待确认草稿列表（识别 → 确认 → 入账）。

    项目名在**读取时**从映射表解析，不写进草稿：草稿只存稳定 id，
    这样项目改名后草稿跟着显示新名（与账本同一条纪律：只存 id、显示名走映射表），
    项目被删掉时也如实回落成「未归项目」，与账本事件的回落口径一致。
    """
    items = pending.list_all(DATA_DIR)
    for d in items:
        d["project_name"] = _project_label(d.get("project", ""))
    return {"pending": items}


@app.post("/api/pending/{pid}/accept")
def pending_accept(pid: str, body: PendingAcceptIn | None = None):
    """确认入账：写入事件账本。

    `body.project` 可选（2026-09-26，欠账 E 组 18）：**用户在确认那一刻改归属**，
    可传项目 id 或完整名；空字符串 ＝ 就是未归项目。不传则沿用草稿生成时的项目。
    """
    override = body.project if body is not None else None
    if override is not None:
        # ⚠️ 必须先把"名字"解析成 id 再写账本：`_append_event` 假定拿到的是已解析好的 id
        # （账本只存稳定 id，显示名走映射表）。传名字就写，会存进一个不存在 id 的幽灵值。
        override = projects.resolve_incoming(DATA_DIR, override)[0] if override.strip() else ""
    # 分类同理可在确认那一刻改：草稿分类是「待确认」（机器没判出来）时就该由人补上。
    # `pending.accept` 内部还会把占位符收成「其他」，账本里永远不出现「待确认」这个"分类"。
    # ⚠️ **空字符串 ＝ 用户没改分类，不是"把分类清空"**：草稿识别出来的分类必须留住。
    # 2026-09-28 真机踩到：前端两处都无条件发 category: ""（没动过下拉也是空串），
    # 于是「待确认」里每确认一笔，草稿分类就被冲成「其他」—— 18 笔全变「其他」。
    # （project 那边不一样：空字符串有明确含义＝未归项目，所以那里不能这么收。）
    cat_override = (body.category or "").strip() if body is not None else None
    if not cat_override:
        cat_override = None
    draft = pending.get(DATA_DIR, pid) or {}
    strict = draft.get("source") in _FILE_DRAFT_SOURCES
    append = functools.partial(_append_event, strict_dedupe=strict)
    out = pending.accept(DATA_DIR, pid, append, project_override=override,
                         category_override=cat_override)
    if out is None:
        raise HTTPException(status_code=404, detail="待确认草稿不存在")
    state = run_state(LEDGER_PATH, DB_PATH)["state"]
    return {"ok": True, "appended": out["appended"], "draft": out["draft"],
            "project_name": _project_label((out.get("event") or {}).get("project", "")),
            "state": state}


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
    # 本机登录名：三条路（LLM 解析 / LLM 润色 / 规则兜底）都要带上，
    # 否则「我是谁」在有 Key 和没 Key 两种档位下会给出两个不同答案。
    user_ctx = _user_context(body.user)

    if llm_skill.is_configured():
        parsed = llm_skill.parse_accounting(prompt, user_ctx)
        if parsed is not None:
            run = _run_actions(parsed, body.text, body.project)
            if run["executed"]:
                if run.get("decision"):
                    # 决策类：文案直接用确定性引擎产出的那句，**不经过 compose_reply**
                    # —— 数字不许被 LLM 复述或重算（红线：宁可说不知道，绝不编造结论）。
                    final = "\n".join(run["parts"])
                else:
                    results_json = json.dumps(run["results"], ensure_ascii=False, default=str)[:2200]
                    final = ""
                    try:
                        final = llm_skill.compose_reply(body.text, results_json, context=user_ctx)
                    except Exception:
                        final = ""
                    if not final or len(final) < 8:
                        final = "\n".join(run["parts"])
                # 查询答复要如实标注口径范围（查询始终是全账本，不跟随选中项目）
                if any(str(x.get("action", "")).startswith("ask_") for x in run["results"]):
                    note = _scope_note(body.project)
                    if note:
                        final = f"{final}\n{note}"
            else:
                final = str(parsed.get("reply", "")).strip() or "我还在学习这句怎么接——可以记一笔账，或问我最近流水 / 现金流。"
            session_mem.add_turn(sid, body.text, final, result_note=final[:120])
            out = {"ok": True, "text": final, "session_id": sid,
                   "pending": [{"id": d["id"], "direction": d["direction"],
                                "amount_cents": d["amount_cents"], "category": d["category"],
                                "channel": d["channel"], "project": d.get("project", ""),
                                "project_name": _project_label(d.get("project", ""))}
                               for d in run["drafts"]]}
            if run.get("decision"):
                out["decision"] = run["decision"]
            return out

    r = _rule_reply(body.text, body.project, body.user)
    session_mem.add_turn(sid, body.text, r.get("text", ""))
    r["session_id"] = sid
    return r
