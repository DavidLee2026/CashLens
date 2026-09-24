"""工时 / 工分表导入与工资参考表汇总单测

用纯标准库现场合成 xlsx（inlineStr 写法），不依赖 openpyxl，
也不提交真实工时表（含员工姓名属个人信息）。
"""
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import timesheet  # noqa: E402


def _col_ref(col: int) -> str:
    """0 起的列号 → Excel 列字母。"""
    s = ""
    col += 1
    while col:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def make_xlsx(path: Path, rows: list[list[str]]) -> Path:
    """合成一个最小 xlsx（inlineStr 写法），供测试使用。"""
    xml_rows = []
    for ri, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{_col_ref(ci)}{ri}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
            for ci, v in enumerate(row) if str(v) != ""
        )
        xml_rows.append(f'<row r="{ri}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def test_reads_inline_str_cells(tmp_path):
    p = make_xlsx(tmp_path / "a.xlsx", [["姓名", "工时"], ["阿明", "40"]])
    rows = timesheet.read_xlsx_rows(p)
    assert rows[0] == ["姓名", "工时"]
    assert rows[1] == ["阿明", "40"]


def test_reads_from_bytes(tmp_path):
    p = make_xlsx(tmp_path / "a.xlsx", [["姓名"], ["阿明"]])
    rows = timesheet.read_xlsx_rows(p.read_bytes())
    assert rows[0] == ["姓名"]


def test_summary_basic(tmp_path):
    """页面示例同款：阿明 40h × ¥30 = ¥1200，小周 36h × ¥25 = ¥900。"""
    p = make_xlsx(tmp_path / "工时.xlsx", [
        ["姓名", "项目", "工时", "单价"],
        ["阿明", "A项目", "40", "30"],
        ["小周", "B项目", "36", "25"],
    ])
    out = timesheet.analyze(p)
    assert out["ok"] is True
    assert out["mapping"] == {"name": 0, "project": 1, "hours": 2, "rate": 3}
    assert out["total_pay_cents"] == 210000
    by = {e["name"]: e for e in out["employees"]}
    assert by["阿明"]["pay_cents"] == 120000
    assert by["小周"]["pay_cents"] == 90000
    assert by["阿明"]["hours"] == 40.0
    assert out["needs_confirm"] == []


def test_header_not_in_first_row(tmp_path):
    """项目表常带标题行，表头不在第一行也要能找到。"""
    p = make_xlsx(tmp_path / "工时.xlsx", [
        ["2026 年 8 月项目工时统计表"],
        [],
        ["姓名", "项目", "工时", "单价"],
        ["阿明", "A项目", "40", "30"],
    ])
    out = timesheet.analyze(p)
    assert out["header_row"] == 2
    assert out["mapping"]["hours"] == 2
    assert out["total_pay_cents"] == 120000


def test_columns_in_any_order(tmp_path):
    """列序乱、列名各异，按关键词识别。"""
    p = make_xlsx(tmp_path / "工时.xlsx", [
        ["单价", "员工", "出勤小时", "工程"],
        ["28", "阿明", "10", "C项目"],
    ])
    out = timesheet.analyze(p)
    assert out["mapping"] == {"rate": 0, "name": 1, "hours": 2, "project": 3}
    assert out["total_pay_cents"] == 28000


def test_aggregates_multiple_rows_per_person(tmp_path):
    p = make_xlsx(tmp_path / "工时.xlsx", [
        ["姓名", "工时", "单价"],
        ["阿明", "8", "30"],
        ["阿明", "8", "30"],
        ["阿明", "8", "30"],
    ])
    out = timesheet.analyze(p)
    assert len(out["employees"]) == 1
    assert out["employees"][0]["hours"] == 24.0
    assert out["employees"][0]["pay_cents"] == 72000
    assert out["employees"][0]["row_count"] == 3


def test_manual_overrides_win(tmp_path):
    """列名认不出时，用户确认映射后必须按用户指定的列走。"""
    p = make_xlsx(tmp_path / "工时.xlsx", [
        ["甲", "乙", "丙"],
        ["阿明", "40", "30"],
    ])
    auto = timesheet.analyze(p)
    assert "name" in auto["needs_confirm"]

    fixed = timesheet.analyze(p, overrides={"name": 0, "hours": 1, "rate": 2})
    assert fixed["mapping_confirmed"] is True
    assert fixed["needs_confirm"] == []
    assert fixed["total_pay_cents"] == 120000


def test_rate_conflict_flagged(tmp_path):
    p = make_xlsx(tmp_path / "工时.xlsx", [
        ["姓名", "工时", "单价"],
        ["阿明", "10", "30"],
        ["阿明", "10", "35"],
    ])
    out = timesheet.analyze(p)
    assert out["employees"][0]["rate_conflict"] is True
    assert any("不同单价" in s for s in out["issues"])
    # 参考值按第一个单价计，不猜
    assert out["employees"][0]["pay_cents"] == 60000


def test_points_unit_detected(tmp_path):
    """工分口径（点数）要如实标注，便于页面区分工时与工分。"""
    p = make_xlsx(tmp_path / "工分.xlsx", [
        ["姓名", "工分", "单价"],
        ["阿明", "120", "5"],
    ])
    out = timesheet.analyze(p)
    assert out["unit"] == "points"
    assert out["total_pay_cents"] == 60000


def test_bad_file_returns_error(tmp_path):
    p = tmp_path / "坏文件.xlsx"
    p.write_bytes(b"not a zip at all")
    try:
        out = timesheet.analyze(p)
    except zipfile.BadZipFile:
        return  # 交给上层端点转 400，也接受直接抛出
    assert out["ok"] is False


def test_empty_sheet_returns_error(tmp_path):
    p = make_xlsx(tmp_path / "空.xlsx", [])
    out = timesheet.analyze(p)
    assert out["ok"] is False
