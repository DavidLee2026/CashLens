"""项目详情（`GET /api/projects/{pid}/detail`）的口径测试。

为什么单测这一块：详情弹窗要和左栏"总览"显示同一套数字。**同一个项目在两处显示不同金额
是硬伤**，所以详情必须复用 `summary()` 的去重口径（支出侧同号发票只计一次），这里把这个
"必须一致"钉成断言。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import projects  # noqa: E402
from app.services.state_engine import make_event  # noqa: E402


def _ev(direction, cents, category, project, ts, invoice_no="", note=""):
    return make_event(ts=ts, event_type=direction, amount_cents=cents, evidence_kind="voice",
                      channel="manual", category=category, note=note or category,
                      confirmed=True, project=project,
                      extra={"invoice_no": invoice_no} if invoice_no else None)


def _book(tmp_path, *events):
    """把事件写进临时账本，返回 (data_dir, events)。"""
    from app.services.state_engine import EventLedger
    led = EventLedger(tmp_path / "finance_events.jsonl")
    for e in events:
        led.append(e, strict_dedupe=False)
    return tmp_path, list(events)


def test_detail_matches_summary_totals(tmp_path):
    """详情里的合计必须与 summary 一致（同一套去重口径）。"""
    from datetime import datetime
    p = projects.create(tmp_path, "919 昆明项目")
    d = str(tmp_path)
    events = [
        _ev("expense", 12862, "交通", p["id"], "2026-09-20T10:00:00", invoice_no="A1"),
        _ev("expense", 12862, "交通", p["id"], "2026-09-20T11:00:00", invoice_no="A1"),  # 同号，应被去重
        _ev("expense", 5000, "餐饮", p["id"], "2026-09-21T12:00:00", invoice_no="A2"),
        _ev("income", 300000, "接单", p["id"], "2026-09-22T09:00:00"),
    ]
    tmp_path, events = _book(tmp_path, *events)
    s = projects.summary(d, events, project=p["id"])["projects"][0]
    dt = projects.detail(d, events, p["id"])
    assert dt["ok"] is True
    assert dt["totals"]["expense_cents"] == s["expense_cents"] == 17862      # 去重后
    assert dt["totals"]["income_cents"] == s["income_cents"] == 300000
    assert dt["totals"]["net_cents"] == s["net_cents"]
    assert dt["totals"]["count"] == 3                                        # 3 笔计入（1 笔重复被剔除）


def test_detail_income_and_expense_breakdown(tmp_path):
    from datetime import datetime
    p = projects.create(tmp_path, "919 昆明项目")
    d = str(tmp_path)
    events = [
        _ev("expense", 6000, "交通", p["id"], "2026-09-20T10:00:00"),
        _ev("expense", 4000, "餐饮", p["id"], "2026-09-20T11:00:00"),
        _ev("expense", 1000, "餐饮", p["id"], "2026-09-21T11:00:00"),
        _ev("income", 100000, "接单", p["id"], "2026-09-22T09:00:00"),
        _ev("income", 20000, "退款", p["id"], "2026-09-23T09:00:00"),
    ]
    tmp_path, events = _book(tmp_path, *events)
    dt = projects.detail(d, events, p["id"])
    # 支出按金额倒序，同分类合并
    assert [(c["category"], c["amount_cents"], c["count"]) for c in dt["expense_by_category"]] == [
        ("交通", 6000, 1), ("餐饮", 5000, 2)]
    # 收入侧也有构成（2026-09-26 新增）
    assert [(c["category"], c["amount_cents"]) for c in dt["income_by_category"]] == [
        ("接单", 100000), ("退款", 20000)]


def test_detail_invoices_deduped_and_listed(tmp_path):
    from datetime import datetime
    p = projects.create(tmp_path, "919 昆明项目")
    d = str(tmp_path)
    events = [
        _ev("expense", 96200, "住宿", p["id"], "2026-09-20T10:00:00", invoice_no="26537000000118274568"),
        _ev("expense", 96200, "住宿", p["id"], "2026-09-20T11:00:00", invoice_no="26537000000118274568"),
        _ev("expense", 3000, "交通", p["id"], "2026-09-21T10:00:00"),          # 无发票号：不参与去重
    ]
    tmp_path, events = _book(tmp_path, *events)
    dt = projects.detail(d, events, p["id"])
    re_ = dt["reimbursement"]
    assert re_["invoice_count"] == 1                     # 同一号码只算一张
    assert re_["duplicate_count"] == 1                   # 重复的那笔如实报出来
    assert re_["no_duplicate"] is False
    assert re_["invoices"][0]["invoice_no"] == "26537000000118274568"
    assert re_["invoices"][0]["amount_cents"] == 96200


def test_detail_recent_and_range(tmp_path):
    p = projects.create(tmp_path, "919 昆明项目")
    d = str(tmp_path)
    events = [
        _ev("expense", 1000, "交通", p["id"], "2026-09-20T10:00:00", note="最早"),
        _ev("expense", 2000, "交通", p["id"], "2026-09-25T10:00:00", note="最晚"),
    ]
    tmp_path, events = _book(tmp_path, *events)
    dt = projects.detail(d, events, p["id"])
    assert dt["range"]["first_date"] == "2026-09-20"
    assert dt["range"]["last_date"] == "2026-09-25"
    assert [r["note"] for r in dt["recent"]] == ["最晚", "最早"]      # 倒序
    assert dt["recent"][0]["amount_cents"] == 2000
    assert dt["recent"][0]["evidence"] == "voice"


def test_detail_only_counts_own_project(tmp_path):
    """详情只算这个项目自己的账（别人的不能混进来）。"""
    a = projects.create(tmp_path, "A 项目")
    b = projects.create(tmp_path, "B 项目")
    d = str(tmp_path)
    events = [
        _ev("expense", 1000, "交通", a["id"], "2026-09-20T10:00:00"),
        _ev("expense", 9999, "交通", b["id"], "2026-09-20T10:00:00"),
        _ev("expense", 777, "交通", "", "2026-09-20T10:00:00"),            # 未归项目
    ]
    tmp_path, events = _book(tmp_path, *events)
    dt = projects.detail(d, events, a["id"])
    assert dt["totals"]["expense_cents"] == 1000
    assert dt["project"]["name"] == "A 项目"


def test_detail_unassigned_bucket(tmp_path):
    """未归项目也能看详情（删项目之后账单回落的地方）。"""
    d = str(tmp_path)
    events = [_ev("expense", 888, "交通", "", "2026-09-20T10:00:00")]
    tmp_path, events = _book(tmp_path, *events)
    dt = projects.detail(d, events, "未归项目")
    assert dt["ok"] is True
    assert dt["project"]["id"] == ""
    assert dt["totals"]["expense_cents"] == 888


def test_detail_empty_project_is_all_zero(tmp_path):
    p = projects.create(tmp_path, "刚建的项目")
    d = str(tmp_path)
    tmp_path, events = _book(tmp_path)
    dt = projects.detail(d, events, p["id"])
    assert dt["ok"] is True
    assert dt["totals"] == {"income_cents": 0, "expense_cents": 0, "net_cents": 0,
                            "income_count": 0, "expense_count": 0, "count": 0}
    assert dt["expense_by_category"] == [] and dt["recent"] == []
    assert dt["range"] == {"first_date": "", "last_date": ""}
    assert dt["reimbursement"]["no_duplicate"] is True          # 没有重复，不能说有


def test_detail_unknown_project_reports_error(tmp_path):
    tmp_path, events = _book(tmp_path)
    out = projects.detail(str(tmp_path), events, "不存在的项目")
    assert out["ok"] is False and "找不到项目" in out["error"]


def test_detail_deleted_project_bills_fall_back_to_unassigned(tmp_path):
    """删掉项目后，它名下的账单要出现在「未归项目」详情里，数字不能消失。"""
    p = projects.create(tmp_path, "临时项目")
    d = str(tmp_path)
    events = [_ev("expense", 1234, "交通", p["id"], "2026-09-20T10:00:00")]
    tmp_path, events = _book(tmp_path, *events)
    projects.mark_deleted(d, p["id"], purged_count=0, with_data=False)
    un = projects.detail(d, events, "未归项目")
    assert un["totals"]["expense_cents"] == 1234
