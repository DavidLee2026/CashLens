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
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .services import (amount_guard, categories, data_reset, evidence_files, expense_list,
                       export_xlsx, intake,
                       invoice_tax, llm_skill, model_config, parser_rule, pending, projects,
                       query_tools, reconcile as reconcile_svc, session_mem, tenancy,
                       timesheet, timesheet_records)
from .services.state_engine import (EventLedger, Projection, calc, decision as decision_engine,
                                    make_event, run_state)

REPO_ROOT = Path(__file__).resolve().parents[2]


# 路径全部按「当前请求」解析（2026-10-19 多租户改造）：
# 单用户/本地开发时 = CASH_DATA_DIR 本身；CASH_MULTI_TENANT=1 时 = 当前邀请码的租户目录。
# 服务层本来就接收 data_dir 参数，所以只改这里的取值来源，不动任何服务签名。
def data_dir() -> Path:
    return tenancy.data_dir()


def ledger_path() -> Path:
    return tenancy.ledger_path()


def db_path() -> Path:
    return tenancy.db_path()


def _mem_sid(sid: str) -> str:
    """会话记忆按租户分开。

    起因（2026-10-19）：聊天会话 id 存在浏览器 localStorage 里，同一台电脑换一个
    邀请码登录时，旧 id 还在 —— 后端若按裸 session_id 存上下文，新租户会读到
    上一个租户的对话。key 前缀用 slug（随机 8 位十六进制，目录名同源），
    单用户模式不加前缀，行为与改造前一致。
    """
    slug = tenancy.current_slug()
    return f"{slug}:{sid}" if tenancy.enabled() and slug else sid


