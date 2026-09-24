"""报销表（xlsx）通道单测：认列 / 金额 / 日期 / 嵌入图 / 项目列语义。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services import report_sheet as rs  # noqa: E402
from _xlsx_helper import make_xlsx  # noqa: E402


def test_suggest_mapping_prefers_longer_keyword():
    """「总计」不能被「计」抢走，「费用」要归金额而不是类别。"""
    header = ["编号", "日期", "项目", "费用", "截图", "备注", "总计（元）"]
    m = rs.suggest_mapping(header)
    assert m["owner"] == 0
    assert m["date"] == 1
    assert m["category"] == 2
    assert m["amount"] == 3
    assert m["image"] == 4
    assert m["note"] == 5
    assert m["total"] == 6


def test_to_date_excel_serial_and_strings():
    assert rs.to_date(46283) == "2026-09-18"
    assert rs.to_date(46284) == "2026-09-19"
    assert rs.to_date("2026-09-20") == "2026-09-20"
    assert rs.to_date("2026年9月20日") == "2026-09-20"
    assert rs.to_date("") == ""
    assert rs.to_date("认不出来") == ""


def test_to_cents_tolerates_currency_marks():
    assert rs.to_cents("47.13") == 4713
    assert rs.to_cents("¥1,234.5") == 123450
    assert rs.to_cents(-0.0) == 0
    assert rs.to_cents("") == 0


def test_dispimg_id_extraction():
    assert rs._dispimg_id('=DISPIMG("ID_ABC123",1)') == "ID_ABC123"
    assert rs._dispimg_id('=DISPIMG(ID_FF00,1)') == "ID_FF00"
    assert rs._dispimg_id("普通文本") == ""


def test_analyze_reads_rows_and_classifies():
    """表头「项目」列装的是费用类别（打车/吃饭）时，要判成 expense_category。"""
    body = make_xlsx(
        header=["编号", "日期", "项目", "费用", "备注"],
        rows=[["前端", 46283, "打车", 47.13, "打车去上海机场"],
              ["", 46284, "吃饭", 32.26, "早餐加午餐"]],
    )
    out = rs.analyze(body)
    assert out["ok"] is True
    assert out["row_count"] == 2
    assert out["category_column_semantics"] == "expense_category"
    assert out["rows"][0]["amount_cents"] == 4713
    assert out["rows"][0]["date"] == "2026-09-18"
    assert out["rows"][0]["category"] == "交通"
    assert out["rows"][1]["category"] == "餐饮"
    assert out["rows"][0]["owner"] == "前端"
    assert out["sum_amount_cents"] == 4713 + 3226


def test_analyze_treats_project_column_as_project_when_values_are_not_categories():
    """表头写着「项目」但值是真项目名（不是费用类别）时，要判成 project。"""
    body = make_xlsx(
        header=["日期", "项目", "金额", "备注"],
        rows=[[46283, "919昆明项目", 100.0, "差旅"],
              [46284, "924豫园灯会", 200.0, "物料"]],
    )
    out = rs.analyze(body)
    assert out["category_column_semantics"] == "project"
    assert out["rows"][0]["project_hint"] == "919昆明项目"


def test_analyze_reads_declared_total():
    body = make_xlsx(
        header=["日期", "费用", "备注", "总计（元）"],
        rows=[[46283, 47.13, "打车", 384.35],
              [46284, 37.54, "打车", ""]],
    )
    out = rs.analyze(body)
    assert out["declared_total_cents"] == 38435
    assert out["sum_amount_cents"] == 4713 + 3754


def test_extract_embedded_images_and_row_mapping(tmp_path):
    """嵌入图要能取出来，并且每行对得上自己那张。"""
    png = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    body = make_xlsx(
        header=["日期", "项目", "费用", "截图"],
        rows=[[46283, "打车", 47.13, ""], [46284, "吃饭", 32.26, ""]],
        embedded={"image1.jpeg": png, "image2.jpeg": png},
        dispimg_col=3,
        dispimg_ids=["ID_AAA", "ID_BBB"],
    )
    out = rs.analyze(body)
    assert out["embedded_image_count"] == 2
    assert out["rows"][0]["image_name"] == "image1.jpeg"
    assert out["rows"][1]["image_name"] == "image2.jpeg"
    assert out["rows_missing_image"] == []

    images = rs.extract_embedded_images(body)
    assert set(images) == {"image1.jpeg", "image2.jpeg"}
    assert images["image1.jpeg"] == png

    # image_dir 给了就要落盘
    rs.analyze(body, image_dir=tmp_path)
    assert (tmp_path / "image1.jpeg").exists()


def test_analyze_rejects_non_xlsx():
    out = rs.analyze(b"not a zip at all")
    assert out["ok"] is False
    assert "error" in out
