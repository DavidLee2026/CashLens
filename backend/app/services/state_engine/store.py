"""SQLite 派生投影（可重建缓存）

对应 v0.2 规格 §四：transactions / assumptions / state / events_meta。
重建规则：model_version 变化 / 文件损坏 / 显式请求 → 从空表全量重放。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .spec import SCHEMA_VERSION

MODEL_VERSION = "state-engine-v0.2-mvp"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    channel TEXT,
    category TEXT,
    counterparty TEXT,
    evidence_kind TEXT,
    evidence_weight REAL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS assumptions (
    assumption_id TEXT PRIMARY KEY,
    event_id TEXT,
    subject TEXT,
    expected TEXT,
    actual TEXT,
    status TEXT
);
CREATE TABLE IF NOT EXISTS state (
    as_of TEXT PRIMARY KEY,
    financial_health REAL,
    cashflow_confidence REAL,
    stability_days INTEGER,
    label TEXT,
    model_version TEXT
);
"""


class Projection:
    """SQLite 投影读写。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def needs_rebuild(self) -> bool:
        row = self.conn.execute("SELECT value FROM events_meta WHERE key='model_version'").fetchone()
        return row is None or row[0] != MODEL_VERSION

    def rebuild(self, events: list[dict]) -> None:
        """从空表全量重建投影（幂等：同一批事件 → 同一结果）。"""
        self.conn.executescript("""
            DELETE FROM transactions; DELETE FROM assumptions; DELETE FROM state;
            DELETE FROM events_meta;
        """)
        for ev in events:
            e = ev.get("evidence", {})
            self.conn.execute(
                """INSERT OR REPLACE INTO transactions
                   (event_id, ts, type, amount_cents, channel, category, counterparty,
                    evidence_kind, evidence_weight, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    ev["event_id"],
                    ev["ts"],
                    ev["type"],
                    ev["amount_cents"],
                    ev.get("channel"),
                    ev.get("category"),
                    ev.get("counterparty"),
                    e.get("kind"),
                    e.get("weight", 0.0),
                    ev.get("note", ""),
                ),
            )
            if ev["type"] == "assumption" and isinstance(ev.get("assumption"), dict):
                a = ev["assumption"]
                self.conn.execute(
                    """INSERT OR REPLACE INTO assumptions
                       (assumption_id, event_id, subject, expected, actual, status)
                       VALUES (?,?,?,?,?,?)""",
                    (ev["event_id"], ev["event_id"], a.get("subject"), a.get("expected"), None, "pending"),
                )
            if ev["type"] == "resolution":
                ref = (ev.get("extra") or {}).get("assumption_ref") or ev.get("assumption_ref")
                if ref:
                    actual = (ev.get("extra") or {}).get("actual")
                    expected_row = self.conn.execute(
                        "SELECT expected FROM assumptions WHERE assumption_id=?", (ref,)
                    ).fetchone()
                    status = "resolved" if expected_row and actual == expected_row[0] else "mismatch"
                    self.conn.execute(
                        "UPDATE assumptions SET actual=?, status=? WHERE assumption_id=?",
                        (actual, status, ref),
                    )
        self.conn.executemany(
            "INSERT OR REPLACE INTO events_meta(key,value) VALUES (?,?)",
            [("model_version", MODEL_VERSION), ("schema_version", str(SCHEMA_VERSION)),
             ("projected_at", datetime.now().isoformat(timespec="seconds"))],
        )
        self.conn.commit()

    def upsert_state(self, as_of: str, state: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO state
               (as_of, financial_health, cashflow_confidence, stability_days, label, model_version)
               VALUES (?,?,?,?,?,?)""",
            (as_of, state["financial_health"], state["cashflow_confidence"],
             state["stability_days"], state["label"], MODEL_VERSION),
        )
        self.conn.commit()

    def assumptions_status(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM assumptions").fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM assumptions").description]
        return [dict(zip(cols, r)) for r in rows]

    def close(self) -> None:
        self.conn.close()