def _source_mtime() -> int:
    """`backend/app/**/*.py` 里**最新**的修改时间（epoch 秒），导入时算一次。

    给启动脚本用：它拿这个值（= 进程启动时代码的样子）与磁盘上的当前值比，
    一旦磁盘更新就说明**这个进程在跑旧代码**，于是停掉它重起。
    起因（2026-09-29 真机）：重跑 `启动-CashLens.command` 时，它只校验
    `/api/health` 的账本路径与 `/api/categories` 返回 200 —— 旧进程两条都满足，
    于是打印「直接复用（不再启动新的后端）」，**新加的 `/api/reset` 一直 404**，
    表现为「重置失败：Not Found」，而且怎么重启都还是旧的。
    """
    newest = 0.0
    for p in Path(__file__).resolve().parent.rglob("*.py"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return int(newest)


SOURCE_MTIME = _source_mtime()

class TenantMiddleware:
    """多租户鉴权 + 当前请求用哪个账本（2026-10-19）。

    - **只在 `CASH_MULTI_TENANT=1` 时生效**；关着就直通，本地行为与改造前一致。
    - 把会话 cookie 解成 slug 放进 contextvar，`data_dir()` 据此选目录。
    - `/api/*` 里除白名单外，没有有效会话一律 401 —— 前端看到 401 就弹邀请码页。
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not tenancy.enabled():
            await self.inner(scope, receive, send)
            return
        slug = tenancy.verify_token(_cookie_from(scope, tenancy.COOKIE_NAME))
        tenancy.set_current(slug)
        try:
            path = scope.get("path", "")
            if path.startswith("/api/") and slug is None and not _session_free(path):
                body = json.dumps({"detail": "需要邀请码才能使用", "code": "tenant_required"},
                                  ensure_ascii=False).encode("utf-8")
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            await self.inner(scope, receive, send)
        finally:
            tenancy.reset_current()


def _cookie_from(scope, name: str) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v.strip()
    return None


def _session_free(path: str) -> bool:
    return path in tenancy.ALLOW_EXACT or path.startswith(tenancy.ALLOW_PREFIXES)


app = FastAPI(title="CashLens API", version="0.1")
app.add_middleware(TenantMiddleware)
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
    """健康检查 + **代码新鲜度**（source_mtime 供启动脚本判断"这个进程是不是旧代码"）。"""
    return {"ok": True, "api": "cashlens", "ledger": str(ledger_path()),
            "source_mtime": SOURCE_MTIME}


class RedeemIn(BaseModel):
    """邀请码兑换会话。"""
    code: str = ""


@app.post("/api/session/redeem")
def session_redeem(body: RedeemIn, request: Request, response: Response):
    """用邀请码换一个 30 天会话。

    失败时**不区分**「码不存在」「码已停用」「抄错了」—— 只回一句「不对或已停用」，
    不给枚举线索；同一个来源 10 分钟内错 8 次就先拦住（码本身够长，这层是兜底）。
    """
    if not tenancy.enabled():
        return {"ok": True, "multi_tenant": False, "label": ""}
    who = request.client.host if request.client else "unknown"
    if tenancy.too_many_attempts(who):
        raise HTTPException(status_code=429, detail="尝试次数太多，请 10 分钟后再试")
    slug = tenancy.verify_code(body.code)
    if not slug:
        tenancy.note_attempt(who)
        raise HTTPException(status_code=401, detail="邀请码不对，或者已经停用")
    tenancy.clear_attempts(who)
    tenancy.touch(slug)
    tenancy.set_current(slug)
    row = tenancy.get_tenant(slug) or {}
    response.set_cookie(tenancy.COOKIE_NAME, tenancy.issue_token(slug),
                        max_age=tenancy.SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=tenancy.session_cookie_secure(), path="/")
    return {"ok": True, "multi_tenant": True, "label": row.get("label", "")}


@app.get("/api/session/me")
def session_me():
    """当前会话是谁。前端启动时调它决定「显示工作台」还是「弹邀请码页」。"""
    if not tenancy.enabled():
        return {"multi_tenant": False, "label": "", "tenant": "", "active": True}
    slug = tenancy.current_slug()
    row = tenancy.get_tenant(slug) if slug else None
    if not row:
        raise HTTPException(status_code=401, detail="需要邀请码")
    # tenant 给前端做本地缓存分区用（消息 / 会话 id 按租户分开）；
    # 它不是凭证 —— 真正的凭证是 HttpOnly 的会话 cookie。
    return {"multi_tenant": True, "label": row.get("label", ""),
            "tenant": slug or "", "active": bool(row.get("active"))}


class RenameIn(BaseModel):
    """改当前邀请码的显示名（只改称呼，不动账本）。"""

    label: str = ""


@app.post("/api/session/rename")
def session_rename(body: RenameIn):
    """用户登录后改自己的显示名。

    只改 `tenants.json` 里的 label —— 账本、租户目录、slug、会话签名都不动。
    名字同时用于右上角与「这本账」的称呼；不想暴露真名也可以填公司名/代号。
    """
    if not tenancy.enabled():
        raise HTTPException(status_code=400, detail="单用户模式不需要改显示名")
    slug = tenancy.current_slug()
    if not slug:
        raise HTTPException(status_code=401, detail="需要邀请码")
    label = (body.label or "").strip()
    if not label:
        raise HTTPException(status_code=422, detail="名字不能为空")
    if len(label) > 20:
        raise HTTPException(status_code=422, detail="名字最多 20 个字")
    new = tenancy.rename(slug, label)
    if not new:
        raise HTTPException(status_code=404, detail="找不到当前账本")
    return {"ok": True, "label": new}


@app.post("/api/session/logout")
def session_logout(response: Response):
    """退出：清掉会话 cookie（账本一根手指都不动）。"""
    response.delete_cookie(tenancy.COOKIE_NAME, path="/")
    tenancy.reset_current()
    return {"ok": True}


class ResetIn(BaseModel):
    """重置本机演示数据。`seed_demo=True` 时顺带载入合成演示样本（录视频开局一致）。"""

    seed_demo: bool = False


@app.post("/api/reset")
def reset_data(body: ResetIn = ResetIn()):
    """把本机演示数据重置为空账（可选载入演示样本）。

    ⚠️ **不是删文件**：当前整套状态先搬进 `data/_reset_backup_<ts>/`，再起一套空账，
    守住「账本可追溯、误删可救」这条红线。模型档位配置（model_config.json）不动。
    返回 before/after 两套计数与备份路径，界面据此如实回报（2026-09-29）。
    """
    # 旧会话上下文不能渗进新对话。多租户下只清当前租户的 key，
    # 不能把别人正在聊的上下文一起清掉（2026-10-19 隔离修复）。
    slug = tenancy.current_slug()
    if tenancy.enabled() and slug:
        session_mem.clear_prefix(f"{slug}:")
    else:
        session_mem.clear_all()
    try:
        return data_reset.reset(data_dir(), seed_demo=body.seed_demo)
    except FileNotFoundError as e:
        # 演示样本是**本机生成**的（`.gitignore` 的 `data/` 规则把 samples 下的 data 目录也挡住了，
        # 所以它不在版本库里）。缺了就如实说清怎么办 —— 不能让前端笼统报成"后端没在运行"。
        raise HTTPException(status_code=409, detail=(
            f"{e}。演示样本是本机生成的、不入版本库；请先在 09-代码 目录运行 "
            f"python3 scripts/make_synthetic_business_ledger.py 生成，"
            f"或改用不载入样本的「重置本机数据」。"))


@app.post("/api/events")
def add_event(ev: EventIn):
    """写一笔到事件账本（dedupe 幂等），返回写结果 + 最新状态。

    project 留空时归入当前默认项目（没有就建 Project A），并在返回里附一句名称提醒。
    """
    ts = datetime.fromisoformat(ev.ts) if ev.ts else datetime.now()
    pid, project_hint = projects.resolve_incoming(data_dir(), ev.project)
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
    book = EventLedger(ledger_path())
    result = book.append(event, strict_dedupe=True)
    state = run_state(ledger_path(), db_path())["state"]
    out = {"appended": result["appended"], "event": event, "state": state}
    if project_hint:
        out["project_hint"] = project_hint
    return out


@app.get("/api/export/xlsx")
def export_ledger_xlsx(project: str = ""):
    """导出账目为 Excel（明细 + 汇总）。project 留空＝全部项目。

    口径与工作台**完全一致**：复用 `projects.summary()`，同项目内同号发票只计一次，
    被剔除的行在明细里标出来、汇总里单列一行（不许悄悄删）。
    """
    from urllib.parse import quote
    try:
        data, filename = export_xlsx.build_workbook(data_dir(), _events(), project=project)
    except ValueError as e:  # 项目名解析不到
        raise HTTPException(status_code=404, detail=str(e)) from e
    disposition = (f"attachment; filename=\"account.xlsx\"; "
                   f"filename*=UTF-8''{quote(filename)}")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


@app.get("/api/events")
def list_events(limit: int = 50):
    """最近事件（倒序展示用）。"""
    events = EventLedger(ledger_path()).load()
    evidence_files.attach(events, evidence_files.load_map(data_dir()))
    return {"total": len(events), "events": list(reversed(events[-limit:]))}


@app.get("/api/state")
def get_state():
    """当前财务状态（真计算：asOf 重放）。"""
    out = run_state(ledger_path(), db_path())
    evidence_files.attach(out.get("events") or [], evidence_files.load_map(data_dir()))
    return out


@app.get("/api/forecast")
def get_forecast(horizon_days: int = 30):
    """未来现金流区间（90% 置信带，真计算）。"""
    events = EventLedger(ledger_path()).load()
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
    return EventLedger(ledger_path()).load()


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
    book = EventLedger(ledger_path())
    res = book.append(ev, strict_dedupe=strict_dedupe)
    return ev, res["appended"]


def _project_label(pid: str) -> str:
    """project id → 显示名；找不到、为空、或**项目已删**时回落成「未归项目」。

    已删项目要回落，是为了和账本视图口径一致：账本里已删项目名下的事件在视图里
    也回落成「未归项目」（见 `projects.detail`）。否则同一笔钱在草稿和账本里
    会显示成两个不同的归属（2026-09-28 修：此前草稿挂在已删项目上会照旧显示旧名）。
    """
    row = projects.get(data_dir(), pid) if pid else None
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
    pid = projects.resolve(data_dir(), s) or s
    return (f"（口径提示：以上数字是全部项目的合计——总账户；"
            f"当前选中只是让新账默认记入「{_project_label(pid)}」，查询不分项目）")


def _text_channel(raw: str | None) -> str:
    """这笔是**打字**进来的，不是语音（2026-10-19 修）。

    现象：界面里明明没有语音输入，三笔手动记的收入却显示「语音」。根因是提示词把
    `voice` 列成了 channel 的可选值，模型看着「说一句话记账」的语境就猜成了 voice，
    而规则解析那条路给的是 manual/wechat —— 同一句换个解析方式，来源标签就变了。

    口径：**输入途径服务端比模型清楚**。模型说 voice 一律落 manual；
    微信 / 支付宝 / 现金 / 银行卡 是**付款渠道**（文字里真说了），保留原样。
    以后真做语音输入，由请求显式带 channel，不再让模型猜。
    """
    ch = (raw or "").strip()
    if not ch or ch == "voice":
        return "manual"
    return ch


# 「这笔记到 X / 记入 X / 归到 X」这类句式（2026-10-19 立）
_PROJECT_HINT_RE = re.compile(
    r"(?:这?笔记(?:到|入|在)|记(?:到|入|在)|算(?:到|入)|归(?:到|入)|属于|挂在|划到)"
    r"[\s「“\"']*([^，,。；;！!？?、「」“”\"'\s]{1,20})"
)


def _project_from_text(text: str, known: list[str] | None = None) -> str | None:
    """从一句话里取项目名（确定性，不问模型）。

    起因（2026-10-19 真机）：David 第二次说「收到尾款 5200，这笔记到秋季市集」，
    草稿却落到「未归项目」—— 因为项目归属**完全靠模型抽**，这一次它没抽出来。
    这与 9/13 工时那次是同一个病：**结构性字段不能让模型猜**。

    三条规矩：
    1. 先认句式（这笔记到 / 记入 / 归到 / 属于…）；
    2. 句式没命中，就看文字里有没有**已存在的项目名**（长的优先，免得截短）；
    3. 候选名字若是某个已知项目名的延长（如「秋季市集项目」），落回那个已知项目 ——
       `projects.resolve` 只做**精确**匹配，多一个字就会新建出一个重复项目。
    """
    t = (text or "").strip()
    if not t:
        return None
    known = [n for n in (known or []) if n]

    def _snap(cand: str) -> str:
        c = cand.strip("「」“”\"' 　")
        for name in sorted(known, key=len, reverse=True):
            if c == name or c.startswith(name) or name in c:
                return name
        return c

    m = _PROJECT_HINT_RE.search(t)
    if m:
        cand = _snap(m.group(1))
        if cand:
            return cand
    for name in sorted(known, key=len, reverse=True):
        if name in t:
            return name
    return None


def _known_project_names() -> list[str]:
    try:
        return [str(r.get("name") or "") for r in projects.visible_rows(data_dir())]
    except Exception:  # noqa: BLE001  读不到就当没有已知项目，不阻断记账
        return []


def _draft_one(direction: str, amount_cents: int, category: str, channel: str, note: str,
               counterparty: str = "", project: str | None = None) -> dict:
    """识别 → 只生成待确认草稿（不直接入账）。

    project 可传 id 或显示名（"当前选中的项目"也走这里）；**留空 ＝ 未归项目（总账户）**，
    不再落到"表里第一个项目"（见 `projects.resolve_incoming` 的 2026-09-26 改动）。
    """
    pid, hint = projects.resolve_incoming(data_dir(), project)
    d = pending.create(
        data_dir(), direction=direction, amount_cents=amount_cents,
        category=category, channel=channel, note=note, counterparty=counterparty,
        project=pid, occurred_at=datetime.now().isoformat(timespec="seconds"),
    )
    if hint:
        d["project_hint"] = hint
    return d


def _project_line(d: dict) -> str:
    """草稿落哪个项目的那句话（优先用 resolve 时给的提醒原文）。"""
    return str(d.get("project_hint") or f"归入项目「{_project_label(d.get('project', ''))}」。")


def _draft_label(d: dict, limit: int = 22) -> str:
    """逐条列草稿时的名字：用原话第一行（「3个n6三脚架」），太长就截断。"""
    text = str(d.get("note") or "").strip().splitlines()
    label = (text[0] if text else "").strip() or str(d.get("counterparty") or "").strip()
    if not label:
        return ""
    return (label[:limit] + "…") if len(label) > limit else label


def _draft_block(drafts: list[dict]) -> tuple[list[str], dict]:
    """一批草稿的**确定性**文案，合计在这里算（2026-10-20 立）。

    起因（第一个真人客户的真机）：12 行清单让他「帮我合计」，12 条草稿每一笔都对，
    前端相加是 ¥9,980.00，可那句话里的合计是模型自己加的（8980，少了一个 1000）。
    同一屏两个数，用户不知道信哪个。**合计从此只由这里算**，模型不复述。

    单笔：沿用原来的一句话（识别到支出 ¥X（分类 · 渠道）——请确认后入账。归入…）。
    多笔：先报「几笔 + 合计」，再逐条列名字与金额，最后一句落到归属。
    """
    n = len(drafts)
    exp = sum(int(d.get("amount_cents") or 0) for d in drafts if d.get("direction") == "expense")
    inc = sum(int(d.get("amount_cents") or 0) for d in drafts if d.get("direction") == "income")
    totals = {"count": n, "expense_cents": exp, "income_cents": inc}
    if n == 1:
        d = drafts[0]
        dir_cn = "收入" if d["direction"] == "income" else "支出"
        return ([f"识别到{dir_cn} {_yuan(d['amount_cents'])}（{d['category']} · {d['channel']}）"
                 f"——请确认后入账。{_project_line(d)}"], totals)
    kind_cn = "支出" if not inc else ("收入" if not exp else "")
    head = f"识别到 {n} 笔{kind_cn}"
    lines = [f"{head} —— 都还没入账，逐笔确认后才进账本："]
    for d in drafts:
        label = _draft_label(d)
        lines.append(f"· {label}{' ' if label else ''}{_yuan(d['amount_cents'])}（{d['category']}）")
    bits = []
    if exp:
        bits.append(f"支出 {_yuan(exp)}")
    if inc:
        bits.append(f"收入 {_yuan(inc)}")
    lines.append(f"合计 {' / '.join(bits)}（{n} 笔）。{_project_line(drafts[0])}")
    return lines, totals


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
    # 工时/工分：**规则层先认**（不依赖 LLM）——
    # 「阿明今天 8 小时，时薪 50」这类说法很规整，正则就能确定性地读出来；
    # 认不出才交给记账规则（避免把「打车 28 元」之类的钱误当工时）。
    ts = timesheet_records.parse_text(text)
    if ts["entries"]:
        parts, summary = _record_timesheet(ts["entries"], current_project, text)
        return {"ok": True, "text": "\n".join(parts), "timesheet": summary}
    if ts["contract"]:
        return {"ok": True, "text": _CONTRACT_REFUSAL, "timesheet": None}
    r = parser_rule.analyze(text)
    if r["kind"] == "fallback":
        return {"ok": False, "text": "这句我先接不上：说一句带金额的记账（如「昨天微信收了 3000 尾款」），或问「最近一笔收入 / 这个月花了多少 / 现金流怎么样」。配置 LLM API Key 后可自由对话。"}
    if r["kind"] == "ask_cashflow":
        out = _cashflow_text()
        note = _scope_note(current_project)
        return {"ok": True, "text": out["text"] + (f"\n{note}" if note else ""),
                "state": out.get("state"), "forecast": out.get("forecast")}
    d = _draft_one(r["direction"], r["amount_cents"], r["category"], r["channel"], r["text"],
                   project=_project_from_text(text, _known_project_names()) or current_project or None)
    dir_cn = "收入" if r["direction"] == "income" else "支出"
    return {
        "ok": True,
        "text": f"识别到{dir_cn} {_yuan(r['amount_cents'])}（{r['category']} · {r['channel']}）——请确认后入账。{_project_line(d)}",
        "pending": [d],
    }


_CONTRACT_REFUSAL = (
    "合同工这条我暂时接不了：合同工要算社保公积金（交金），规则和外包完全不同，"
    "按外包那套算出来的数会误导你。这一版只做**外包（按小时）**——"
    "你说「阿明 8 小时，时薪 50」我就能记；合同工的口径等我按真实规则做完再上。"
)


def _record_timesheet(entries: list[dict], current_project: str, text: str) -> tuple[list[str], dict]:
    """把解析出来的工时/工分条目**追加进工时表**（外包按小时），并返回最新的汇总表。

    ⚠️ 只写 `data/timesheet.jsonl`，**不写账本**：工时/工分不是钱，应付金额是参考值，
    要入账必须用户另行确认（与「工时表」那条路同一口径）。
    """
    for e in entries:
        timesheet_records.append(data_dir(), person=e.get("person") or "（没写名字）",
                                 qty=e.get("qty") or 0, unit=e.get("unit") or "hours",
                                 rate_cents=e.get("rate_cents"),
                                 kind=e.get("kind") or "outsourced",
                                 project=e.get("project") or current_project or "",
                                 date=e.get("date") or "", source="chat", note=text)
    summary = timesheet_records.summarize(timesheet_records.list_all(data_dir()))
    unit_cn = "工分（点数）" if summary["unit"] == "points" else "工时（小时）"
    names = "、".join(f"{e.get('person') or '（没写名字）'} {e.get('qty')}"
                     f"{'分' if (e.get('unit') == 'points') else ' 小时'}" for e in entries)
    parts = [f"记下这一笔（外包口径 · {unit_cn}）：{names}。这一步只进工时表，不入账；"
             f"要按它付钱/入账，跟我说一声，我列成「待确认」给你点。"]
    return parts, summary


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
        out = decision_engine.decide(evs, order, projects=projects.visible_rows(data_dir()))
        lines = [f"{out['decision']}｜{out['headline']}"]
        # reasons[0] 就是 headline 本身（引擎里第一条理由重复了结论）：这里过滤掉，
        # 否则聊天回复会把同一句话印两遍（前端卡片早就 filter 了，聊天这条路一直没滤）。
        seen = [r for r in out["reasons"] if r["text"] != out["headline"]][:3]
        lines += [f"· {r['text']}" for r in seen]
        if out["min_price_cents"]:
            lines.append(f"· 报价下限 {_yuan(out['min_price_cents'])}；"
                         f"推荐 {_yuan(out['quote']['recommend_cents'])}、"
                         f"上限 {_yuan(out['quote']['ceiling_cents'])}")
        lines.append(f"· 主要风险：{out['risk']}")
        lines.append(out["disclaimer"])
        return {"text": "\n".join(lines), "payload": {"decision": out}}

    def h_record_timesheet(evs, act):
        """记工时/工分（外包按小时）；合同工如实拒绝，不按外包口径硬算。"""
        if str(act.get("person_kind") or "").lower() in ("contract", "合同工"):
            return {"text": _CONTRACT_REFUSAL, "payload": {"refused": "contract"}}
        entries = [{
            "person": str(act.get("person") or "").strip(),
            "qty": float(act.get("qty") or 0),
            "unit": "points" if str(act.get("unit") or "hours") == "points" else "hours",
            "rate_cents": int(act["rate_cents"]) if act.get("rate_cents") else None,
            "project": str(act.get("project") or ""),
            "date": str(act.get("date") or ""),
        }]
        if not entries[0]["person"] or entries[0]["qty"] <= 0:
            return {"text": "这条我没读全：给我「谁 + 多少小时」就行，例如「阿明 8 小时，时薪 50」。",
                    "payload": {}}
        parts, summary = _record_timesheet(entries, current_project, text)
        return {"text": "\n".join(parts), "payload": {"timesheet": summary}}

    def h_pending_total(evs, act):
        """「待确认」合计：**只数草稿，不碰账本**，也不是预测（2026-10-20 立）。

        起因：戚老师问「帮我算个总价 / 每一行空格之后的数字需要合计」——这种问题以前只能
        让模型自己加，于是出现了 8980 与 9980 两个数。合计从此走这里，金额由代码加。
        """
        items = pending.list_all(data_dir())
        exp = sum(int(d.get("amount_cents") or 0) for d in items if d.get("direction") == "expense")
        inc = sum(int(d.get("amount_cents") or 0) for d in items if d.get("direction") == "income")
        if not items:
            text = "「待确认」里现在一笔都没有，没有可合计的金额。"
        else:
            bits = [f"共 {len(items)} 笔"]
            if exp:
                bits.append(f"支出合计 {_yuan(exp)}")
            if inc:
                bits.append(f"收入合计 {_yuan(inc)}")
            text = ("「待确认」里" + "，".join(bits)
                    + " —— 都还没入账，逐笔确认后才进账本。")
        return {"text": text, "payload": {"count": len(items), "expense_cents": exp,
                                          "income_cents": inc}}

    handlers = {
        "ask_cashflow": h_cashflow,
        "record_timesheet": h_record_timesheet,
        "ask_latest_income": h_latest_income,
        "ask_latest_expense": h_latest_expense,
        "ask_spending": h_spending,
        "ask_recent": h_recent,
        "ask_decision": h_decision,
        "ask_pending_total": h_pending_total,
    }
    parts: list[str] = []
    results: list[dict] = []
    drafts: list[dict] = []
    draft_slot: int | None = None
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
            d = _draft_one(direction, amount, a.get("category"), _text_channel(a.get("channel")),
                           a.get("note") or text, a.get("counterparty", ""),
                           _project_from_text(text, _known_project_names())
                           or a.get("project") or current_project or None)
            if draft_slot is None:
                draft_slot = len(parts)  # 草稿文案按「这一批」整体插回原位置（保持与查询交错的顺序）
            drafts.append(d)
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
    totals: dict = {}
    if drafts:
        block, totals = _draft_block(drafts)
        pos = draft_slot if draft_slot is not None else len(parts)
        parts[pos:pos] = block
        # 把「代码算的合计」也交给模型：它要提合计就照抄，不许自己加（`amount_guard` 会核对）
        results.append({"action": "record_total", **totals,
                        "note": "合计由系统计算：要提合计就照抄 expense_cents/income_cents，不要自己加减"})
    # 决策类结果单独拎出来给前端渲染决策卡（其余查询结果不额外透出）
    decision_payload = next(
        (r["data"]["decision"] for r in results
         if r.get("action") == "ask_decision"
         and isinstance(r.get("data"), dict) and "decision" in r["data"]),
        None,
    )
    timesheet_payload = next(
        (r["data"]["timesheet"] for r in results
         if r.get("action") == "record_timesheet"
         and isinstance(r.get("data"), dict) and "timesheet" in r["data"]),
        None,
    )
    return {"parts": parts, "executed": executed, "results": results, "drafts": drafts,
            "totals": totals, "decision": decision_payload, "timesheet": timesheet_payload}


def _skipped_line(skipped: list[str], limit: int = 3) -> str:
    """清单里没标金额的行 —— 如实点名，别让用户以为系统把它们吃了。"""
    shown = "、".join(s[:14] + ("…" if len(s) > 14 else "") for s in skipped[:limit])
    more = f" 等 {len(skipped)} 行" if len(skipped) > limit else ""
    return f"另有 {len(skipped)} 行没标金额，没计入合计（{shown}{more}）—— 补上数字我再帮你加。"


def _reply_expense_list(lst: dict, body, sid: str, mem_sid: str) -> dict:
    """一份「清单」的确定性回复：逐行落草稿 + 代码算合计（2026-10-20 立）。

    不经过模型：金额、条数、合计全部由 `services/expense_list.py` 与 `_draft_block()` 给出。
    这样即使当前档位是「不用模型」，或者模型这次没开窍，用户也不用先解释一遍格式。
    """
    project_ref = (_project_from_text(body.text, _known_project_names())
                   or body.project or None)
    drafts = [_draft_one(it["direction"], it["amount_cents"], it["category"], "manual",
                         it["label"], project=project_ref)
              for it in lst["items"]]
    block, _totals = _draft_block(drafts)
    lines = list(block)
    if lst.get("skipped"):
        lines.append(_skipped_line(lst["skipped"]))
    if lst.get("context_filled"):
        # 分类里有一部分是"跟着这份清单的性质"给的，必须说清，别让它看起来像逐条判出来的
        lines.append(f"（其中 {lst['context_filled']} 条按「这份清单是项目成本」整体归到经营，"
                     f"不对可以在「待确认」里改分类）")
    final = "\n".join(lines)
    session_mem.add_turn(mem_sid, body.text, final, result_note=final[:120])
    return {"ok": True, "text": final, "session_id": sid,
            "pending": [{"id": d["id"], "direction": d["direction"],
                         "amount_cents": d["amount_cents"], "category": d["category"],
                         "channel": d["channel"], "project": d.get("project", ""),
                         "project_name": _project_label(d.get("project", ""))}
                        for d in drafts],
            "list": {k: lst[k] for k in ("items", "skipped", "total_cents",
                                         "expense_cents", "income_cents")}}


def _guard_reply(reply: str, run: dict, user_text: str) -> str:
    """模型写的这句话，金额与合计必须出自本地引擎；不过闸就整条作废。

    2026-10-20 立（第一个真人客户 · 戚老师）：12 行清单让「帮我合计」，草稿 12 笔全对
    （前端相加 ¥9,980.00），但模型自己在话里加成了 8980 —— 少了一个 1000，两个数同屏。
    闸门规则见 `services/amount_guard.py`：带货币标记的数必须在系统给过的数里；
    说了合计就必须等于代码算出的合计。

    **为什么不是"改提示词让它别算"**：提示词已经写了「金额只来自真数据」，它还是算了 ——
    这类错只能靠确定性闸门兜，不能靠嘱咐。
    """
    allowed = amount_guard.allowed_sets(
        "\n".join(run.get("parts") or []),
        drafts=run.get("drafts") or (),
        totals=run.get("totals") or {},
        user_text=user_text,
    )
    verdict = amount_guard.check(reply, allowed["cents"], allowed["totals"], allowed["counts"])
    if verdict["ok"]:
        return reply
    return "\n".join(run.get("parts") or []) or reply


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
    return decision_engine.decide(_events(), order, projects=projects.visible_rows(data_dir()))


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
    if model_config.locked():
        raise HTTPException(status_code=403,
                            detail="服务器已锁定模型档位，请在服务器的环境变量里修改")
    try:
        return model_config.select(tier=body.tier, base_url=body.base_url, model=body.model,
                                   api_key=body.api_key, preset_id=body.preset_id,
                                   declared_vision=body.declared_vision)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/models/test")
def models_test():
    """按当前档位做一次连通性自检。只发一句固定文本，不发送任何用户数据。"""
    if model_config.locked():
        raise HTTPException(status_code=403,
                            detail="服务器已锁定模型档位，不能从界面触发连通性测试")
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
    """建项目 / 改项目名。id 留空 = 新建；带 id（或显示名）+ name = 重命名。

    `reuse`：同名项目已存在时**直接返回那一个**，不再新建（默认 false，保持"新建就是新建"）。
    只给「导入前替他建默认项目」这条路用 —— 否则同一天导两批就会出现两个「默认项目 10-20」。
    """

    name: str = ""
    id: str = ""
    reuse: bool = False


class ReorderIn(BaseModel):
    """项目拖动排序：按数组顺序落盘（前端松手时一次性发全量）。"""

    ids: list[str] = []


@app.get("/api/projects")
def projects_list():
    """项目清单（含每个项目的收支汇总）+ 未归项目一行。

    项目是账本事件的属性维度：真实项目（如 示例项目 A），不是费用类别（打车/吃饭）。
    """
    return projects.list_projects(data_dir(), _events())


@app.post("/api/projects/reorder")
def projects_reorder(body: ReorderIn):
    """保存左侧项目列表的拖动顺序。

    只改 projects.json 里的 order 字段，账本与项目名都不动。
    """
    if not body.ids:
        raise HTTPException(status_code=422, detail="没有传项目顺序")
    try:
        projects.reorder(data_dir(), body.ids)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    view = projects.list_projects(data_dir(), _events())
    return {"ok": True, "order": [p["id"] for p in view["projects"]],
            "projects": view["projects"]}


@app.post("/api/projects")
def projects_upsert(body: ProjectIn):
    """建项目或重命名。

    账本只存稳定 id，显示名存在 data/projects.json 映射表里；
    因此重命名只改映射，账本一字不动（守住账本不可变）。
    """
    if body.id:
        pid = projects.resolve(data_dir(), body.id)
        if pid is None:
            raise HTTPException(status_code=404, detail=f"项目不存在：{body.id}")
        try:
            row = projects.rename(data_dir(), pid, body.name)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "action": "renamed", "project": row}
    if body.reuse:
        wanted = str(body.name or "").strip()
        for r in projects.visible_rows(data_dir()):
            if str(r.get("name") or "").strip() == wanted:
                return {"ok": True, "action": "reused", "project": r}
    return {"ok": True, "action": "created", "project": projects.create(data_dir(), body.name)}


def _reassign_events(ids: list[str], target_pid: str) -> dict:
    """把一批已入账事件的**归属**改到 target_pid（追加式订正，不重写历史）。

    为什么不是"改一下那个字段"：账本是追加式事件流，事件的 project 就是它的归属，
    直接改历史等于让"这笔钱当时记在哪"无从追溯。所以按同一套订正写法走：
    **追加一条作废记录（void）+ 按新项目重记一笔**，两笔都留在账本里，谁改的、从哪改到哪、
    什么时候改的都能查（`reason` 里写明原归属与新归属）。

    ts **沿用原事件的交易时间**（不能换成 now）—— 否则改一次归属，这笔钱就从"上周"
    跳到"今天"，月度统计会跟着变。金额/分类/渠道/对手方/备注原样保留。
    """
    book = EventLedger(ledger_path())
    live = {str(e.get("event_id")): e for e in book.load()}
    moved, skipped = [], []
    for eid in ids:
        ev = live.get(str(eid))
        if ev is None:
            skipped.append(str(eid))   # 不存在或已被作废：如实跳过，不假装成功
            continue
        moved.append(ev)
    if not moved:
        return {"moved": 0, "skipped": skipped, "total_cents": 0, "new_ids": []}

    to_label = _project_label(target_pid)
    new_ids = []
    for ev in moved:
        from_label = _project_label(str(ev.get("project") or ""))
        book.append_void(
            [ev["event_id"]],
            reason=f"改归属：从「{from_label}」改到「{to_label}」（用户订正）",
            project=str(ev.get("project") or ""))
        evidence = ev.get("evidence") or {}
        new = make_event(
            ts=ev.get("ts"), event_type=str(ev.get("type")),
            amount_cents=int(ev.get("amount_cents") or 0),
            evidence_kind=str(evidence.get("kind") or "voice"),
            channel=str(ev.get("channel") or "manual"),
            category=str(ev.get("category") or ""),
            counterparty=str(ev.get("counterparty") or ""),
            note=str(ev.get("note") or ""),
            confirmed=True, project=target_pid,
            # 留痕：这条是从哪个项目改过来的（void 里也有 reason，两处都能查）
            extra={"reassigned_from": str(ev.get("project") or "")},
            dedupe_unique=True,
        )
        book.append(new, strict_dedupe=False)
        new_ids.append(new["event_id"])

    # 作废 + 重记会改变投影里的行，必须显式重建（同删除项目那条的理由）
    proj = Projection(db_path())
    proj.rebuild(EventLedger(ledger_path()).load())
    proj.close()
    return {"moved": len(moved), "skipped": skipped,
            "total_cents": sum(int(e.get("amount_cents") or 0) for e in moved),
            "new_ids": new_ids}


class ReassignIn(BaseModel):
    """改归属：把哪些事件改到哪个项目（项目可传 id 或完整名；空串＝未归）。"""

    ids: list[str] = Field(default_factory=list)
    project: str = ""


@app.post("/api/events/reassign")
def events_reassign(body: ReassignIn):
    """逐笔改归属（一条或几条）。前端「未归项目」那一行用的是批量版本，核心是同一段代码。"""
    ids = [str(x).strip() for x in (body.ids or []) if str(x).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="没有指定要改归属的账目")
    pid, hint = projects.resolve_incoming(data_dir(), body.project)
    out = _reassign_events(ids, pid)
    if out["moved"] == 0:
        raise HTTPException(status_code=404, detail="这些账目不存在，或已经被作废了")
    return {"ok": True, "project": pid, "project_name": _project_label(pid),
            "project_hint": hint, **out}


class AbsorbUnassignedIn(BaseModel):
    """把「未归项目」名下的账一次性归到一个项目。"""

    project: str = ""


@app.post("/api/projects/unassigned/absorb")
def projects_absorb_unassigned(body: AbsorbUnassignedIn):
    """一键收编：把「未归项目」名下的**全部**账改到指定项目（追加式订正，可追溯）。

    起因（2026-10-20 真机）：客户先确认了一批草稿、之后才建项目，12 笔全落在未归，
    他建的项目一笔都没有。逐笔改要 12 次，所以给「未归项目」那一行一个批量出口。
    ⚠️ 硬要求只有一条：**目标不能是空串**（"收编到未归"毫无意义）。
    传一个还没建过的名字会照 `resolve_incoming` 的既定语义**顺手建出这个项目**
    （与"记账时说一个新项目名"同一个行为）——界面上是下拉选已有项目，走不到这条路。
    """
    ref = str(body.project or "").strip()
    if not ref:
        raise HTTPException(status_code=400,
                            detail="要把未归的账归到哪个项目？请先选一个项目")
    pid, _ = projects.resolve_incoming(data_dir(), ref)
    if not pid:
        raise HTTPException(status_code=400, detail="目标项目不存在（先新建它，再收编）")
    ids = projects.event_ids_of(_events(), "")
    if not ids:
        return {"ok": True, "moved": 0, "skipped": [], "total_cents": 0,
                "project": pid, "project_name": _project_label(pid),
                "note": "「未归项目」里现在没有账，不用收编。"}
    out = _reassign_events(ids, pid)
    return {"ok": True, "project": pid, "project_name": _project_label(pid), **out,
            "note": (f"已把「未归项目」里的 {out['moved']} 笔改到「{_project_label(pid)}」。"
                     f"账本里每笔都留了一条「作废」和一条「重记」，改了什么可追溯。")}


@app.delete("/api/projects/{pid}")
def projects_delete(pid: str, with_data: bool = False, bills_only: bool = False):
    """删除项目。两种口径由用户在界面上自己选：

    - `with_data=false`（只删项目）：映射表标记删除，**账本一字不动**；
      该项目名下的账单在视图里回落到「未归项目」，数字不会凭空消失。
    - `with_data=true`（项目与账单一起删）：向账本**追加一条作废记录**，被作废的
      事件从此不再参与任何统计。**不重写历史文件**，守住账本追加式不可变这条红线，
      因此误删仍可从账本里查回。

    已删除的项目不再出现在清单里，也不再参与入账归属（避免新账记进已删项目）。

    **`bills_only=true`（2026-10-20 新增）**：**只作废名下的账、项目本身留着** ——
    「这个项目还要继续记，但之前那些账不算了」这种需求，原来只能"连项目一起删掉再新建"，
    等于把项目名与归属也一起丢了。
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
            uvoided = EventLedger(ledger_path()).append_void(
                uids, reason=f"清理「{projects.UNASSIGNED_LABEL}」名下的账（用户主动作废）",
                project="")["voided"]
            uproj = Projection(db_path())
            uproj.rebuild(EventLedger(ledger_path()).load())
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

    target = projects.resolve(data_dir(), pid)
    if target is None:
        raise HTTPException(status_code=404, detail=f"项目不存在：{pid}")
    row = projects.get(data_dir(), target) or {}
    label = str(row.get("name") or target)

    # ③ 只删账、保留项目（2026-10-20 David 要求）：作废名下账单，项目行留在清单里
    if bills_only:
        bids = projects.event_ids_of(_events(), target)
        bvoided = 0
        if bids:
            bvoided = EventLedger(ledger_path()).append_void(
                bids, reason=f"清空项目「{label}」名下的账（项目保留）", project=target)["voided"]
            bproj = Projection(db_path())
            bproj.rebuild(EventLedger(ledger_path()).load())
            bproj.close()
        return {
            "ok": True,
            "action": "voided_bills",
            "project": row,
            "bill_count": len(bids),
            "voided_count": bvoided,
            "note": (f"「{label}」名下的 {bvoided} 笔已作废，项目本身留着"
                     f"（账本里留痕，可追溯）。以后还能继续往这个项目里记账。"
                     if bvoided else
                     f"「{label}」名下本来就没有账，什么都没动。"),
        }

    ids = projects.event_ids_of(_events(), target)
    voided = 0
    if with_data:
        voided = EventLedger(ledger_path()).append_void(
            ids, reason=f"删除项目「{label}」时一并作废", project=target)["voided"]
        # SQLite 投影是按 MODEL_VERSION 缓存的，作废不改变模型版本 —— 必须显式重建，
        # 否则投影里的旧行（assumptions/transactions）还会留着已被作废的事件。
        proj = Projection(db_path())
        proj.rebuild(EventLedger(ledger_path()).load())
        proj.close()

    out = projects.mark_deleted(data_dir(), target, purged_count=voided, with_data=with_data)
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
    out = projects.summary(data_dir(), _events(), project=project)
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail=out.get("error", "项目不存在"))
    return out


