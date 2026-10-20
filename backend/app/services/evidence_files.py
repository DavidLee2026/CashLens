"""账本事件 ↔ 原始票据文件的对照表（2026-10-20 立）

## 为什么单独存一张表

对外那句话是：「每一笔都能追到那张票据」。但账本事件里只有
`evidence: {kind, weight, confirmed}`——知道"这笔是票据来的"，不知道"是哪张票"。

两种补法：
- ✗ 改历史事件：给老事件补字段 = 重写 JSONL 行，违反「账本只追加」；
- ✓ 另存一张对照表：事件 id → 文件名，追加式，老事件没有就是没有。

所以这张表只做一件事：记下「这个 event 来自哪个文件」。查不到就如实说"来源未记录"。

## 纪律

- 只追加，不改不删（同一 event 重复写只保留第一条）
- 只存**文件名**（`_intake/` 里的相对名），不存绝对路径、不存内容
- 文件本身缺失时，界面照样显示文件名，但点开要如实说"原文件已被清理"
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_FILE = "evidence_files.jsonl"


def _path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _FILE


def record(data_dir: str | Path, event_id: str, file_name: str) -> None:
    """记一条对照（幂等：同一 event_id 只留第一条）。"""
    eid = (event_id or "").strip()
    name = (file_name or "").strip()
    if not eid or not name:
        return
    if eid in load_map(data_dir):
        return
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"event_id": eid, "file": name,
           "recorded_at": datetime.now().isoformat(timespec="seconds")}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_map(data_dir: str | Path) -> dict[str, str]:
    """event_id → 文件名。文件不存在/坏了都返回空表，不抛异常。"""
    p = _path(data_dir)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = str(row.get("event_id") or "")
            name = str(row.get("file") or "")
            if eid and name and eid not in out:
                out[eid] = name
    except OSError:
        return {}
    return out


def attach(events: list[dict], mapping: dict[str, str]) -> list[dict]:
    """把对照表贴到事件上（`evidence_file` 字段）。没记录的**不加字段**，前端显示"来源未记录"。"""
    for ev in events or []:
        name = mapping.get(str(ev.get("event_id") or ""))
        if name:
            ev["evidence_file"] = name
    return events
