"""项目对账（2026-10-20，A 档 ②）

对账要回答的是「账本、账单、票据三者对不对得上」，所以这组用例钉四件事：
① 账面口径必须与 `projects.detail` 一致（不另算一套）；
② 没导入过账单时不许拿 0 冒充「到账 0 元」（`available=false`）；
③ 票据缺口要数得清，并点名是哪几笔；
④ 关账只写一个时间戳，**账本一个字节不动**。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import projects as P, reconcile as R  # noqa: E402


def _env(tmp_path: Path) -> Path:
    rows = [{"id": "p1", "name": "秋季市集", "named": True, "created": "2026-10-01T09:00:00"}]
    (tmp_path / "projects.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    acts = [
        # 收入：一笔来自微信账单（有账单凭证），一笔手动（无凭证）
        {"event_id": "i1", "type": "income", "amount_cents": 1860000, "ts": "2026-10-10T10:00:00",
         "category": "接单", "channel": "wechat", "project": "p1"},
        {"event_id": "i2", "type": "income", "amount_cents": 200000, "ts": "2026-10-11T10:00:00",
         "category": "接单", "channel": "manual", "project": "p1"},
        # 支出：一笔有发票号、一笔有原文件、一笔什么都没有
        {"event_id": "e1", "type": "expense", "amount_cents": 96200, "ts": "2026-10-12T10:00:00",
         "category": "住宿", "channel": "receipt", "project": "p1", "invoice_no": "INV1"},
        {"event_id": "e2", "type": "expense", "amount_cents": 50000, "ts": "2026-10-13T10:00:00",
         "category": "餐饮", "channel": "receipt", "project": "p1"},
        {"event_id": "e3", "type": "expense", "amount_cents": 30000, "ts": "2026-10-14T10:00:00",
         "category": "交通", "channel": "voice", "project": "p1"},
    ]
    (tmp_path / "finance_events.jsonl").write_text(
        "\n".join(json.dumps(a, ensure_ascii=False) for a in acts) + "\n", encoding="utf-8")
    return tmp_path


def _events(d: Path) -> list[dict]:
    from app.services.state_engine.ledger import EventLedger
    return EventLedger(d / "finance_events.jsonl").load()


def test_book_matches_detail(tmp_path):
    d = _env(tmp_path)
    ev = _events(d)
    out = R.reconcile(d, ev, "p1")
    detail = P.detail(d, ev, "p1")
    assert out["book"]["income_cents"] == detail["totals"]["income_cents"]
    assert out["book"]["expense_cents"] == detail["totals"]["expense_cents"]
    assert out["book"]["count"] == detail["totals"]["count"]


def test_bill_column_only_when_available(tmp_path):
    d = _env(tmp_path)
    ev = _events(d)
    out = R.reconcile(d, ev, "p1")
    assert out["bill"]["available"] is True
    assert out["bill"]["income_cents"] == 1860000, "只有微信那笔算有账单凭证"
    assert out["bill"]["channels"] == ["微信"]
    # 差额：账面 2,060,000 - 账单 1,860,000
    assert out["diff"]["book_vs_bill_cents"] == 200000


def test_bill_column_hidden_when_nothing_imported(tmp_path):
    d = _env(tmp_path)
    ev = _events(d)
    for e in ev:
        e["channel"] = "manual"
    out = R.reconcile(d, ev, "p1")
    assert out["bill"]["available"] is False
    assert out["diff"]["book_vs_bill_cents"] is None, "没有账单凭证就必须是 null，不是 0"
    assert any("不是「到账 0 元」" in n for n in out["notes"])


def test_docs_gap_counts_and_names(tmp_path):
    d = _env(tmp_path)
    ev = _events(d)
    mapping = {"e2": "票据示例_02.jpg"}
    out = R.reconcile(d, ev, "p1", evidence_map=mapping)
    assert out["docs"]["total"] == 5
    # 带票的只有 e1（发票号）与 e2（原文件）；「微信账单通道」算账单凭证，不算票
    assert out["docs"]["with_doc"] == 2
    assert out["docs"]["missing"] == 3
    assert {x["event_id"] for x in out["docs"]["missing_items"]} == {"i1", "i2", "e3"}


def test_close_writes_timestamp_and_leaves_ledger_alone(tmp_path):
    d = _env(tmp_path)
    before = (d / "finance_events.jsonl").read_bytes()
    row = P.close_project(d, "p1", closed=True)
    assert row and row["closed_at"]
    assert (d / "finance_events.jsonl").read_bytes() == before, "关账不许动账本"
    # 再取详情与对账，closed_at 要能带出来
    ev = _events(d)
    assert P.detail(d, ev, "p1")["project"]["closed_at"]
    assert R.reconcile(d, ev, "p1")["project"]["closed_at"]
    # 取消关账
    P.close_project(d, "p1", closed=False)
    assert P.detail(d, _events(d), "p1")["project"]["closed_at"] == ""
