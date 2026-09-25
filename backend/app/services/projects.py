"""项目维度：映射表 + 按项目归集汇总（纯本机计算）

设计约束（2026-09-24 David 拍板）：
1. 账本只存稳定 id（project_a / project_b ...），显示名放 data/projects.json 映射表；
   重命名只改映射，账本一字不动，守住「事件账本不可变」这条产品红线。
2. 默认名 Project A / Project B / Project C；用户没输入完整名称时先用默认名，
   之后随时可重命名。创建时会回一句提醒，请用户补全项目全名。
3. 项目维度是「真实项目」（如 919 昆明项目），不是费用类别（打车/吃饭）。

合规边界（与 02-脑暴/合规与边界-责任切分与红线-v1.0.md 一致）：
只做归集与呈现，不替用户判断「这笔该记哪个项目」，不给指令。

报销去重口径：
同一项目内「发票号码相同」的支出事件只计一次，其余进 duplicates 并如实标注；
账本未记录发票号码的事件不参与去重（不推断）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from .state_engine.spec import (DEFAULT_PROJECT_LABEL, MAX_DEFAULT_PROJECTS,
                               PROJECT_ID_PREFIX, UNASSIGNED_PROJECT)

UNASSIGNED_LABEL = "未归项目"


def _path(data_dir: str | Path) -> Path:
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / "projects.json"


def _load(data_dir: str | Path) -> list[dict]:
    fp = _path(data_dir)
    if not fp.exists():
        return []
    try:
        rows = json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def _save(data_dir: str | Path, rows: list[dict]) -> None:
    _path(data_dir).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def default_name(index: int) -> str:
    """第 index 个（从 0 起）项目的默认显示名：Project A / B / C ..."""
    i = max(0, int(index))
    if i < MAX_DEFAULT_PROJECTS:
        return f"{DEFAULT_PROJECT_LABEL} {chr(ord('A') + i)}"
    return f"{DEFAULT_PROJECT_LABEL} {i + 1}"


def _next_id(rows: list[dict]) -> str:
    used = {str(r.get("id")) for r in rows}
    for i in range(MAX_DEFAULT_PROJECTS):
        pid = f"{PROJECT_ID_PREFIX}{chr(ord('a') + i)}"
        if pid not in used:
            return pid
    return f"{PROJECT_ID_PREFIX}{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create(data_dir: str | Path, name: str = "") -> dict:
    """新建项目。name 为空则用默认名 Project A/B/C，并在返回值里给出补全名称的提醒。"""
    rows = _load(data_dir)
    pid = _next_id(rows)
    clean = str(name or "").strip()
    display = clean or default_name(len(rows))
    row = {"id": pid, "name": display, "named": bool(clean), "created": _now()}
    rows.append(row)
    _save(data_dir, rows)
    out = dict(row)
    if not clean:
        out["name_hint"] = (f"已先建为「{display}」。建议补上这个项目的完整名称"
                            f"（例如「919 昆明项目」）：POST /api/projects 传 id 与 name 即可重命名，"
                            f"账本不受影响。")
    return out


def rename(data_dir: str | Path, pid: str, name: str) -> dict | None:
    """重命名项目。只改映射表里的显示名，账本里的事件一字不动。"""
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("新项目名不能为空")
    rows = _load(data_dir)
    for r in rows:
        if str(r.get("id")) == str(pid):
            r["name"] = clean
            r["named"] = True
            r["renamed_at"] = _now()
            _save(data_dir, rows)
            return dict(r)
    return None


def is_deleted(row: dict | None) -> bool:
    """项目是否已被删除（软删除：映射表里留痕，账本不受影响）。"""
    return bool((row or {}).get("deleted"))


def visible_rows(data_dir: str | Path) -> list[dict]:
    """正常显示的项目（不含已删除的）。"""
    return [r for r in _load(data_dir) if not is_deleted(r)]


def event_ids_of(events: list[dict] | None, pid: str) -> list[str]:
    """该项目名下的全部事件 id（「连数据一起删」要用它去追加作废记录）。"""
    target = str(pid or "")
    return [str(ev.get("event_id") or "") for ev in (events or [])
            if str(ev.get("project") or "") == target and ev.get("event_id")]


def mark_deleted(data_dir: str | Path, pid: str, purged_count: int = 0,
                 with_data: bool = False) -> dict | None:
    """软删除项目：映射表标记 deleted，**账本一字不动**。

    with_data=True 时记录「它的 N 笔事件已作废」；作废动作本身由调用方
    通过 EventLedger.append_void 追加，本模块不依赖账本（保持纯映射/归集职责）。
    """
    rows = _load(data_dir)
    for r in rows:
        if str(r.get("id")) == str(pid):
            r["deleted"] = True
            r["deleted_at"] = _now()
            if with_data:
                r["purged"] = True
                r["purged_count"] = int(purged_count)
            _save(data_dir, rows)
            return dict(r)
    return None


def get(data_dir: str | Path, pid: str) -> dict | None:
    for r in _load(data_dir):
        if str(r.get("id")) == str(pid):
            return dict(r)
    return None


def resolve(data_dir: str | Path, ref: str) -> str | None:
    """把用户给的稳定 id 或显示名解析成稳定 id；解析不到返回 None。

    已删除的项目不参与解析：否则新建/入账会把新账记到一个已经删掉的项目下面。
    """
    s = str(ref or "").strip()
    if not s:
        return None
    for r in visible_rows(data_dir):
        if s == str(r.get("id")) or s == str(r.get("name")):
            return str(r.get("id"))
    return None


def ensure_default(data_dir: str | Path) -> dict:
    """保证至少存在一个**未删除**的项目（默认 Project A），返回第一个项目。

    若项目全被删光，这里会新建一个 Project A，避免新账无处可归。
    """
    rows = visible_rows(data_dir)
    if not rows:
        return create(data_dir)
    return dict(rows[0])


def resolve_incoming(data_dir: str | Path, ref: str | None = None) -> tuple[str, str]:
    """入账时决定这笔归哪个项目，返回 (project_id, 给用户的提示文案)。

    - 说了项目（id 或完整名）→ 用它；表里没有就按这个名字新建，别丢掉用户的归类意图。
    - 没说项目 → 落到当前默认项目（表里第一个，没有就建 Project A），并提示可改名。
    """
    s = str(ref or "").strip()
    if s:
        pid = resolve(data_dir, s)
        if pid is None:
            row = create(data_dir, s)
            return row["id"], f"已新建项目「{row['name']}」并把这笔归入。"
        return pid, ""
    row = ensure_default(data_dir)
    return row["id"], f"未指定项目，这笔先归入「{row['name']}」，可随时重命名。"


def display_name(rows: list[dict], pid: str) -> str:
    """project id → 显示名；空 id 一律叫未归项目。"""
    s = str(pid or "")
    if not s:
        return UNASSIGNED_LABEL
    for r in rows:
        if str(r.get("id")) == s:
            return str(r.get("name") or s)
    return s


def _event_project_ids(events: list[dict] | None) -> list[str]:
    out: list[str] = []
    for ev in events or []:
        pid = str(ev.get("project") or "")
        if pid and pid not in out:
            out.append(pid)
    return out


def _sync(data_dir: str | Path, events: list[dict] | None) -> list[dict]:
    """把账本里出现、但映射表里缺失的项目补登进来（例如手工写账本或换了机器），避免归集时丢项目。"""
    rows = _load(data_dir)
    known = {str(r.get("id")) for r in rows}
    changed = False
    for pid in _event_project_ids(events):
        if pid not in known:
            rows.append({"id": pid, "name": pid, "named": False, "created": _now(),
                         "note": "账本里出现但映射表缺失，已自动补登，请重命名"})
            known.add(pid)
            changed = True
    if changed:
        _save(data_dir, rows)
    return rows


def _invoice_no_of(ev: dict) -> str:
    """取事件的发票号码（报销去重用）。账本无此字段时返回空串，不推断。"""
    no = ev.get("invoice_no")
    if not no:
        extra = ev.get("extra")
        if isinstance(extra, dict):
            no = extra.get("invoice_no")
    return str(no or "").strip()


def _empty_group(pid: str, rows: list[dict]) -> dict:
    return {
        "project": pid,
        "name": display_name(rows, pid),
        "income_cents": 0,
        "expense_cents": 0,
        "income_count": 0,
        "expense_count": 0,
        "by_category": {},
        "duplicates": [],
        "_seen_invoices": {},
    }


def summary(data_dir: str | Path, events: list[dict] | None,
            project: str | None = None) -> dict:
    """按项目归集账本事件。

    project 传 id 或显示名则只看该项目；传「未归项目」只看未归的；不传则全部项目。
    支出侧按发票号码去重（同一项目内相同号码只计一次），去掉的进 duplicates 如实标注。

    已删除项目名下的账单**不会消失**：账本记录还在，归集时回落到「未归项目」。
    这样删项目只影响分组，不会让用户以为钱也没了（数字必须看得见）。
    """
    rows = _sync(data_dir, events)
    deleted_ids = {str(r.get("id")) for r in rows if is_deleted(r)}

    raw = str(project or "").strip()
    if not raw:
        target: str | None = None
    elif raw in (UNASSIGNED_LABEL, "unassigned", "-"):
        target = UNASSIGNED_PROJECT
    else:
        target = resolve(data_dir, raw)
        if target is None:
            return {"ok": False, "error": f"找不到项目：{raw}"}

    groups: dict[str, dict] = {}
    for ev in events or []:
        pid = str(ev.get("project") or "")
        if pid in deleted_ids:
            pid = UNASSIGNED_PROJECT
        if target is not None and pid != target:
            continue
        g = groups.setdefault(pid, _empty_group(pid, rows))
        amount = int(ev.get("amount_cents") or 0)
        etype = str(ev.get("type") or "")

        # 收入侧：income 与 refund 都算正向现金流入（与 spec.HEALTH_POSITIVE_TYPES 同口径）
        if etype in ("income", "refund"):
            g["income_cents"] += amount
            g["income_count"] += 1
            continue
        if etype != "expense":
            continue

        no = _invoice_no_of(ev)
        if no and no in g["_seen_invoices"]:
            g["duplicates"].append({
                "invoice_no": no,
                "event_id": ev.get("event_id", ""),
                "date": str(ev.get("ts") or "")[:10],
                "amount_cents": amount,
                "kept_event_id": g["_seen_invoices"][no],
                "reason": "同一项目内发票号码重复，已从合计中剔除",
            })
            continue
        if no:
            g["_seen_invoices"][no] = ev.get("event_id", "")

        g["expense_cents"] += amount
        g["expense_count"] += 1
        cat = str(ev.get("category") or "").strip() or "未分类"
        c = g["by_category"].setdefault(cat, {"category": cat, "amount_cents": 0, "count": 0})
        c["amount_cents"] += amount
        c["count"] += 1

    projects: list[dict] = []
    for g in groups.values():
        g.pop("_seen_invoices", None)
        g["net_cents"] = g["income_cents"] - g["expense_cents"]
        g["by_category"] = sorted(g["by_category"].values(), key=lambda c: -c["amount_cents"])
        g["reimbursement"] = {
            "expense_total_cents": g["expense_cents"],
            "expense_count": g["expense_count"],
            "duplicate_count": len(g["duplicates"]),
            "no_duplicate": len(g["duplicates"]) == 0,
        }
        projects.append(g)

    # 未归项目排最后；其余按支出、收入倒序
    projects.sort(key=lambda p: (p["project"] == UNASSIGNED_PROJECT,
                                 -p["expense_cents"], -p["income_cents"]))

    return {
        "ok": True,
        "project_count": len(projects),
        "projects": projects,
        "total": {
            "income_cents": sum(p["income_cents"] for p in projects),
            "expense_cents": sum(p["expense_cents"] for p in projects),
            "net_cents": sum(p["net_cents"] for p in projects),
            "duplicate_count": sum(len(p["duplicates"]) for p in projects),
        },
        "dedupe_basis": "同一项目内发票号码相同的事件只计一次；账本未记录发票号码的事件不参与去重（不推断）。",
        "disclaimer": "项目归集由账本事件的项目字段聚合得出，仅为事实陈述，不构成记账或税务结论。",
    }


def list_projects(data_dir: str | Path, events: list[dict] | None) -> dict:
    """项目清单（含每个项目的收支汇总）+ 未归项目一行。

    已删除的项目不出现在清单里；它名下的账单由 summary 回落到「未归项目」，
    因此删项目不会让任何一笔钱从数字里消失。
    """
    rows = _sync(data_dir, events)
    stat = {p["project"]: p for p in summary(data_dir, events)["projects"]}

    out = []
    for r in rows:
        if is_deleted(r):
            continue
        s = stat.get(str(r.get("id")), {})
        out.append({
            "name": r.get("name", ""),
            "named": bool(r.get("named", False)),
            "created": r.get("created", ""),
            "renamed_at": r.get("renamed_at", ""),
            "income_cents": s.get("income_cents", 0),
            "expense_cents": s.get("expense_cents", 0),
            "count": s.get("income_count", 0) + s.get("expense_count", 0),
        })
        out[-1]["id"] = r.get("id", "")
        if r.get("name_hint"):
            out[-1]["name_hint"] = r["name_hint"]

    un = stat.get(UNASSIGNED_PROJECT) or {"income_cents": 0, "expense_cents": 0,
                                          "income_count": 0, "expense_count": 0}
    unnamed = [p["id"] for p in out if not p["named"]]
    return {
        "ok": True,
        "project_count": len(out),
        "projects": out,
        "unassigned": {
            "project": UNASSIGNED_PROJECT,
            "name": UNASSIGNED_LABEL,
            "income_cents": un.get("income_cents", 0),
            "expense_cents": un.get("expense_cents", 0),
            "count": un.get("income_count", 0) + un.get("expense_count", 0),
        },
        "unnamed_project_ids": unnamed,
        "naming_hint": ("有项目还在用默认名，建议补上完整项目名称（例如「919 昆明项目」）。"
                        "POST /api/projects 传 id 与 name 重命名，账本不受影响。"
                        if unnamed else ""),
        "disclaimer": "项目清单由映射表与账本事件聚合得出，仅为事实陈述。",
    }
