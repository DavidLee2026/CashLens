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


# ─── 聊天/文字截图：图里文字中的金额 → 候选草稿（2026-09-28）──────────
# 起因：用户把微信群里的报账截图拖进来（「打车 186.79+闪送 110=296.79」），
# 模型完全读得出来，但票据提示词只会答"一张票一个金额" → 金额 0 → 三张图全部「未生成草稿」。
# 现在引擎层把文字逐行抄回来（text_lines），这里只做**形式化提取**、每条都进待确认让人挑。

def test_cache_keeps_text_lines_and_fingerprint_carries_prompt_version():
    """缓存必须带上 `text_lines`，且指纹里要有提示词版本 —— 否则新功能会静默失灵。

    这条是 2026-09-28 加"聊天截图读金额"时差点踩到的坑：缓存只留 4 个旧字段，
    命中缓存的那次没有原文可用；而旧缓存条目又会一直命中。指纹加版本号 = 旧条目自动失效。
    """
    assert "text_lines" in intake._CACHE_FIELDS
    fp = intake._model_fingerprint()
    assert intake._RECOG_PROMPT_VERSION in fp, "提示词版本要进指纹，改结构才能让旧缓存失效"


def test_text_amount_candidates_reads_amounts_from_chat_lines():
    """只挑金额，不挑噪音：日期/时间/人数/文件大小不是钱。"""
    got = intake.text_amount_candidates([
        "打车186.79+闪送110=296.79",
        "午饭晚饭水一共549.6",
        "296.79+549.6=846.39",
        "客户有五份晚饭",
        "11:56 5G 71",
        "0918-19豫园中秋晚会(13)",
        "滴滴电子发票 A(12).pdf 83KB",
        "打车 120 元",
        "全程10.73公里，33分钟",      # 里程与时长不是钱（实弹里被抓成过两笔）
        "全程12.3公里，25分钟",
    ])
    cents = [c["amount_cents"] for c in got]
    assert 18679 in cents and 11000 in cents and 54960 in cents and 84639 in cents
    assert 12000 in cents, "「120 元」这种整数带元的也要认（人写的金额常常不带小数）"
    for noise in (7100, 1300, 8300, 1200, 1073, 1230, 3300, 2500):
        assert noise not in cents, f"{noise/100} 不是金额，是从日期/时间/人数/文件大小里误抓的"
    # 等号右边的是合计：只**标注**，不删（删了就等于替用户判断）
    totals = {c["amount_cents"] for c in got if c["is_total"]}
    assert 29679 in totals and 84639 in totals
    assert 18679 not in totals and 54960 not in totals


def test_text_amount_candidates_merges_ocr_split_duplicates():
    """OCR 把一行拆成两行（「¥100.00」与「的¥100.00」）→ 合成一条；
    但两笔真实的同额消费（两次 28 元）**必须都留着** —— 那才是用户要挑的东西。"""
    one = intake.text_amount_candidates(["¥100.00", "的¥100.00"])
    assert [c["amount_cents"] for c in one] == [10000]
    assert one[0]["line"] == "的¥100.00", "留原文更完整的那条"
    two = intake.text_amount_candidates(["打车 28 元", "闪送 28 元"])
    assert [c["amount_cents"] for c in two] == [2800, 2800], "互不包含的同额两笔都要留"


def test_identical_lines_are_kept_and_flagged_not_merged():
    """**完全相同的两行要都留**（可能是两份，也可能是一行被 OCR 抄两遍），只打标记。

    实弹来源（2026-09-28）：一张饿了么订单卡里「辣椒炒肉盖码饭+例… ¥23.55」出现两行。
    我原来的"完全相同就合并"会把第二份吞掉 —— 用户看不到、也没法补。
    而**不同但互相包含**的行（「¥100.00」/「的¥100.00」＝同一笔转账在两个卡片里）仍要合并。
    """
    rows = intake.text_amount_candidates(["辣椒炒肉盖码饭+例... ¥23.55",
                                          "辣椒炒肉盖码饭+例... ¥23.55"])
    assert [c["amount_cents"] for c in rows] == [2355, 2355], "完全相同的两行不能合并"
    assert all(c["repeat"] for c in rows), "要标记「与另一行完全相同」，让用户对着原图判"
    merged = intake.text_amount_candidates(["¥100.00", "的¥100.00"])
    assert [c["amount_cents"] for c in merged] == [10000], "不同但包含的行仍按同一笔合并"
    assert merged[0]["repeat"] is False


