"""项目维度单测：映射表 / 默认命名 / 归集 / 报销发票去重。用临时目录，不碰真实数据。

重点覆盖两个容易踩的坑：
1. 重命名不能动账本（账本不可变是产品红线）。
2. project 必须进去重键，否则同一天同金额同分类的两笔支出落在不同项目时，第二笔会被误杀。
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import projects  # noqa: E402
from app.services.state_engine import EventLedger, make_event  # noqa: E402


def _ev(ts, etype, cents, *, project="", category="", channel="manual",
        counterparty="", invoice_no="", evidence="receipt"):
    """造一条测试事件。invoice_no 走 extra，模拟识别层带回来的发票号码。"""
    extra = {"invoice_no": invoice_no} if invoice_no else None
    return make_event(ts=ts, event_type=etype, amount_cents=cents, evidence_kind=evidence,
                      channel=channel, category=category, counterparty=counterparty,
                      project=project, confirmed=True, extra=extra)


def _book(tmp_path):
    return EventLedger(tmp_path / "finance_events.jsonl")


# ─── 映射表与默认命名 ───────────────────────────────────

def test_create_default_names_then_rename(tmp_path):
    a = projects.create(tmp_path)
    assert a["name"] == "Project A" and a["named"] is False
    assert "919" in a["name_hint"] or "完整名称" in a["name_hint"]  # 会提醒补全项目名

    b = projects.create(tmp_path, "919 昆明项目")
    assert b["name"] == "919 昆明项目" and b["named"] is True

    c = projects.create(tmp_path)
    assert c["name"] == "Project C"  # 已存在 2 个，第三个默认 C

    row = projects.rename(tmp_path, a["id"], "924 豫园灯会")
    assert row["name"] == "924 豫园灯会" and row["named"] is True
    assert projects.get(tmp_path, a["id"])["name"] == "924 豫园灯会"


def test_rename_by_display_name_also_works(tmp_path):
    projects.create(tmp_path, "919 昆明项目")
    pid = projects.resolve(tmp_path, "919 昆明项目")
    assert pid is not None
    assert projects.rename(tmp_path, pid, "919 昆明出差")["name"] == "919 昆明出差"


def test_rename_rejects_empty_name(tmp_path):
    a = projects.create(tmp_path)
    try:
        projects.rename(tmp_path, a["id"], "   ")
    except ValueError as e:
        assert "不能为空" in str(e)
    else:
        raise AssertionError("空名字应当被拒绝")


def test_list_projects_flags_unnamed(tmp_path):
    projects.create(tmp_path)
    out = projects.list_projects(tmp_path, [])
    assert out["project_count"] == 1
    assert out["unnamed_project_ids"]
    assert out["naming_hint"]
    assert out["unassigned"]["name"] == projects.UNASSIGNED_LABEL


# ─── 红线：重命名不动账本 ───────────────────────────────

def test_rename_does_not_touch_ledger(tmp_path):
    a = projects.create(tmp_path)
    book = _book(tmp_path)
    book.append(_ev(datetime(2026, 9, 19), "expense", 4713, project=a["id"], category="打车"),
                strict_dedupe=True)
    ledger_file = tmp_path / "finance_events.jsonl"
    before = ledger_file.read_text(encoding="utf-8")

    projects.rename(tmp_path, a["id"], "919 昆明项目")

    assert ledger_file.read_text(encoding="utf-8") == before  # 账本一个字节都没动
    out = projects.summary(tmp_path, book.load())
    assert out["projects"][0]["name"] == "919 昆明项目"        # 显示新名
    assert out["projects"][0]["project"] == a["id"]           # 账本里仍是稳定 id


# ─── 关键坑：project 进去重键 ───────────────────────────

def test_project_in_dedupe_key_and_schema_v3(tmp_path):
    ts = datetime(2026, 9, 19, 12, 0)
    e1 = _ev(ts, "expense", 100, project="project_a")
    e2 = _ev(ts, "expense", 100, project="project_b")
    assert e1["schema_version"] == 3
    assert e1["project"] == "project_a"
    assert e1["dedupe_key"] != e2["dedupe_key"]


def test_same_day_same_amount_across_projects_not_deduped(tmp_path):
    """不同项目 = 不同归属 = 两笔合法事件，绝不能当成重复丢掉。"""
    a = projects.create(tmp_path, "项目A")
    b = projects.create(tmp_path, "项目B")
    book = _book(tmp_path)
    ts = datetime(2026, 9, 19, 12, 0)
    r1 = book.append(_ev(ts, "expense", 4713, project=a["id"], category="打车"), strict_dedupe=True)
    r2 = book.append(_ev(ts, "expense", 4713, project=b["id"], category="打车"), strict_dedupe=True)
    assert r1["appended"] is True
    assert r2["appended"] is True
    assert len(book.load()) == 2


def test_same_project_same_day_same_amount_still_deduped(tmp_path):
    """同一个项目里同日同额同分类，重复语义不变，仍然只记一次。"""
    a = projects.create(tmp_path, "项目A")
    book = _book(tmp_path)
    ts = datetime(2026, 9, 19, 12, 0)
    r1 = book.append(_ev(ts, "expense", 4713, project=a["id"], category="打车"), strict_dedupe=True)
    r2 = book.append(_ev(ts, "expense", 4713, project=a["id"], category="打车"), strict_dedupe=True)
    assert r1["appended"] is True
    assert r2["appended"] is False


def test_legacy_v2_event_without_project_still_readable(tmp_path):
    """v2 旧账目没有 project 字段，归集时按未归项目处理，不报错。"""
    import json
    ledger_file = tmp_path / "finance_events.jsonl"
    ledger_file.write_text(json.dumps({
        "schema_version": 2, "event_id": "old1", "ts": "2026-09-01T10:00:00",
        "recorded_at": "2026-09-01T10:00:00", "type": "expense", "amount_cents": 2800,
        "channel": "manual", "category": "交通", "counterparty": "", "note": "",
        "evidence": {"kind": "voice", "weight": 0.75, "confirmed": True},
        "dedupe_key": "legacykey",
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    out = projects.summary(tmp_path, EventLedger(ledger_file).load())
    assert out["total"]["expense_cents"] == 2800
    assert out["projects"][0]["project"] == ""
    assert out["projects"][0]["name"] == projects.UNASSIGNED_LABEL


# ─── 报销口径：发票号码去重 ─────────────────────────────

def test_reimbursement_dedup_by_invoice_no(tmp_path):
    a = projects.create(tmp_path, "919 昆明项目")
    book = _book(tmp_path)
    no = "26537000000118274568"
    book.append(_ev(datetime(2026, 9, 20), "expense", 96200, project=a["id"],
                    category="住宿", channel="receipt", invoice_no=no), strict_dedupe=True)
    # 渠道不同 → 去重键不同 → 第二笔确实进了账本，靠发票号码在归集时剔除
    book.append(_ev(datetime(2026, 9, 20), "expense", 96200, project=a["id"],
                    category="住宿", channel="wechat", invoice_no=no), strict_dedupe=True)
    assert len(book.load()) == 2

    out = projects.summary(tmp_path, book.load())
    g = out["projects"][0]
    assert g["expense_cents"] == 96200                    # 只计一次
    assert g["expense_count"] == 1
    assert g["reimbursement"]["duplicate_count"] == 1
    assert g["reimbursement"]["no_duplicate"] is False
    assert g["duplicates"][0]["invoice_no"] == no


def test_reimbursement_clean_case_reports_no_duplicate(tmp_path):
    a = projects.create(tmp_path, "919 昆明项目")
    book = _book(tmp_path)
    for i, cents in enumerate([4713, 3754, 4395]):
        book.append(_ev(datetime(2026, 9, 19 + i), "expense", cents, project=a["id"],
                        category="打车", channel="wechat", invoice_no=f"no{i}"), strict_dedupe=True)

    out = projects.summary(tmp_path, book.load())
    g = out["projects"][0]
    assert g["expense_cents"] == 4713 + 3754 + 4395
    assert g["reimbursement"]["no_duplicate"] is True
    assert g["reimbursement"]["duplicate_count"] == 0


def test_events_without_invoice_no_are_not_deduped(tmp_path):
    """没记发票号码的支出不参与去重（不推断），两笔都算。"""
    a = projects.create(tmp_path, "项目A")
    book = _book(tmp_path)
    book.append(_ev(datetime(2026, 9, 19), "expense", 3000, project=a["id"],
                    category="餐饮", channel="wechat"), strict_dedupe=True)
    book.append(_ev(datetime(2026, 9, 20), "expense", 3000, project=a["id"],
                    category="餐饮", channel="wechat"), strict_dedupe=True)
    out = projects.summary(tmp_path, book.load())
    assert out["projects"][0]["expense_cents"] == 6000


# ─── 入账时的项目归属 ───────────────────────────────────

def test_resolve_incoming_defaults_to_first_project(tmp_path):
    pid, hint = projects.resolve_incoming(tmp_path, None)
    assert pid and "Project A" in hint and "重命名" in hint
    pid2, hint2 = projects.resolve_incoming(tmp_path, None)
    assert pid2 == pid and hint2  # 第二次仍落同一个默认项目


def test_resolve_incoming_creates_project_named_by_user(tmp_path):
    pid, hint = projects.resolve_incoming(tmp_path, "919 昆明项目")
    assert projects.get(tmp_path, pid)["name"] == "919 昆明项目"
    assert "919 昆明项目" in hint
    assert len(projects.list_projects(tmp_path, [])["projects"]) == 1


def test_resolve_incoming_accepts_existing_id_or_name(tmp_path):
    a = projects.create(tmp_path, "919 昆明项目")
    assert projects.resolve_incoming(tmp_path, a["id"])[0] == a["id"]
    assert projects.resolve_incoming(tmp_path, "919 昆明项目")[0] == a["id"]


# ─── 归集汇总 ───────────────────────────────────────────

def test_summary_groups_and_unassigned(tmp_path):
    a = projects.create(tmp_path, "项目A")
    book = _book(tmp_path)
    book.append(_ev(datetime(2026, 9, 19), "expense", 4713, project=a["id"], category="打车"),
                strict_dedupe=True)
    book.append(_ev(datetime(2026, 9, 20), "income", 300000, project=a["id"], category="接单"),
                strict_dedupe=True)
    book.append(_ev(datetime(2026, 9, 21), "expense", 2000, project="", category="餐饮"),
                strict_dedupe=True)

    out = projects.summary(tmp_path, book.load())
    names = {p["name"]: p for p in out["projects"]}
    assert names["项目A"]["expense_cents"] == 4713
    assert names["项目A"]["income_cents"] == 300000
    assert names["项目A"]["net_cents"] == 300000 - 4713
    assert names["项目A"]["by_category"][0]["category"] == "打车"
    assert projects.UNASSIGNED_LABEL in names
    assert out["total"]["expense_cents"] == 6713
    assert out["total"]["income_cents"] == 300000


def test_summary_filters_by_project_and_rejects_unknown(tmp_path):
    a = projects.create(tmp_path, "项目A")
    b = projects.create(tmp_path, "项目B")
    book = _book(tmp_path)
    book.append(_ev(datetime(2026, 9, 19), "expense", 1000, project=a["id"]), strict_dedupe=True)
    book.append(_ev(datetime(2026, 9, 20), "expense", 2000, project=b["id"]), strict_dedupe=True)

    one = projects.summary(tmp_path, book.load(), project="项目A")
    assert one["project_count"] == 1
    assert one["projects"][0]["expense_cents"] == 1000

    by_id = projects.summary(tmp_path, book.load(), project=b["id"])
    assert by_id["projects"][0]["expense_cents"] == 2000

    bad = projects.summary(tmp_path, book.load(), project="查无此项目")
    assert bad["ok"] is False and "找不到项目" in bad["error"]


def test_summary_can_isolate_unassigned(tmp_path):
    a = projects.create(tmp_path, "项目A")
    book = _book(tmp_path)
    book.append(_ev(datetime(2026, 9, 19), "expense", 1000, project=a["id"]), strict_dedupe=True)
    book.append(_ev(datetime(2026, 9, 20), "expense", 2000, project=""), strict_dedupe=True)

    out = projects.summary(tmp_path, book.load(), project=projects.UNASSIGNED_LABEL)
    assert out["project_count"] == 1
    assert out["projects"][0]["project"] == ""
    assert out["total"]["expense_cents"] == 2000


def test_summary_backfills_missing_project_row(tmp_path):
    """账本里出现了映射表没有的项目 id（例如手工写账本），归集时自动补登不丢账。"""
    book = _book(tmp_path)
    book.append(_ev(datetime(2026, 9, 19), "expense", 1500, project="project_z"),
                strict_dedupe=True)
    out = projects.summary(tmp_path, book.load())
    assert out["total"]["expense_cents"] == 1500
    assert projects.get(tmp_path, "project_z") is not None
