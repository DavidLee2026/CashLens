"""账本事件 ↔ 原始票据文件（2026-10-20 立）

对外那句话是「每一笔都能追到那张票据」。账本事件里只有 `evidence.kind`，没有文件名，
所以另存一张只追加的对照表：事件 id → 文件名。

三条纪律，各有用例钉住：
1. **不改账本**：只在对照表追加，账本 JSONL 一个字节不动；
2. **老事件如实**：没有对照就不加字段（前端显示"来源未记录"），不补造；
3. **文件没了也要能追**：对照表存的是文件名，原文件被清理时仍能说出"这笔来自哪张票"。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import evidence_files, pending  # noqa: E402


def test_record_then_lookup(tmp_path):
    evidence_files.record(tmp_path, "e1", "票据示例_01.jpg")
    assert evidence_files.load_map(tmp_path) == {"e1": "票据示例_01.jpg"}


def test_same_event_is_idempotent(tmp_path):
    evidence_files.record(tmp_path, "e1", "a.jpg")
    evidence_files.record(tmp_path, "e1", "b.jpg")
    assert evidence_files.load_map(tmp_path) == {"e1": "a.jpg"}, "同一事件只认第一条，不静默改写"


def test_blank_values_are_ignored(tmp_path):
    evidence_files.record(tmp_path, "", "a.jpg")
    evidence_files.record(tmp_path, "e1", "")
    assert evidence_files.load_map(tmp_path) == {}


def test_attach_only_touches_recorded_events(tmp_path):
    evidence_files.record(tmp_path, "e1", "a.jpg")
    evs = [{"event_id": "e1"}, {"event_id": "e2"}]
    evidence_files.attach(evs, evidence_files.load_map(tmp_path))
    assert evs[0]["evidence_file"] == "a.jpg"
    assert "evidence_file" not in evs[1], "没有对照就不许加字段（不许编「来源未知」之外的东西）"


def test_accept_records_source_file(tmp_path):
    """确认入账时把草稿上的 image 记进对照表 —— 账本本身不变。"""
    d = pending.create(tmp_path, direction="expense", amount_cents=1200,
                       category="经营", channel="receipt", note="打印", project="")
    pending.update(tmp_path, d["id"], image="票据_01.png")
    ledger_before = (tmp_path / "finance_events.jsonl").read_text(encoding="utf-8") if \
        (tmp_path / "finance_events.jsonl").exists() else None

    def fake_append(direction, amount_cents, category, channel, note, counterparty, project):
        return {"event_id": "ev-1", "amount_cents": amount_cents}, True

    out = pending.accept(tmp_path, d["id"], fake_append)
    assert out["appended"] is True
    assert evidence_files.load_map(tmp_path) == {"ev-1": "票据_01.png"}
    # 账本没有被这次操作碰过（accept 只回调 append_fn，测试里那个是假的）
    after = (tmp_path / "finance_events.jsonl").read_text(encoding="utf-8") if \
        (tmp_path / "finance_events.jsonl").exists() else None
    assert after == ledger_before