def test_transfer_line_wins_and_order_items_are_not_called_transfers(tmp_path, monkeypatch):
    """有转账记录就只列转账；**转账卡片紧挨着的订单金额不许被算成转账**。

    实弹来源（2026-09-28，`…115033_73_19.jpg`）：这张图里客户只付出去一笔 100 元
    （微信转账卡片：向漆彩 王琦琪转账 / ¥100.00 / 已被接收），下面是没付款的饿了么订单卡
    23.55×2 与一张 -92.00 的行程单。原文里转账卡片的尾行就是孤零零一个「转账」，
    **它紧挨着的正是订单卡的第一行金额** —— 我第一版取 ±1 行做上下文，于是那条 23.55
    被沾成了转账，图里读出 2 笔而不是 1 笔。定稿：只看**本行 + 下一行**。
    """
    text_lines = [
        "商品 美团/大众点评预订单", "-92.00", "滴滴行程单",
        "向漆彩 王琦琪转账", "¥100.00", "已被接收", "转账",
        "辣椒炒肉盖码饭+例... ¥23.55", "辣椒炒肉盖码饭+例... ¥23.55", "餐具数量",
        "漆彩 王琦琪", "的¥100.00", "已收款",
    ]
    rows = intake.text_amount_candidates(text_lines)
    transfers = [c for c in rows if c["kind"] == "transfer"]
    assert [c["amount_cents"] for c in transfers] == [10000], \
        f"只有那笔 ¥100 转账算转账（本行/下一行判定），实得 {transfers}"
    assert all(c["kind"] != "transfer" for c in rows if c["amount_cents"] == 2355), \
        "订单卡里的 23.55 不是转账，不许被上一行的「转账」二字沾上"

    # 走真路径：整张图 → 只落 1 条草稿（100 元），订单金额与行程单都不进待确认
    monkeypatch.setattr(intake, "recognize_file", lambda p: {
        "type": "expense", "amount": 0, "category": "其他",
        "note": "不是票据，是聊天/文字截图", "text_lines": text_lines,
    })
    intake.ingest(tmp_path, [
        {"name": "…115033_73_19.jpg", "rel_path": "919昆明项目发票/…115033_73_19.jpg",
         "content": b"x"},
    ])
    drafts = pending.list_all(tmp_path)
    assert [d["amount_cents"] for d in drafts] == [10000], \
        "图里只有一笔钱真的付出去了：其余金额是订单/行程单，不许进待确认"


def test_arithmetic_totals_are_not_listed_and_each_amount_gets_its_own_label():
    """图里自己算的账不列；每条金额带**自己的条目名**（一行两笔钱＝两个类别）。

    实弹（2026-09-28，80_19 简乐吴浩军那张）：用户给的结构是
    「打车（交通）186.79 / 闪送（快递→经营）110 / 午饭晚饭水（餐饮）549.6」。
    """
    rows = intake.text_amount_candidates([
        "打车186.79+闪送110=296.79",
        "午饭晚饭水一共549.6",
        "296.79+549.6=846.39",
    ])
    keep = [(c["amount_cents"], c["label"], c["derived"]) for c in rows if not c["derived"]]
    assert keep == [(18679, "打车", False), (11000, "闪送", False), (54960, "午饭晚饭水一共", False)], keep
    assert all(c["derived"] for c in rows if c["amount_cents"] in (29679, 84639)), \
        "等号右边是左边加出来的合计，不列（左边那几条已经代表了这笔钱）"
    # 549.6 在合计行里被再引用一次 —— 同一笔钱，不重复列
    assert sum(1 for c in rows if c["amount_cents"] == 54960 and not c["derived"]) == 1
    # 光杆合计（左边没有加数）仍要留：否则会把图里唯一的金额扔掉
    only = intake.text_amount_candidates(["合计=846.39"])
    assert [(c["amount_cents"], c["derived"], c["is_total"]) for c in only] == [(84639, False, True)]


def test_text_amount_candidates_ignores_lines_without_amounts():
    assert intake.text_amount_candidates(["客户有五份晚饭", "好的好的，闪送是什么", ""]) == []
    assert intake.text_amount_candidates(None) == []