@app.get("/api/projects/{pid}/detail")
def projects_detail(pid: str):
    """单个项目的明细（详情弹窗用）：合计 / 分类构成 / 报销与发票 / 最近事件 / 时间范围。

    口径复用 `/api/projects/summary`（支出侧按发票号去重），所以"总览"与"详情"的数字必然一致
    —— 不另写一套算法，避免同一笔在两处显示不同金额。
    """
    out = projects.detail(data_dir(), _events(), pid)
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail=out.get("error", "项目不存在"))
    return out


class ProjectHoursIn(BaseModel):
    """这个活一共花了多少小时（A 档口径，2026-10-29）。

    传 0 / 不传 = 清掉这个数。**含沟通、改稿、换素材的全部投入时间**，不是只算干活。
    """
    hours: float | None = Field(default=None, ge=0)


@app.post("/api/projects/{pid}/hours")
def projects_hours(pid: str, body: ProjectHoursIn):
    """记录项目工时（历史时薪的分母）。**只写映射表，账本一个字不动**。

    为什么必须有这个入口：历史时薪 = 项目收入 ÷ 项目工时，没有小时数就算不出来。
    2026-10-29 之前只能往收入事件上挂 `hours`，界面上根本没有入口 ——
    真机走一遍永远是「你的历史时薪：算不出」。
    """
    try:
        row = projects.set_hours(data_dir(), pid, body.hours)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail=f"找不到项目：{pid}")
    return {"ok": True, **row}


