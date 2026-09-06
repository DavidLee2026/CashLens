"""待确认入账草稿（识别 → 人确认 → 入账）

流程：/api/chat 记账只生成草稿（不入账）→ 用户点「确认入账」→ accept 写进事件账本；
点「不要这笔」→ decline 丢弃。数据放运行时目录 data/pending.json（不入库）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path


def _path(data_dir: str | Path) -> Path:
    p = Path(data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / "pending.json"


def list_all(data_dir: str | Path) -> list[dict]:
    fp = _path(data_dir)
    if not fp.exists():
        return []
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save(data_dir: str | Path, drafts: list[dict]) -> None:
    fp = _path(data_dir)
    fp.write_text(json.dumps(drafts, ensure_ascii=False, indent=1), encoding="utf-8")


def create(data_dir: str | Path, *, direction: str, amount_cents: int, category: str,
           channel: str, note: str, counterparty: str = "") -> dict:
    drafts = list_all(data_dir)
    draft = {
        "id": uuid.uuid4().hex[:12],
        "direction": direction,
        "amount_cents": amount_cents,
        "category": category or "其他",
        "channel": channel or "manual",
        "note": note or "",
        "counterparty": counterparty or "",
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


def accept(data_dir: str | Path, pid: str, append_fn) -> dict | None:
    """确认入账：从草稿弹出并回调写账本。append_fn(direction, amount_cents, category, channel, note, counterparty) -> (event, appended)"""
    drafts = list_all(data_dir)
    for i, d in enumerate(drafts):
        if d["id"] == pid:
            ev, appended = append_fn(d["direction"], d["amount_cents"], d["category"],
                                     d["channel"], d["note"], d["counterparty"])
            del drafts[i]
            _save(data_dir, drafts)
            return {"draft": d, "event": ev, "appended": appended}
    return None


def decline(data_dir: str | Path, pid: str) -> bool:
    drafts = list_all(data_dir)
    n = len(drafts)
    drafts = [d for d in drafts if d["id"] != pid]
    if len(drafts) != n:
        _save(data_dir, drafts)
        return True
    return False