def test_chat_screenshot_creates_candidate_drafts(tmp_path, monkeypatch):
    """聊天截图 → 每条金额一条「需核对」草稿，原文进 note，不自动入账。

    这里的原文就是 80_19 那张真图（「打车186.79+闪送110=296.79 / 午饭晚饭水一共549.6」）。
    用户 2026-09-28 定了结构：**该报的是 186.79 + 110 + 549.6 三条**，图里自己加的
    296.79 与 846.39 不列（那笔钱已由明细代表），算式里被再引用的 549.6 也不重复列。
    """
    monkeypatch.setattr(intake, "recognize_file", lambda p: {
        "type": "expense", "amount": 0, "category": "其他", "note": "不是票据，是聊天/文字截图",
        "text_lines": ["打车186.79+闪送110=296.79", "午饭晚饭水一共549.6",
                       "客户有五份晚饭", "296.79+549.6=846.39"],
    })
    out = intake.ingest(tmp_path, [
        {"name": "微信图片_1.jpg", "rel_path": "919昆明项目发票/微信图片_1.jpg", "content": b"x"},
    ])
    drafts = pending.list_all(tmp_path)
    amounts = sorted(d["amount_cents"] for d in drafts)
    assert amounts == [11000, 18679, 54960], f"该报的只有三条明细：{amounts}"
    # 每条的 note 都要留住原文，且带「请人工核对」→ 面板上的「需核对」徽章靠它亮
    assert all("原文：" in d["note"] for d in drafts)
    assert all("请人工核对" in d["note"] for d in drafts)
    # 分类按**条目名**判，不按整行（一行里两笔钱可以是两个类别）
    cats = {d["amount_cents"]: d["category"] for d in drafts}
    assert cats[18679] == "交通", "「打车」→ 交通"
    assert cats[11000] == "经营", "「闪送」寄的是物料 → 经营（这一类含快递/寄件）"
    assert cats[54960] == "餐饮", "「午饭晚饭水」→ 餐饮"
    labels = {d["amount_cents"]: d["note"] for d in drafts}
    assert "打车" in labels[18679] and "闪送" in labels[11000], "条目名要写进 note，用户才看得懂"
    b = out["batches"][0]
    assert b["draft_count"] == 3
    assert b["files"][0]["text_amounts"] == 7, "读到的金额仍是 7 个（含图里自己算的合计）"
    assert b["files"][0]["total_amounts"] == 4, "如实报出有几个是图里自己算的合计"
    # 汇总要靠这个数才知道"这批是候选，别全确认"（之前写错位置，图片走不到，一直显示 0）
    assert b["text_amount_drafts"] == 3


def test_payment_voucher_makes_exactly_one_draft(tmp_path, monkeypatch):
    """付款凭证（账单详情 / 支付成功 / 行程已结束）→ **正好一条**草稿，金额是实付。

    实弹来源（2026-09-28，919 昆明项目）：
      支付宝账单详情「-40.50 交易成功 / 收款方全称 云南强林乐家… / 商品说明 …711…REDEMPTION」→ 40.50 购物
      打车支付成功「46.17 元 / 优惠1.51元 / 全程 25.37 公里」→ 46.17 交通（1.51、25.37 都不是钱）
      行程已结束「¥40.60 费用明细 / 全程27.63公里 41分钟」→ 40.60 交通
    """
    monkeypatch.setattr(intake, "recognize_file", lambda p: {
        "type": "expense", "amount": 40.50, "date": "2026-09-18",
        "merchant": "云南强林乐家连锁便利店有限公司",
        "category": "购物",
        "items": [{"name": "云南711支付宝东风广场金格店REDEMPTION", "amount": 40.50}],
        "note": "付款凭证", "text_lines": [], "confidence": 0.9,
    })
    out = intake.ingest(tmp_path, [
        {"name": "微信图片_20260924123103.jpg", "rel_path": "919昆明项目/x.jpg", "content": b"x"},
    ])
    drafts = pending.list_all(tmp_path)
    assert [d["amount_cents"] for d in drafts] == [4050], "付款凭证只能是一条，金额是实付 40.50"
    assert drafts[0]["category"] == "购物", "711 便利店的商品说明要能判成购物"
    b = out["batches"][0]
    assert b["files"][0].get("text_amounts") in (None, 0), "付款凭证不走「图里文字候选」那条路"
    assert b["text_amount_drafts"] == 0