class ProjectCloseIn(BaseModel):
    """关账 / 取消关账。只改一个时间戳，不动账本。"""
    closed: bool = True


@app.get("/api/projects/{pid}/reconcile")
def projects_reconcile(pid: str):
    """项目对账（只读）：「账面 / 有账单凭证的收入 / 票据」三栏 + 差额。

    有几栏出几栏：没导入过账单时 `bill.available=false`，**不拿 0 冒充「到账 0 元」**。
    口径复用 `projects.detail`（同号发票只算一次），所以对账表里的数=工作台里的数。
    """
    out = reconcile_svc.reconcile(data_dir(), _events(), pid,
                                  evidence_files.load_map(data_dir()))
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail=out.get("error", "项目不存在"))
    return out


@app.post("/api/projects/{pid}/close")
def projects_close(pid: str, body: ProjectCloseIn | None = None):
    """关账：在这个项目上记一个 `closed_at`。**不动账本、不动金额**。

    关账是「这个活我算完了」的宣告，也是「项目关账率」这个指标的分母来源。
    """
    row = projects.close_project(data_dir(), pid, closed=True if body is None else body.closed)
    if row is None:
        raise HTTPException(status_code=404, detail=f"找不到项目：{pid}")
    return {"ok": True, **row}


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


# 订单列表那行与付款凭证的时间窗（秒）。用户口径（2026-09-28）：这两张是不是同一笔
# **看时间** —— 付款凭证的支付时间 11:56:37 对上订单列表那条的打印时间 11:55:53（差 44 秒）。
# 金额只作附注（78 这种数字很容易撞车），所以时间对不上就不配。
_LIST_MATCH_WINDOW_SECONDS = 300


