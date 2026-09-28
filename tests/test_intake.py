"""统一采集入口单测：分流 / 文件夹建项目 / 草稿生成 / 双源对账。

识别通道一律 mock 掉（不打模型、不产生费用）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services import intake, pending, projects  # noqa: E402
from _xlsx_helper import make_xlsx  # noqa: E402


def _sheet_bytes():
    return make_xlsx(
        header=["编号", "日期", "项目", "费用", "截图", "备注", "总计（元）"],
        rows=[["前端", 46283, "打车", 47.13, "", "打车去上海机场", 384.35],
              ["", 46284, "吃饭", 32.26, "", "早餐加午餐", ""]],
        embedded={"image1.jpeg": b"\xff\xd8fake1", "image2.jpeg": b"\xff\xd8fake2"},
        dispimg_col=4,
        dispimg_ids=["ID_AAA", "ID_BBB"],
    )


# ─── 分流与分组 ─────────────────────────────────────────

def test_classify_by_extension():
    assert intake.classify("a.JPG") == "image"
    assert intake.classify("a.png") == "image"
    assert intake.classify("票.pdf") == "pdf"
    assert intake.classify("报销.xlsx") == "sheet"
    assert intake.classify("账单.csv") == "csv"
    assert intake.classify("老表.xls") == "unsupported"
    assert intake.classify("说明.txt") == "unknown"


def test_merge_folder_groups_by_first_level_dir():
    files = [
        {"name": "a.jpg", "rel_path": "919昆明项目发票/a.jpg"},
        {"name": "b.xlsx", "rel_path": "919昆明项目发票/b.xlsx"},
        {"name": "c.jpg", "rel_path": "924豫园灯会/c.jpg"},
        {"name": "loose.jpg", "rel_path": "loose.jpg"},
    ]
    groups = {g["folder"]: len(g["files"]) for g in intake.merge_folder(files)}
    assert groups["919昆明项目发票"] == 2
    assert groups["924豫园灯会"] == 1
    assert groups[None] == 1          # 不在文件夹里的，不建项目


# ─── 文件夹名建项目 ─────────────────────────────────────

def test_ingest_creates_project_from_folder_name(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "recognize_file", lambda p: {"amount": 0})
    body = _sheet_bytes()
    out = intake.ingest(tmp_path, [
        {"name": "云南报销.xlsx", "rel_path": "919昆明项目发票/云南报销.xlsx", "content": body},
    ])
    assert out["batches"][0]["project_name"] == "919昆明项目发票"
    # 项目真的建出来了
    assert projects.resolve(tmp_path, "919昆明项目发票") is not None
    # 导入后要提示改名（David 定的交互）
    assert "改" in out["batches"][0]["project_hint"]


def test_ingest_uses_explicit_project_over_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "recognize_file", lambda p: {"amount": 0})
    projects.create(tmp_path, "指定项目")
    out = intake.ingest(
        tmp_path,
        [{"name": "a.xlsx", "rel_path": "某文件夹/a.xlsx", "content": _sheet_bytes()}],
        project_ref="指定项目",
    )
    assert out["batches"][0]["project_name"] == "指定项目"


# ─── Excel：表内数字入账 + 嵌入图核对 ───────────────────

def test_sheet_ingest_drafts_use_table_numbers_not_recognition(tmp_path, monkeypatch):
    """表里的数字优先；识别结果只用于核对，不能覆盖人写的数。"""
    # 让识别故意返回一个错的金额，验证入账金额仍以表内为准
    monkeypatch.setattr(intake, "recognize_file", lambda p: {"amount": 999.99})
    out = intake.ingest(tmp_path, [
        {"name": "云南报销.xlsx", "rel_path": "919昆明项目发票/云南报销.xlsx", "content": _sheet_bytes()},
    ])
    drafts = pending.list_all(tmp_path)
    assert len(drafts) == 2
    assert sum(d["amount_cents"] for d in drafts) == 4713 + 3226   # 表内数字
    assert all(d["category"] in ("交通", "餐饮") for d in drafts)
    assert all(d["source"] == "报销表" for d in drafts)
    assert all(d["date"] for d in drafts)
    # 识别的错金额被记进核对结果，等人工看
    b = out["batches"][0]
    assert b["reconcile"]["checked"] == 2
    assert b["reconcile"]["matched"] == 0
    assert len(b["reconcile"]["mismatched"]) == 2


def test_sheet_ingest_reports_reconcile_match(tmp_path, monkeypatch):
    """识别金额与表内一致时，要标成对上。"""
    vals = {"image1.jpeg": 47.13, "image2.jpeg": 32.26}
    monkeypatch.setattr(intake, "recognize_file",
                        lambda p: {"amount": vals.get(Path(p).name, 0), "merchant": "测试"})
    out = intake.ingest(tmp_path, [
        {"name": "云南报销.xlsx", "rel_path": "919昆明项目发票/云南报销.xlsx", "content": _sheet_bytes()},
    ])
    b = out["batches"][0]
    assert b["reconcile"]["matched"] == 2
    assert b["reconcile"]["mismatched"] == []
    assert b["declared_total_cents"] == 38435
    assert b["reconcile_diff_cents"] == 38435 - (4713 + 3226)


def test_sheet_reconcile_off_skips_recognition_and_says_so(tmp_path, monkeypatch):
    """关掉「核对表内发票」= **一次识别都不跑**，且结果里要如实说"本次未核对"。

    为什么要专门测：关掉之后 `reconcile.checked` 是 0，跟前端「双源核对 0/0 张一致」
    长得很像 —— 那是假汇报（听起来像"核过了、都没问题"）。跳过必须在数据层就标出来，
    前端才有东西可依。
    """
    called = []
    monkeypatch.setattr(intake, "recognize_file",
                        lambda p: called.append(p) or {"amount": 47.13})
    out = intake.ingest(tmp_path, [
        {"name": "云南报销.xlsx", "rel_path": "919昆明项目发票/云南报销.xlsx", "content": _sheet_bytes()},
    ], reconcile=False)
    b = out["batches"][0]
    # 表内数字照旧入账（关掉的只是核对，不是导入）
    assert len(pending.list_all(tmp_path)) == 2
    assert b["reconcile_skipped"] is True
    assert b["reconcile"]["checked"] == 0
    assert called == [], "关掉核对时不该再去识别任何嵌入图（这正是慢的那一步）"


def test_sheet_reconcile_stage_text_distinguishes_skipped_from_no_image():
    """「跳过核对」和「表里没有嵌入图」是两回事，界面上不能写成同一句。"""
    from app.main import _file_stages
    base = {"kind": "sheet", "row_count": 10, "embedded_image_count": 2, "declared_total_cents": 1000}
    skipped = _file_stages("表.xlsx", [dict(base, reconcile_skipped=True,
                                             reconcile={"checked": 0, "matched": 0,
                                                        "mismatched": [], "unreadable": []})], [], 2, 947)
    assert "本次未核对" in skipped["recognized"] and "2" in skipped["recognized"]
    assert "没有嵌入图" not in skipped["recognized"], "跳过 ≠ 表里没有图，别混成一句"
    assert "未" in skipped["how"]
    # 核对过的那条路照旧报匹配数
    done = _file_stages("表.xlsx", [dict(base, reconcile_skipped=False,
                                        reconcile={"checked": 2, "matched": 2,
                                                   "mismatched": [], "unreadable": []})], [], 2, 947)
    assert "2/2" in done["recognized"]


# ─── 图片：识别 → 草稿 ──────────────────────────────────

def test_image_ingest_creates_drafts(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "recognize_file", lambda p: {
        "amount": 47.13, "date": "2026-09-18", "merchant": "李师傅",
        "category": "交通", "invoice_no": "26537000000118274568", "confidence": 0.95,
        "_cloud_uploaded": False,
    })
    out = intake.ingest(tmp_path, [
        {"name": "打车.jpg", "rel_path": "出差/打车.jpg", "content": b"\xff\xd8x"},
    ])
    drafts = pending.list_all(tmp_path)
    assert len(drafts) == 1
    assert drafts[0]["amount_cents"] == 4713
    assert drafts[0]["category"] == "交通"
    assert drafts[0]["date"] == "2026-09-18"
    assert out["batches"][0]["project_name"] == "出差"


def test_zero_amount_recognition_makes_no_draft(tmp_path, monkeypatch):
    """认不出金额的不建草稿（没金额就不是一笔账），但要如实记在结果里。"""
    monkeypatch.setattr(intake, "recognize_file",
                        lambda p: {"amount": 0, "note": "无法识别"})
    out = intake.ingest(tmp_path, [
        {"name": "糊图.jpg", "rel_path": "出差/糊图.jpg", "content": b"\xff\xd8x"},
    ])
    assert pending.list_all(tmp_path) == []
    assert out["batches"][0]["files"][0]["ok"] is True
    assert out["batches"][0]["files"][0]["amount_cents"] == 0


def test_recognition_error_does_not_break_the_batch(tmp_path, monkeypatch):
    """单张识别失败不能让整批导入崩掉。"""
    def fake(p):
        if "坏" in Path(p).name:
            return {"error": "API HTTP 500"}
        return {"amount": 10.0, "merchant": "好店"}
    monkeypatch.setattr(intake, "recognize_file", fake)
    out = intake.ingest(tmp_path, [
        {"name": "坏图.jpg", "rel_path": "出差/坏图.jpg", "content": b"\xff\xd8x"},
        {"name": "好图.jpg", "rel_path": "出差/好图.jpg", "content": b"\xff\xd8y"},
    ])
    assert out["batches"][0]["errors"]
    assert len(pending.list_all(tmp_path)) == 1     # 好的那张照常建草稿


# ─── 不支持的格式与去重 ─────────────────────────────────

def test_unsupported_file_reported_not_silently_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "recognize_file", lambda p: {"amount": 0})
    out = intake.ingest(tmp_path, [
        {"name": "老表.xls", "rel_path": "出差/老表.xls", "content": b"x"},
    ])
    assert out["batches"][0]["errors"][0]["error"].startswith("暂不支持")


def test_two_folders_make_two_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "recognize_file", lambda p: {"amount": 0})
    out = intake.ingest(tmp_path, [
        {"name": "a.xlsx", "rel_path": "项目甲/a.xlsx", "content": _sheet_bytes()},
        {"name": "b.xlsx", "rel_path": "项目乙/b.xlsx", "content": _sheet_bytes()},
    ])
    names = sorted(b["project_name"] for b in out["batches"])
    assert names == ["项目乙", "项目甲"]
    assert len(pending.list_all(tmp_path)) == 4


def test_reconcile_diff_uses_sheet_total_only(tmp_path, monkeypatch):
    """一批里同时有图片和 Excel 时，差额只能拿 Excel 那部分算。

    实弹踩过：拿整批合计去减表内总计，得到一个毫无意义的差额（¥-1398.10）。
    """
    monkeypatch.setattr(intake, "recognize_file",
                        lambda p: {"amount": 50.0, "merchant": "某店"})
    out = intake.ingest(tmp_path, [
        {"name": "云南报销.xlsx", "rel_path": "出差/云南报销.xlsx", "content": _sheet_bytes()},
        {"name": "票.jpg", "rel_path": "出差/票.jpg", "content": b"\xff\xd8x"},
    ])
    b = out["batches"][0]
    assert b["declared_total_cents"] == 38435
    assert b["sheet_draft_total_cents"] == 4713 + 3226
    assert b["reconcile_diff_cents"] == 38435 - (4713 + 3226)
    # 整批合计仍然要包含图片那笔
    assert b["identified_total_cents"] == 4713 + 3226 + 5000


def test_category_ignores_technical_note(tmp_path, monkeypatch):
    """PDF 回退说明里含 "API HTTP 400"，不能因此把酒店发票判成经营成本。"""
    monkeypatch.setattr(intake, "recognize_file", lambda p: {
        "amount": 962.0, "merchant": "", "category": "",
        "note": "本机 PDF 文本层解析（图像未出本机）：商户与分类需人工确认"
                "（模型通道失败，已回退本机文本层：API HTTP 400: invalid image input）",
    })
    intake.ingest(tmp_path, [
        {"name": "酒店.pdf", "rel_path": "出差/酒店.pdf", "content": b"%PDF"},
    ])
    drafts = pending.list_all(tmp_path)
    assert len(drafts) == 1
    assert drafts[0]["category"] == "待确认"      # 关键：不是「经营」


# ─── 商户交叉核对 + 票面名目参与判分类（2026-09-28 修）───
# 合成名称，不含真实票面信息。

def test_merchant_cross_checked_against_the_item_category():
    """文本层里买卖方名字顺序不可靠，所以**不能按顺序取**；用"分类能否对上"交叉核对。

    住宿票的卖方应当是酒店、餐饮票的是餐厅 —— 这是票面项目名目给出的独立证据，
    所以这是核对而不是猜。命中必须**唯一**才采纳。
    """
    cands = ["某某文化传播有限公司", "某某酒店管理有限公司"]
    assert intake._merchant_by_category(cands, "住宿") == "某某酒店管理有限公司"

    # 命中 0 个 → 留空交人工（不许硬挑一个）
    assert intake._merchant_by_category(cands, "交通") == ""
    # 占位符分类不做核对（还没有可信分类可对）
    assert intake._merchant_by_category(cands, "待确认") == ""
    assert intake._merchant_by_category(cands, "") == ""
    # 两个都命中 → 不唯一，同样留空
    dup = ["某某酒店管理有限公司", "某某酒店有限公司"]
    assert intake._merchant_by_category(dup, "住宿") == ""


def test_category_can_come_from_the_invoice_item_name_alone():
    """分类只看 merchant+category 时，文本层 PDF 这两样都是空的 → 必然「待确认」。

    补上票面项目名目之后，「*生产生活服务*住宿费」这种名目就能判出「住宿」。
    这条锁住"名目确实参与判分类"这个事实（否则本机 PDF 通道会永远判不出分类）。
    """
    from app.services import categories
    assert categories.match_category("", "", "住宿费") == "住宿"
    assert categories.match_category("", "", "打车费") == "交通"
    # 名目认不出时仍如实落占位符，不硬猜
    assert categories.match_category("", "", "某个说不清的名目") == categories.UNCONFIRMED_CATEGORY


# ─── 报销表核对：能跳过、要留痕、有缓存（2026-09-28 用户提的"3 分钟太慢"）───

def _fake_sheet(image: Path) -> dict:
    """一张只有一行、行内嵌一张图的假报销表（省掉真 xlsx 的构造）。"""
    return {
        "ok": True, "row_count": 1, "embedded_image_count": 1,
        "category_column_semantics": "", "declared_total_cents": 4713,
        "sum_amount_cents": 4713, "needs_confirm": [], "total_row": None,
        "rows": [{"amount_cents": 4713, "category": "交通", "category_raw": "交通",
                  "note": "打车", "owner": "", "date": "2026-09-18",
                  "image_name": image.name, "image_path": str(image)}],
    }


def test_reconcile_can_be_skipped_and_says_so(tmp_path, monkeypatch):
    """关掉双源核对时**不许调模型**，而且必须如实标注"跳过了"。

    起因（2026-09-28 用户）：一张 10 行的报销表导入跑了 3 分钟 —— 实测读表 0.00 秒，
    时间全在「逐张识别表内嵌入图」。表内数字本来就是权威（以表内数字为准），
    核对只用来发现"表和票对不上"，所以给用户一个能关的开关；关掉要留痕，
    不能让界面看起来像"核对过了、都没问题"。
    """
    img = tmp_path / "a.jpeg"
    img.write_bytes(b"fake-image")
    monkeypatch.setattr(intake.report_sheet, "analyze", lambda body, image_dir=None: _fake_sheet(img))

    def must_not_be_called(*a, **kw):
        raise AssertionError("reconcile=False 时不该调用识别通道")

    monkeypatch.setattr(intake, "recognize_file", must_not_be_called)
    out = intake.ingest(tmp_path, [{"name": "x.xlsx", "rel_path": "x.xlsx", "content": b"x"}],
                        work_root=tmp_path / "w", reconcile=False)
    item = out["batches"][0]["files"][0]
    assert item["reconcile"]["checked"] == 0
    assert item["reconcile_skipped"] is True, "跳过了就要如实标注"
    assert out["batches"][0]["draft_count"] == 1, "表内数字照样要出草稿（核对不是入账必需）"


def test_reconcile_result_is_cached_by_image_and_model(tmp_path, monkeypatch):
    """同一张图 + 同一个模型 → 第二次不再调模型（用户会反复导入同一份表）。

    实测（真实报表 10 张嵌入图）：串行 ~180 秒 → 并发 4 路 **75.5 秒** → 命中缓存 **0.0 秒**。
    """
    img = tmp_path / "a.jpeg"
    img.write_bytes(b"same-bytes")
    monkeypatch.setattr(intake.report_sheet, "analyze", lambda body, image_dir=None: _fake_sheet(img))

    calls = {"n": 0}

    def fake_recognize(path, *a, **kw):
        calls["n"] += 1
        return {"amount": 47.13, "date": "2026-09-18", "merchant": "某出行", "invoice_no": ""}

    monkeypatch.setattr(intake, "recognize_file", fake_recognize)
    files = [{"name": "x.xlsx", "rel_path": "x.xlsx", "content": b"x"}]

    first = intake.ingest(tmp_path, files, work_root=tmp_path / "w1")
    second = intake.ingest(tmp_path, files, work_root=tmp_path / "w2")

    assert calls["n"] == 1, f"第二次应当命中缓存、不再调模型，实际调了 {calls['n']} 次"
    c1 = first["batches"][0]["files"][0]["checks"][0]
    c2 = second["batches"][0]["files"][0]["checks"][0]
    assert c1["match"] is True and c2["match"] is True
    assert not c1.get("from_cache") and c2.get("from_cache") is True, "要如实标注这条来自缓存"