def test_payment_with_refund_books_the_net_amount(tmp_path, monkeypatch):
    """账单「-78.00」+「已退款 ¥6.00」→ 入账 **-72.00**（用户 2026-09-28 口径）。

    引擎层只读两个数（实付 / 退款），**减法在后端做** —— 不让模型自己算净额。
    「爱萝卜打印」这四个字必须进 items，否则一笔打印费会落进「待确认」
    （页面上它写在「商家小程序」那一行，不在商品说明里）。
    """
    monkeypatch.setattr(intake, "recognize_file", lambda p: {
        "type": "expense", "doc_kind": "payment", "amount": 78.00, "refund_amount": 6.00,
        "date": "2026-09-18", "merchant": "商户_杨甜子", "category": "其他",
        "items": [{"name": "爱萝卜订单", "amount": 78.0}, {"name": "爱萝卜打印", "amount": 0}],
        "note": "付款凭证", "text_lines": [], "confidence": 0.9,
    })
    out = intake.ingest(tmp_path, [
        {"name": "微信图片_20260924123051.jpg", "rel_path": "919昆明项目/x.jpg", "content": b"x"},
    ])
    drafts = pending.list_all(tmp_path)
    assert [d["amount_cents"] for d in drafts] == [7200], "净额 = 78.00 - 6.00"
    assert drafts[0]["category"] == "经营", "「爱萝卜打印」→ 经营（打印）"
    assert "78.00" in drafts[0]["note"] and "6.00" in drafts[0]["note"], (
        "实付与退款都要留在备注里，用户才能对着账单核"
    )
    assert out["batches"][0]["files"][0]["refund_cents"] == 600


def test_unpaid_list_and_full_refund_make_no_draft_but_say_which(tmp_path, monkeypatch):
    """待支付 / 订单列表 / 全额退款 —— **都不建草稿**，但必须说清是哪一种。

    实弹（2026-09-28，919 昆明项目）：
      · 「待支付打车订单，未实际完成付款」的图，当时还从文字里挑了一条 57.70 进待确认；
      · 「我的订单」列表里的 ¥78.0 与另一张账单详情里的 ¥78.00 是**同一笔**（爱萝卜这个平台），
        两张都记就是记两遍。
    不让它们建草稿**不等于**不说：`no_draft_reason` 是给界面的那句话的依据。
    """
    cases = [
        ("unpaid", {"doc_kind": "unpaid", "amount": 0, "note": "待支付打车订单，未实际完成付款"},
         "unpaid"),
        ("list", {"doc_kind": "list", "amount": 0, "note": "不是票据，是订单列表截图"}, "list"),
        ("fully_refunded", {"doc_kind": "payment", "amount": 78.0, "refund_amount": 78.0,
                            "note": "当前状态 已退款"}, "fully_refunded"),
    ]
    for name, payload, want in cases:
        d = tmp_path / name
        d.mkdir()
        monkeypatch.setattr(intake, "recognize_file", lambda p, _p=payload: {
            "type": "expense", "date": "", "merchant": "", "category": "其他",
            "items": [], "text_lines": [], "confidence": 0.5, **_p,
        })
        out = intake.ingest(d, [{"name": f"{name}.jpg", "rel_path": f"p/{name}.jpg",
                                 "content": b"x"}])
        assert pending.list_all(d) == [], f"{name}：不该建草稿"
        assert out["batches"][0]["files"][0]["no_draft_reason"] == want, (
            f"{name}：要如实标出是哪种，界面才说得清为什么没记"
        )
        assert out["batches"][0]["draft_count"] == 0


def test_real_invoice_never_goes_through_the_candidate_path(tmp_path, monkeypatch):
    """有票面金额的图照旧走原路：不许因为图里同时有别的数字就多建候选草稿。"""
    monkeypatch.setattr(intake, "recognize_file", lambda p: {
        "type": "expense", "amount": 962.00, "date": "2026-09-12", "merchant": "某公司",
        "category": "住宿", "invoice_no": "26537000000118274568", "confidence": 0.95,
        "text_lines": ["住宿费 962 元", "打车 186.79"],
    })
    intake.ingest(tmp_path, [{"name": "票.jpg", "rel_path": "x/票.jpg", "content": b"x"}])
    drafts = pending.list_all(tmp_path)
    assert [d["amount_cents"] for d in drafts] == [96200], "有票面金额时不该再建候选草稿"


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
