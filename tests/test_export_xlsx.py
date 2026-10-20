"""导出 Excel（2026-10-19 立）

起因：对账的交付物是**一张表**，而此前 27 个端点里一个导出都没有 —— 账只能看、不能带走。

这组用例钉四件事（任何一条回退都会让客户数不出来）：
1. 金额是**数字**、单位是**元**（客户要在 Excel 里自己求和）；
2. **同号重复发票**在明细里看得见、但不进合计（与工作台同口径）；
3. 选项目导出只含该项目；
4. 空账本也要出一张能打开的表（表头 + 0），不是报错。
"""
import io
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import export_xlsx, projects  # noqa: E402


def _ev(eid, typ, cents, day, cat="交通", note="", invoice="", project=""):
    return {"event_id": eid, "type": typ, "amount_cents": cents,
            "ts": f"2026-10-{day:02d}T10:00:00", "category": cat,
            "project": project, "channel": "manual", "note": note,
            "extra": {"invoice_no": invoice} if invoice else {}}


def _load(data):
    return load_workbook(io.BytesIO(data))


def test_rows_are_numbers_in_yuan_not_strings():
    evs = [_ev("e1", "income", 520000, 19, "接单", "尾款"),
           _ev("e2", "expense", 8600, 19, note="打车")]
    data, _ = export_xlsx.build_workbook(Path("/tmp"), evs)
    ws = _load(data)["账目明细"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert [r[5] for r in rows] == [5200, 86]          # 元，且是数字不是 "¥5,200.00"
    assert all(isinstance(r[5], (int, float)) for r in rows)
    assert rows[0][2] == "收入" and rows[1][2] == "支出"


def test_same_invoice_is_marked_but_not_counted():
    evs = [_ev("e1", "expense", 8600, 19, invoice="A123"),
           _ev("e2", "expense", 8600, 20, invoice="A123")]
    data, _ = export_xlsx.build_workbook(Path("/tmp"), evs)
    wb = _load(data)
    marks = [r[9] for r in wb["账目明细"].iter_rows(min_row=2, values_only=True)]
    assert marks == ["计入", "重复·未计入"]
    summary = list(wb["汇总"].iter_rows(min_row=2, values_only=True))
    assert summary[0][2] == 86.0                        # 支出只算一次
    assert "重复" in (summary[0][5] or "")


def test_project_filter_only_includes_that_project(tmp_path):
    row = projects.create(tmp_path, "示例项目A")
    pid = row["id"]
    evs = [_ev("e1", "expense", 10000, 19, project=pid),
           _ev("e2", "expense", 20000, 19, project="")]      # 未归项目
    data, name = export_xlsx.build_workbook(tmp_path, evs, project="示例项目A")
    rows = list(_load(data)["账目明细"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 1 and rows[0][1] == "示例项目A" and rows[0][5] == 100
    assert "示例项目A" in name


def test_empty_ledger_still_produces_a_file(tmp_path):
    data, _ = export_xlsx.build_workbook(tmp_path, [])
    wb = _load(data)
    assert wb.sheetnames == ["账目明细", "汇总"]
    assert wb["账目明细"].max_row == 1                  # 只有表头
    last = [r for r in wb["汇总"].iter_rows(values_only=True) if r[0] == "合计"][0]
    assert last[1] == 0 and last[2] == 0


def test_filename_is_sanitised(tmp_path):
    """项目名里有 / 或 : 时文件名不能带路径分隔符（否则下载器会当成目录）。"""
    projects.create(tmp_path, "a/b:c")
    data, name = export_xlsx.build_workbook(tmp_path, [], project="a/b:c")
    assert "/" not in name and ":" not in name
    assert name.endswith(".xlsx")


def test_endpoint_serves_an_xlsx(tmp_path, monkeypatch):
    """端点：202 + 正确的 content-type + 带 UTF-8 文件名（中文项目名不能乱码）。"""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    r = TestClient(app).get("/api/export/xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    disp = r.headers["content-disposition"]
    assert "attachment" in disp and "filename*=UTF-8''" in disp
    assert _load(r.content).sheetnames == ["账目明细", "汇总"]


def test_ui_has_two_entry_points():
    """两个入口都是产品决定，不许悄悄掉：总量在总账目区，单项在项目菜单。"""
    page = (Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "导出 Excel" in page                      # 总账目区块右上角
    assert "async function exportXlsx(" in page
    assert "exportXlsx(p.id)" in page                # 项目 ⋯ 菜单里的单项导出
    # 走 fetch + blob（后端出错能提示），不是直接跳转下载
    assert "URL.createObjectURL(blob)" in page


def test_session_expiry_is_not_reported_as_backend_down():
    """401 要退回邀请码页，不能报成「后端没在运行」（2026-10-20 真机）。

    现场：换码后旧会话立即失效，点「重置」看到「需要邀请码才能使用」——
    看着像"重置要邀请码"。根因是重置/工时表/导入三条路用的是裸 fetch，
    绕过了 j() 的 401 处理，还把后端 detail 原样丢给用户。
    """
    page = (Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    # 三条裸 fetch 必须走同一个带会话的包装
    for call in ('authedFetch("/api/reset"', 'authedFetch("/api/timesheet/summary"',
                 'authedFetch("/api/intake/stream"'):
        assert call in page, f"{call} 还在用裸 fetch"
    # 401 的处理：退到邀请码页，而不是照搬 detail
    assert "async function authedFetch(" in page
    assert "if (r.status === 401)" in page
    assert "function onSessionExpired()" in page
    assert "setSession({ multi_tenant: true, label: \"\" })" in page
    # 重置那条要区分「会话过期」与「后端没在跑」
    assert "why === SESSION_EXPIRED_TEXT" in page