def _parse_ts(value: str) -> tuple[str, int | None] | None:
    """把 ``YYYY-MM-DD HH:MM:SS`` / ``YYYY-MM-DD`` 解析成 (日期, 当天秒数)。

    秒数可能是 None（页面上只有日期，没有时分秒）—— 这不是错误，调用方会退化处理。
    """
    s = str(value or "").strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if not m:
        return None
    y, mo, d, hh, mi, sec = m.groups()
    day = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    secs = int(hh) * 3600 + int(mi) * 60 + int(sec or 0) if hh is not None else None
    return day, secs


def _match_list_orders(records: list[dict]) -> list[dict]:
    """把「订单列表」页里的每一行配到**同一批**的付款凭证上（确定性，不带模型）。

    为什么要它：一张「我的订单」列表和一 Griff 付款凭证可能是**同一笔钱的两个视角**
    （实弹：列表里 11:55:53 那笔 ¥78.0 就是账单详情里 11:56:37 那笔），两张都记就是记两遍；
    反过来，列表里**没配到凭证**的行也不该被悄悄吞掉 —— 要如实说出来。

    匹配规则（用户 2026-09-28 定：以**时间**为主，金额只作附注）：
      · 两边都有时分秒 → 同一天 且 相差 ≤ 5 分钟 即算同一笔；金额不一致时**附注提示核对**，不否决；
      · 有一边只有日期（没有时分秒）→ 必须**同一天且金额一致**才算，否则不配（否则一张列表里
        十条同日订单会全都配到同一张凭证上）。
    命中不代表"自动记账"：列表页本来就不建草稿，这里只是把结论说出来；
    没命中的**不自动记账**，只如实列出（列表页只有总价，可能正是那笔已退款的）。
    """
    vouchers: list[dict] = []
    for rec in records or []:
        if rec.get("ok") is False:
            continue
        if str(rec.get("doc_kind") or "") not in ("receipt", "payment"):
            continue
        cents = int(rec.get("gross_cents") or rec.get("amount_cents") or 0)
        ts = _parse_ts(rec.get("occurred_at") or rec.get("date") or "")
        if cents and ts:
            vouchers.append({"file": rec.get("file", ""), "cents": cents, "ts": ts})

    out: list[dict] = []
    for rec in records or []:
        if str(rec.get("doc_kind") or "") != "list":
            continue
        matched, unmatched = [], []
        for row in rec.get("orders") or []:
            ts = _parse_ts(row.get("at") or "")
            cents = int(round(float(row.get("amount") or 0) * 100))
            if cents <= 0:
                # 金额 0 的行**整个跳过**（用户 2026-09-28：「先不列 0 元的了」「果然是没有必要，
                # 因为这个已经列在其他截图里了，而且不是 0 元」）——
                # 这类行多半是截图被裁掉、金额没进画面，而同一天的付款详情那张通常已经把它记了；
                # 既不能建草稿，也没必要在汇总里占一句让人去核对。
                continue
            hit = None
            for v in vouchers:
                if not ts or v["ts"][0] != ts[0]:
                    continue                       # 先要求同一天
                if ts[1] is not None and v["ts"][1] is not None:
                    if abs(ts[1] - v["ts"][1]) > _LIST_MATCH_WINDOW_SECONDS:
                        continue
                elif cents != v["cents"]:
                    continue                       # 没有时分秒 → 必须金额一致
                hit = {"at": row.get("at", ""), "amount_cents": cents,
                       "name": row.get("name", ""), "status": row.get("status", ""),
                       "voucher_file": v["file"],
                       "delta_seconds": (abs(ts[1] - v["ts"][1])
                                         if ts[1] is not None and v["ts"][1] is not None else None),
                       "amount_differs": bool(cents and v["cents"] and cents != v["cents"])}
                break
            (matched if hit else unmatched).append(hit or {
                "at": row.get("at", ""), "amount_cents": cents, "name": row.get("name", ""),
                "status": row.get("status", "")})
        if matched or unmatched:
            out.append({"file": rec.get("file", ""), "matched": matched, "unmatched": unmatched})
    return out


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
                "how": f"{reason}——已跳过，不影响其他文件；**请把这张图重新上传一次**"}

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
    doc_kind = str(got.get("doc_kind") or "")
    refund = int(got.get("refund_cents") or 0)
    gross = int(got.get("gross_cents") or 0)
    if doc_kind == "expense_list":
        # 费用清单（2026-10-20）：逐行一笔，行尾数字是金额，合计由代码加。
        # 这一段要说清三件事：读到了几行、生成了几条、有几行没标金额所以没算进去 ——
        # 用户最怕的不是少算，是"悄悄少算"。
        n_lines = int(got.get("list_lines") or 0)
        n_skip = int(got.get("skipped_lines") or 0)
        recognized = f"这是一份费用清单：图里读到 {n_lines} 行带金额的条目"
        made = (f"生成 {draft_count} 条待确认草稿，合计 {total_cents / 100:.2f} 元"
                f"——逐条核对后确认，别整批点到不该报的")
        how = "每行末尾的数字当金额、合计由系统加" + (
            f"；另有 {n_skip} 行没标金额，没计入合计" if n_skip else "")
    elif reason == "unreadable":
        # 没读出来就直说，并给出**可执行的下一步**：重新上传这一张（用户 2026-09-28 的建议）
        recognized = "这张没读出来（没识别到金额，也没有可用的文字）"
        made = "未生成草稿——**请把这张图重新上传一次**（尽量用原图、别截屏压缩）"
        how = "识别没结果时不猜、不凑数：重传一次最省事，也可以自己手工记一笔"
    elif reason == "unpaid" or (doc_kind == "unpaid" and draft_count):
        # 待支付页（按钮还是「去支付」）：**有应付金额就列一条候选**（用户 2026-09-28 口径：
        # 「一个过去、一个回来的打车记录，车牌号不一样，57.7 是应该被记录的」）——
        # 状态必须写清，付没付由用户确认；只有读不到金额时才真的不记。
        if draft_count:
            recognized = "待支付页面：截图时还没付款（按钮是「去支付」）"
            made = (f"生成 {draft_count} 条候选草稿，合计 {total_cents / 100:.2f} 元"
                    f"——请确认这笔是否已付、该不该报")
            how = "金额与付款状态都从页面读出；付没付由你确认，机器不替你决定"
        else:
            recognized = "待支付页面：这张单**还没付款**（页面上没读到应付金额）"
            made = "未生成草稿——没读到金额，**请把这张图重新上传一次**"
            how = "等付了款、拿到付款凭证再记；重传后如果还是读不出金额，就手工记一笔"
    elif reason == "list":
        # 订单列表不记账，但**必须点名和哪张图重复**（用户 2026-09-28 要求：
        # 「最好能记录下是和哪个图片的信息有重复，方便我排查」）。
        lm = got.get("list_match") or {}
        hit = lm.get("matched") or []
        miss = lm.get("unmatched") or []
        def _amt(c: int) -> str:
            return f"{c / 100:.2f} 元"
        names = "、".join(f"「{h.get('voucher_file', '')}」" for h in hit[:2])
        recognized = ("订单列表（不是付款凭证）："
                      f"{len(hit)} 笔与另一张图是同一笔，{len(miss)} 笔没配到付款凭证"
                      if miss else
                      f"订单列表（不是付款凭证）：{len(hit)} 笔与另一张图是同一笔")
        drafted = lm.get("drafted") or []
        if hit:
            first = hit[0]
            gap = (f"，付款时间相差 {first['delta_seconds']} 秒"
                   if first.get("delta_seconds") is not None else "")
            made = (f"未生成草稿——{first.get('at', '')} 的 {_amt(first.get('amount_cents', 0))}"
                    f"与{names}是同一笔{gap}，记了就会重复")
        elif drafted:
            made = (f"生成 {draft_count} 条候选草稿，合计 {total_cents / 100:.2f} 元"
                    f"——订单列表里没配到付款凭证的那几笔（员工先垫付、第二天报销是正常的），"
                    f"请确认该不该报")
        else:
            made = "未生成草稿——订单列表只是同一笔钱的另一个视角，记了就会和付款详情重复"
        if miss:
            shown = "、".join(f"{m.get('at', '')} {_amt(m.get('amount_cents', 0))}" for m in miss[:2])
            more = f" 等 {len(miss)} 笔" if len(miss) > 2 else ""
            if drafted:
                tail = (f"没配到付款凭证的 {len(miss)} 笔（{shown}{more}）**已列进「待确认」**"
                        f"（员工先垫付、第二天报销是正常的），请确认该不该报")
            else:
                tail = (f"没配到付款凭证的 {len(miss)} 笔（{shown}{more}）"
                        f"要么是退款、要么没金额，没有建草稿")
        else:
            tail = "列表里没有需要单独记的"
        how = (f"要记这一笔，请用{names}那张；{tail}" if hit else tail)
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


