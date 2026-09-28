"""待确认入账草稿（识别 → 人确认 → 入账）

流程：/api/chat 记账只生成草稿（不入账）→ 用户点「确认入账」→ accept 写进事件账本；
点「不要这笔」→ decline 丢弃。数据放运行时目录 data/pending.json（不入库）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from .categories import UNCONFIRMED_CATEGORY


def _ledger_category(raw: str | None, direction: str = "expense") -> str:
    """落账本的分类：**绝不写占位符**。

    `categories.py` 自己写着「判不出就进『待确认』，它是要人来确认的池子，
    **不是一个类别**」（见该模块 §1 与关键词表注释）。但草稿的 category 若是
    「待确认」，确认入账时会被原样写进账本 —— 于是「待确认」变成了一个真分类，
    还会进支出构成统计。2026-09-28 真机上就这样落了 2 条（¥962.00 与 ¥78.00）。

    收口方式：占位符（以及空值）统一落**「其他」/「其他收入」** —— 也就是这条
    流水线本来就用的"认不出"桶（`main._append_event` 的 `category or "其他"`、
    `query_tools` 的空分类兜底都是它）。**不新造分类名**，也不留空串让下游各自猜
    （`projects.summary()` 兜「未分类」、`query_tools` 兜「其他」，两处口径本来就不一致）。

    用户在确认那一刻补了分类就按补的写 —— 这才是「待确认」这个池子存在的意义。
    """
    c = (raw or "").strip()
    if c and c != UNCONFIRMED_CATEGORY:
        return c
    # 收入侧没有「其他」这个分类，对应的是「其他收入」（见 categories.INCOME_CATEGORIES）
    return "其他收入" if direction == "income" else "其他"


def _path(data_dir: str | Path) -> Path:
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / "pending.json"


def _load_raw(data_dir: str | Path) -> list[dict]:
    """读出文件里的**全部**草稿，含已丢弃的（回收站也在这个数组里）。"""
    fp = _path(data_dir)
    if not fp.exists():
        return []
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def list_all(data_dir: str | Path) -> list[dict]:
    """**待确认**里的草稿（已丢弃的不算 —— 它们在回收站里，可以撤回）。"""
    return [d for d in _load_raw(data_dir) if not d.get("discarded_at")]


def list_trash(data_dir: str | Path) -> list[dict]:
    """回收站：被丢弃的草稿（最近丢的在前）。留着是为了**能撤回**。"""
    rows = [d for d in _load_raw(data_dir) if d.get("discarded_at")]
    return sorted(rows, key=lambda d: str(d.get("discarded_at") or ""), reverse=True)


def _save(data_dir: str | Path, drafts: list[dict]) -> None:
    fp = _path(data_dir)
    fp.write_text(json.dumps(drafts, ensure_ascii=False, indent=1), encoding="utf-8")


def create(data_dir: str | Path, *, direction: str, amount_cents: int, category: str,
           channel: str, note: str, counterparty: str = "", project: str = "") -> dict:
    drafts = list_all(data_dir)
    draft = {
        "id": uuid.uuid4().hex[:12],
        "direction": direction,
        "amount_cents": amount_cents,
        "category": category or "其他",
        "channel": channel or "manual",
        "note": note or "",
        "counterparty": counterparty or "",
        "project": project or "",
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    drafts.append(draft)
    _save(data_dir, drafts)
    return draft


def get(data_dir: str | Path, pid: str) -> dict | None:
    for d in list_all(data_dir):
        if d["id"] == pid:
            return d
    return None


def update(data_dir: str | Path, pid: str, **fields) -> dict | None:
    """就地更新一条草稿的附加字段（date / source / image 等导入来源信息）。"""
    drafts = list_all(data_dir)
    for d in drafts:
        if d["id"] == pid:
            d.update(fields)
            _save(data_dir, drafts)
            return d
    return None


def accept(data_dir: str | Path, pid: str, append_fn,
           project_override: str | None = None,
           category_override: str | None = None) -> dict | None:
    """确认入账：从草稿弹出并回调写账本。

    append_fn(direction, amount_cents, category, channel, note, counterparty, project) -> (event, appended)
    project 用 .get 取，兼容 9/24 之前生成、还没有 project 字段的旧草稿。

    `project_override`（2026-09-26 新增，欠账 E 组 18）：**用户在确认那一刻挑的项目**，
    可传 id 或完整名；空字符串表示"就是未归项目"。归属必须能在确认环节被纠正 —— 草稿上的
    项目是生成时定下的，而用户不可能每次都准确把账放进对应账户（David 原话）。
    传 None 表示不改（沿用草稿上的值）。

    `category_override`（2026-09-28 新增）：**用户在确认那一刻挑的分类**。草稿分类是
    「待确认」（机器没判出来）时，就该由人在这里补上；传 None 表示沿用草稿分类
    （真分类照旧，占位符则被 `_ledger_category` 收成空串，见该函数）。
    """
    drafts = list_all(data_dir)
    for i, d in enumerate(drafts):
        if d["id"] == pid:
            proj = d.get("project", "") if project_override is None else project_override
            cat = d["category"] if category_override is None else category_override
            ev, appended = append_fn(d["direction"], d["amount_cents"],
                                     _ledger_category(cat, d["direction"]),
                                     d["channel"], d["note"], d.get("counterparty", ""),
                                     proj)
            # ⚠️ 只有**真写进账本**才把草稿弹掉。
            # 去重挡下时（账本里已有一笔一模一样的）如果照样删草稿，用户就同时失去
            # 草稿和账目 —— 静默丢数据。2026-09-28 真机：4 笔 ¥28 被去重挡下 3 笔，
            # 草稿全删了、界面还报「已确认入账 4 笔（支出 ¥112.00）」，账本只有 1 笔。
            # 留下的草稿会继续显示在「待确认」里，用户看得见、也还能改（或「不要」）。
            if appended:
                del drafts[i]
                _save(data_dir, drafts)
            return {"draft": d, "event": ev, "appended": appended}
    return None


def decline(data_dir: str | Path, pid: str, *, group: str = "") -> str | None:
    """丢弃一条草稿 —— **软删除**，进回收站，随时能撤回。

    用户 2026-09-28：「（已丢弃 2 笔待确认草稿，账本未变动…）这段小字是我删除记录时候的小字，
    后面增加一个撤回按钮」。要能撤回，就不能真删：给草稿打上 `discarded_at` 与 `discarded_group`，
    `list_all` 把它们过滤掉（界面上看不到），`restore` 按 id 或按 group 放回来。
    返回本次的 group（调用方拿它做撤回按钮）；草稿不存在返回 None。
    """
    drafts = _load_raw(data_dir)
    hit = next((d for d in drafts if d["id"] == pid and not d.get("discarded_at")), None)
    if hit is None:
        return None
    g = group or uuid.uuid4().hex[:12]
    hit["discarded_at"] = datetime.now().isoformat(timespec="seconds")
    hit["discarded_group"] = g
    _save(data_dir, drafts)
    return g


def restore(data_dir: str | Path, *, ids: list[str] | None = None,
            group: str | None = None) -> list[dict]:
    """撤回丢弃：按 id 或按 group 把草稿放回「待确认」。

    两个参数都没给就**什么都不做**（返回空）—— 宁可没反应，也不能"手一抖恢复全部"。
    """
    if not ids and not group:
        return []
    drafts = _load_raw(data_dir)
    out: list[dict] = []
    for d in drafts:
        if not d.get("discarded_at"):
            continue
        if (ids and d["id"] in ids) or (group and d.get("discarded_group") == group):
            d.pop("discarded_at", None)
            d.pop("discarded_group", None)
            out.append(d)
    if out:
        _save(data_dir, drafts)
    return out
