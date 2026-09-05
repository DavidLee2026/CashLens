"""事件账本（不可变，追加式 jsonl）

对应 v0.2 规格 §二：finance_events.jsonl，schema_version=2。
金额一律用分（整数 amount_cents），杜绝浮点误差。
去重键 dedupe_key = sha256(channel|amount_cents|YYYY-MM-DD|category|counterparty)。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .spec import EVENT_TYPES, SCHEMA_VERSION


def _dedupe_key_of(event: dict) -> str:
    """按 v0.2 规格生成去重键（渠道+金额+日期+分类+对手方）。"""
    ts = _parse_ts(event.get("ts"))
    day = ts.date().isoformat()
    raw = "|".join(
        [
            str(event.get("channel", "")),
            str(event.get("amount_cents", 0)),
            day,
            str(event.get("category", "")),
            str(event.get("counterparty", "") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_ts(ts) -> datetime:
    """容忍 str / datetime 的解析（本地时区口径，MVP 不做时区换算）。"""
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)


def make_event(
    *,
    ts,
    event_type: str,
    amount_cents: int,
    evidence_kind: str,
    channel: str = "manual",
    category: str = "",
    counterparty: str = "",
    note: str = "",
    confirmed: bool = False,
    extra: dict | None = None,
) -> dict:
    """构造一条规范事件（recorded_at 自动补当前时间，evidence 带权重）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知事件类型: {event_type}")
    if amount_cents < 0:
        raise ValueError("amount_cents 不能为负（方向用 type=expense 表达）")
    if amount_cents == 0 and evidence_kind not in ("self_report", "guess"):
        raise ValueError("0 金额仅允许 self_report/guess 类语句事件（如『我这月没问题』）")
    weights = {"flow": 1.00, "receipt": 0.85, "voice": 0.75, "guided": 0.45, "self_report": 0.10, "guess": 0.00}
    if evidence_kind not in weights:
        raise ValueError(f"未知证据类型: {evidence_kind}")
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "ts": ts.isoformat() if isinstance(ts, datetime) else ts,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "type": event_type,
        "amount_cents": int(amount_cents),
        "channel": channel,
        "category": category,
        "counterparty": counterparty,
        "note": note,
        "evidence": {"kind": evidence_kind, "weight": weights[evidence_kind], "confirmed": bool(confirmed)},
    }
    if extra:
        event.update(extra)
    event["dedupe_key"] = _dedupe_key_of(event)
    return event


class EventLedger:
    """finance_events.jsonl 的追加式读写。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict, strict_dedupe: bool = True) -> dict:
        """追加写入；strict_dedupe=True 时已存在同 dedupe_key 的事件不再写入。"""
        if strict_dedupe and self._contains(event.get("dedupe_key")):
            return {"event_id": event.get("event_id"), "dedupe_key": event.get("dedupe_key"), "appended": False}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"event_id": event.get("event_id"), "dedupe_key": event.get("dedupe_key"), "appended": True}

    def _contains(self, dedupe_key: str) -> bool:
        if not dedupe_key or not self.path.exists():
            return False
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if json.loads(line).get("dedupe_key") == dedupe_key:
                return True
        return False

    def load(self) -> list[dict]:
        """读全部事件（去重键优先保留先写者），按 ts 升序返回。"""
        if not self.path.exists():
            return []
        events = []
        seen = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            key = ev.get("dedupe_key") or ev.get("event_id")
            if key in seen:
                continue
            seen.add(key)
            events.append(ev)
        events.sort(key=lambda e: _parse_ts(e.get("ts")))
        return events


def interval_days(a: datetime, b: datetime) -> int:
    """两个时间点之间相隔的天数（非负）。"""
    return max(0, (b - a).days) if a < b else max(0, (a - b).days)