def _retry_files(files: list[dict]) -> list[str]:
    """这一批里**需要用户重新上传**的文件：识别失败、或彻底没读出来的那些。

    用户 2026-09-28 的建议：「如果有扫不出来，建议用户重新上传一下没有扫出来的图」——
    逐文件那行说一次还不够，汇总里要能一眼看到**是哪几张**，他才好重传。
    """
    out: list[str] = []
    for it in files or []:
        if it.get("ok") is False or str(it.get("no_draft_reason") or "") == "unreadable":
            out.append(str(it.get("file") or ""))
    return [x for x in out if x]


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
    out = list(merged.values())
    for b in out:
        # 需要重传的文件在这里算好（汇总里要一句话说清是哪几张）
        b["retry_files"] = _retry_files(b.get("files") or [])
    return out


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
        out = intake.ingest(data_dir(), files, project_ref=body.project or None,
                          reconcile=body.reconcile)
    except Exception as e:  # noqa: BLE001 失败要报出来，不吞
        raise HTTPException(status_code=500, detail=f"导入失败：{type(e).__name__}: {str(e)[:200]}")
    out["batches"] = _merge_batches(out.get("batches") or [])
    for b in out["batches"]:
        b["list_matches"] = _match_list_orders(b.get("files") or [])
    for b in out["batches"]:
        pid = str(b.get("project_id") or "")
        for m in b["list_matches"]:
            made = [d for d in (intake.draft_for_list_order(data_dir(), pid, o, m["file"])
                                for o in (m.get("unmatched") or [])) if d]
            if made:
                m["drafted"] = [{"amount_cents": d["amount_cents"], "category": d["category"]}
                                for d in made]
                b["draft_count"] = int(b.get("draft_count") or 0) + len(made)
                b["identified_total_cents"] = (int(b.get("identified_total_cents") or 0)
                                               + sum(d["amount_cents"] for d in made))
    out["pending"] = [{"id": d["id"], "direction": d["direction"],
                       "amount_cents": d["amount_cents"], "category": d["category"],
                       "project": d.get("project", ""),
                       "project_name": _project_label(d.get("project", ""))}
                      for d in pending.list_all(data_dir())]
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
    root = Path(data_dir) if data_dir is not None else tenancy.data_dir()
    total = len(files)
    batches: list[dict] = []
    for i, f in enumerate(files, 1):
        # 先把项目建出来、再把 start 发出去：用户不用等整批跑完才在左栏看到项目名。
        # 归属规则与 ingest 完全一致（显式项目 > 第一层文件夹名 > 不建项目）；
        # ingest 里还会再 resolve 一次，resolve_incoming 对同名是幂等的。
        hint_pid, hint_name = "", ""
        try:
            if project:
                hint_pid, _ = projects.resolve_incoming(root, project)
            else:
                rel = str(f.get("rel_path") or f["name"]).replace("\\", "/").strip("/")
                parts = [p for p in rel.split("/") if p]
                if len(parts) > 1:
                    hint_pid, _ = projects.resolve_incoming(root, parts[0])
            if hint_pid:
                hint_name = projects.display_name(projects._load(root), hint_pid)
        except Exception:  # noqa: BLE001 项目预建失败不能拖垮导入
            hint_pid, hint_name = "", ""
        yield json.dumps({"stage": "start", "index": i, "total": total,
                          "file": f["name"], "rel_path": f["rel_path"],
                          "project_id": hint_pid, "project_name": hint_name},
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
    merged = _merge_batches(batches)
    # 订单列表 ↔ 付款凭证的配对（时间为主）要在**整批都读完**之后做：
    # 流式是逐个文件跑的，列表页可能先到、凭证后到，边跑边配会漏。
    for b in merged:
        b["list_matches"] = _match_list_orders(b.get("files") or [])
    # 没配到付款凭证的那几行**也要列成候选**（用户 2026-09-28 口径：昨天员工先垫付「是做数的」，
    # 第二天才拿来报销，这个时间差是正常的、客户也认）——不列就等于漏账。
    # 退款状态与金额 0 的不列（钱回来了 / 没有金额）。
    for b in merged:
        pid = str(b.get("project_id") or "")
        for m in b["list_matches"]:
            made = []
            for o in m.get("unmatched") or []:
                d = intake.draft_for_list_order(root, pid, o, m["file"])
                if d:
                    made.append(d)
            if made:
                m["drafted"] = [{"amount_cents": d["amount_cents"], "category": d["category"]}
                                for d in made]
                b["draft_count"] = int(b.get("draft_count") or 0) + len(made)
                b["identified_total_cents"] = (int(b.get("identified_total_cents") or 0)
                                               + sum(d["amount_cents"] for d in made))
    # 配对结果要**补写回那张订单列表的四段**（用户 2026-09-28：「最好能记录下是和哪个图片的
    # 信息有重复，方便我排查」）——逐文件那行是在配对之前发出去的，所以这里补发一次，
    # 前端按 index 原地更新那一行，位置与条数都不变。
    by_file = {m["file"]: m for b in merged for m in (b.get("list_matches") or [])}
    if by_file:
        items_by_file = {it.get("file"): it for b in merged for it in (b.get("files") or [])}
        for idx, f in enumerate(files, 1):
            m = by_file.get(f["name"])
            it = items_by_file.get(f["name"])
            if not m or not it:
                continue
            drafted = m.get("drafted") or []
            yield json.dumps({
                "stage": "file_update", "index": idx, "total": total, "file": f["name"],
                "stages": _file_stages(f["name"], [dict(it, list_match=m)], [],
                                       len(drafted),
                                       sum(x["amount_cents"] for x in drafted)),
            }, ensure_ascii=False) + "\n"
    yield json.dumps({"stage": "all_done", "batches": merged},
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
    items = pending.list_all(data_dir())
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
        override = projects.resolve_incoming(data_dir(), override)[0] if override.strip() else ""
    # 分类同理可在确认那一刻改：草稿分类是「待确认」（机器没判出来）时就该由人补上。
    # `pending.accept` 内部还会把占位符收成「其他」，账本里永远不出现「待确认」这个"分类"。
    # ⚠️ **空字符串 ＝ 用户没改分类，不是"把分类清空"**：草稿识别出来的分类必须留住。
    # 2026-09-28 真机踩到：前端两处都无条件发 category: ""（没动过下拉也是空串），
    # 于是「待确认」里每确认一笔，草稿分类就被冲成「其他」—— 18 笔全变「其他」。
    # （project 那边不一样：空字符串有明确含义＝未归项目，所以那里不能这么收。）
    cat_override = (body.category or "").strip() if body is not None else None
    if not cat_override:
        cat_override = None
    draft = pending.get(data_dir(), pid) or {}
    strict = draft.get("source") in _FILE_DRAFT_SOURCES
    append = functools.partial(_append_event, strict_dedupe=strict)
    out = pending.accept(data_dir(), pid, append, project_override=override,
                         category_override=cat_override)
    if out is None:
        raise HTTPException(status_code=404, detail="待确认草稿不存在")
    state = run_state(ledger_path(), db_path())["state"]
    return {"ok": True, "appended": out["appended"], "draft": out["draft"],
            "project_name": _project_label((out.get("event") or {}).get("project", "")),
            "state": state}


@app.post("/api/pending/{pid}/decline")
def pending_decline(pid: str, group: str = ""):
    """不要这笔：丢弃草稿，不入账。

    **软删除**：草稿进回收站（打 `discarded_at`），界面上消失但随时能撤回。
    `group` 由前端给（一次批量丢弃用同一个），撤回时按它一次放回来。
    """
    g = pending.decline(data_dir(), pid, group=group)
    if g is None:
        raise HTTPException(status_code=404, detail="待确认草稿不存在")
    return {"ok": True, "id": pid, "group": g}


class PendingRestoreIn(BaseModel):
    ids: list[str] | None = None
    group: str | None = None


@app.post("/api/pending/restore")
def pending_restore(body: PendingRestoreIn):
    """撤回丢弃：把刚丢掉的草稿放回「待确认」（按 id 或按组件）。

    用户 2026-09-28 要求「已丢弃 N 笔…」那句小字后面加一个撤回按钮 ——
    丢弃本来就没动账本，撤回只是把草稿从回收站拿回来，**不产生任何账本事件**。
    """
    rows = pending.restore(data_dir(), ids=body.ids, group=body.group)
    return {"ok": True, "restored": len(rows), "ids": [d["id"] for d in rows]}


@app.post("/api/chat")
def chat(body: ChatIn):
    """多轮自由对话：会话记忆 + LLM 选动作 + 真执行 + 结果合成（记录走待确认）。"""
    sid = body.session_id or uuid.uuid4().hex
    mem_sid = _mem_sid(sid)
    session_mem.touch(mem_sid)
    memory = session_mem.context_of(mem_sid)
    prompt = f"[对话上下文]\n{memory}\n[用户现在说]\n{body.text}" if memory else body.text
    # 本机登录名：三条路（LLM 解析 / LLM 润色 / 规则兜底）都要带上，
    # 否则「我是谁」在有 Key 和没 Key 两种档位下会给出两个不同答案。
    user_ctx = _user_context(body.user)

    # ── 工时/工分**先过确定性解析**，排在模型前面（2026-09-28 实测踩到的坑）────────
    # 起因：这个说法（「阿明今天 8 小时，时薪 50」）交给模型时，它对合同工那条自由发挥出
    # 「先按你说的留底，回头升级正式入账」——**什么都没记**，却在话里承诺了一件没发生的事。
    # 这类"我们做了什么 / 没做什么"的话不能由模型编，所以工时/工分走确定性路径：
    # 解析得到条目就记进工时表；提到合同工/交金就如实拒绝。两句都不沾才交给模型。
    ts = timesheet_records.parse_text(body.text)
    if ts["entries"]:
        parts, summary = _record_timesheet(ts["entries"], body.project or "", body.text)
        final = "\n".join(parts)
        session_mem.add_turn(mem_sid, body.text, final, result_note=final[:120])
        return {"ok": True, "text": final, "session_id": sid, "pending": [], "timesheet": summary}
    if ts["contract"]:
        session_mem.add_turn(mem_sid, body.text, _CONTRACT_REFUSAL, result_note=_CONTRACT_REFUSAL[:120])
        return {"ok": True, "text": _CONTRACT_REFUSAL, "session_id": sid, "pending": []}

    # ── 「每行一个条目 + 行尾金额」的清单**也先过确定性解析**（2026-10-20）─────────────
    # 起因：客户把同一份 16 行成本清单连发两次 ——
    #   第一次（只有清单 + 一句「帮我算个总价」）模型只挑出 1 行（「10块v口电池」→ ¥10.00），
    #   剩下 12 行全丢；第二次他补一句「**每一行空格之后的数字是需要合计的费用 请帮我合计**」，
    #   模型才按行拆对。也就是说这个结构原来**靠人解释才成立**。
    # 而「名称 + 行尾数字」是报价单 / 成本清单最常见的写法，没有理由让用户先教一遍；
    # 金额与合计更不能交给模型加（见 `services/amount_guard.py` 那次真机）。所以排在模型前面。
    lst = expense_list.parse(body.text)
    if lst["is_list"]:
        return _reply_expense_list(lst, body, sid, mem_sid)

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
                    else:
                        # 数字闸（2026-10-20）：模型写的话里，金额与合计必须是引擎算过的数。
                        # 起因：12 行清单那句合计被模型自己加成了 8980（真值 9980），
                        # 同一屏上「文字 8980」与「确认框 9980」对着干。不过闸就整条作废、
                        # 改用确定性文案 —— 与决策那条路同一个纪律。
                        final = _guard_reply(final, run, body.text)
                # 查询答复要如实标注口径范围（查询始终是全账本，不跟随选中项目）
                if any(str(x.get("action", "")).startswith("ask_") for x in run["results"]):
                    note = _scope_note(body.project)
                    if note:
                        final = f"{final}\n{note}"
            else:
                final = str(parsed.get("reply", "")).strip() or "我还在学习这句怎么接——可以记一笔账，或问我最近流水 / 现金流。"
            session_mem.add_turn(mem_sid, body.text, final, result_note=final[:120])
            out = {"ok": True, "text": final, "session_id": sid,
                   "pending": [{"id": d["id"], "direction": d["direction"],
                                "amount_cents": d["amount_cents"], "category": d["category"],
                                "channel": d["channel"], "project": d.get("project", ""),
                                "project_name": _project_label(d.get("project", ""))}
                               for d in run["drafts"]]}
            if run.get("decision"):
                out["decision"] = run["decision"]
            if run.get("timesheet"):
                out["timesheet"] = run["timesheet"]
            return out

    r = _rule_reply(body.text, body.project, body.user)
    session_mem.add_turn(mem_sid, body.text, r.get("text", ""))
    r["session_id"] = sid
    return r
