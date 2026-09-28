"""票据识别 · 发票号码校验与双源交叉核对单测

回归 2026-09-04 实弹缺陷：真实数电发票的 20 位发票号码被云端 VLM 漏读 1 位
（连续 0 段被压缩一位），而模型自评 confidence 仍为 1.0。

隐私约定（重要）：本文件内所有号码均为**合成值**，只保留真实缺陷的形态特征
（20 位、含连续 0 段、前 2 位年份、第 3 至 4 位行政区划代码）。
真实票据只通过 08-实测/530.pdf 在运行时动态读取，绝不把票面数字写进代码仓，
因为本仓会推送到公开远端，而 .gitignore 只排除 *.pdf 与 data/，管不到 .py 里的字符串。
真实样本缺失时，相关端到端用例自动跳过；纯逻辑用例不依赖样本。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "mcp"))

from receipt_mcp import (  # noqa: E402
    _one_zero_inserted,
    _structurally_plausible,
    _twenty_digit_candidates,
    crosscheck_invoice_no,
    extract_pdf_text_layer,
    parse_from_text_layer,
    recognize_receipt,
    _item_names,
    _merchant_candidates,
    validate_invoice_no,
)

# 合成号码：形态与真实缺陷一致，但数字本身不是任何真实票据
TRUTH = "26312000005416152000"      # 20 位，前 2 位 26（2026 年），第 3 至 4 位 31（上海）
VLM_WRONG = "2631200005416152000"   # 19 位，即 TRUTH 连续 0 段被压缩一位
BANK_LIKE = "31050161413700000099"  # 20 位，第 3 至 4 位 05，不在行政区划白名单（同真实银行账号的排除特征）

REAL_PDF = REPO.parent / "08-实测" / "530.pdf"


# ─── 号码格式校验 ───────────────────────────────────────
def test_validate_accepts_20_digits():
    assert validate_invoice_no(TRUTH)["ok"] is True


def test_validate_rejects_19_digits():
    r = validate_invoice_no(VLM_WRONG)
    assert r["ok"] is False
    assert "19" in r["reason"]


def test_validate_normalizes_spaces():
    spaced = " ".join([TRUTH[:4], TRUTH[4:8], TRUTH[8:12], TRUTH[12:16], TRUTH[16:]])
    assert validate_invoice_no(spaced)["no"] == TRUTH


def test_validate_rejects_empty():
    assert validate_invoice_no("")["ok"] is False


def test_validate_warns_on_unknown_region_prefix():
    # 把第 3 至 4 位换成 99（不在白名单）仍应是 20 位通过、但带软提示
    weird = TRUTH[:2] + "99" + TRUTH[4:]
    r = validate_invoice_no(weird)
    assert r["ok"] is True
    assert r["warnings"]


# ─── 差一位判定 ─────────────────────────────────────────
def test_one_zero_inserted_detects_real_defect():
    assert _one_zero_inserted(VLM_WRONG, TRUTH) is True


def test_one_zero_inserted_rejects_nonzero_diff():
    # 与 TRUTH 差一位，但差的不是被插入的 0
    other = VLM_WRONG[:-1] + "1"
    assert _one_zero_inserted(other, TRUTH) is False


# ─── 双源交叉核对 ───────────────────────────────────────
def test_crosscheck_fixes_missing_zero():
    text = f"发票号码：\n{TRUTH}\n开票日期：2026年08月26日\n"
    r = crosscheck_invoice_no(VLM_WRONG, text)
    assert r["ok"] is True
    assert r["no"] == TRUTH


def test_crosscheck_rejects_ambiguous_candidates():
    """两个分离的 0 段可插出两个不同候选时必须拒绝，不得任选其一。"""
    base19 = "1000200034567890123"
    r = crosscheck_invoice_no(base19, "10000200034567890123 10002000034567890123")
    assert r["ok"] is False


def test_crosscheck_rejects_wrong_length():
    assert crosscheck_invoice_no("12345", TRUTH)["ok"] is False


def test_bank_account_filtered_out():
    """销方银行账号同为 20 位，必须被结构筛选排除（真实样本暴露的坑）。"""
    got = _structurally_plausible([TRUTH, BANK_LIKE], "2026-08-26")
    assert got == [TRUTH]


def test_plausible_rejects_year_mismatch():
    """号码前两位应与开票年份后两位一致。"""
    assert _structurally_plausible([TRUTH], "2025-08-26") == []


# ─── 真实票据端到端（样本缺失自动跳过；号码动态读取，不写进代码）──
@pytest.mark.skipif(not REAL_PDF.exists(), reason="真实票据样本不入公开仓库")
def test_real_pdf_local_only_extracts_20_digits():
    """本机解析真实数电发票：号码必须 20 位且通过校验，且图像不出本机。"""
    text = extract_pdf_text_layer(str(REAL_PDF))
    assert text, "应能抽出 PDF 文本层"

    res = parse_from_text_layer(text, str(REAL_PDF))
    # 不硬编码票面数字：只断言它确实是文本层里的候选之一，且为 20 位数字
    assert res["invoice_no"] in _twenty_digit_candidates(text)
    assert len(res["invoice_no"]) == 20
    assert res["invoice_no"].isdigit()
    assert res["invoice_no_check"]["ok"] is True

    assert res["_cloud_uploaded"] is False
    assert res["amount"] == 530.0
    assert res["date"] == "2026-08-26"
    # 商户与分类在文本流里顺序不可靠，必须留空待人工确认
    assert res["merchant"] == ""
    assert "merchant" in res["needs_human"]


@pytest.mark.skipif(not REAL_PDF.exists(), reason="真实票据样本不入公开仓库")
def test_real_pdf_bank_account_is_filtered():
    """真实发票文本层里同时存在发票号码与销方银行账号（都是 20 位），
    结构筛选必须只留下发票号码。"""
    text = extract_pdf_text_layer(str(REAL_PDF))
    cands = _twenty_digit_candidates(text)
    assert len(cands) >= 2, "真实样本应暴露 20 位数字串不唯一这个坑"
    plausible = _structurally_plausible(cands, "2026-08-26")
    assert len(plausible) == 1


# ─── 文本层要能读出「票面项目名目」与「主体名称候选」（2026-09-28 修）───
# 合成样本，不含任何真实票面数字与商户名。

_SYNTH = """电子发票（增值税专用发票）
发票号码：
开票日期：
名称：
某某文化传播有限公司 某某酒店管理有限公司
某某地址:某市某区某路1号;    电话:0000-0000000;
销方开户银行:某某银行某某分行营业部;    银行账号:00000000000000000000;
合 计
价税合计（大写） （小写）
玖佰陆拾贰圆整 ¥ 962.00
26537000000118274568
2026年09月20日
*生产生活服务*住宿费 907.55 6% 54.45
"""


def test_item_names_are_read_from_the_text_layer():
    """数电票项目名目写作「*大类*具体名目」，是**自包含 token**，不依赖版式分栏。

    这张票的判分类本来就不需要知道"这行在版面的左边还是右边"，只需要这个名目本身。
    此前把「分类」和「商户」一起当成"分栏不可靠"放弃，于是票面上明写着住宿费
    却落了「待确认」。
    """
    assert _item_names(_SYNTH) == ["住宿费"]
    assert _merchant_candidates(_SYNTH) == ["某某文化传播有限公司", "某某酒店管理有限公司"]


def test_text_layer_returns_items_and_candidates_but_does_not_guess_the_seller():
    """文本层把读到的原文交出去；**不认定谁是卖方**（顺序不等于分栏），交给后端判。"""
    res = parse_from_text_layer(_SYNTH, "synthetic.pdf")
    assert res["items"] == ["住宿费"]
    assert res["merchant_candidates"], "候选名称要交给后端做交叉核对"
    assert res["merchant"] == "", "引擎层不许按顺序猜卖方"
    assert res["_cloud_uploaded"] is False
    assert res["_local_text_layer"] is True
    # 读到了票面项目，就不该再说「分类需人工确认」
    assert "category" not in res["needs_human"]
    assert "分类" not in res["note"]
    assert "住宿费" in res["note"], "提示语要如实说读到了什么"


def test_text_layer_says_what_is_actually_missing():
    """读不到的才说要人工确认 —— 提示语按"实际读到了什么"写，不写死。"""
    bare = "随便一段没有项目名目、也没有主体名称的文本 2026年09月20日"
    res = parse_from_text_layer(bare, "synthetic.pdf")
    assert res["items"] == [] and res["merchant_candidates"] == []
    assert "分类" in res["note"] and "商户" in res["note"]


def test_text_layer_pdf_never_calls_the_model():
    """带文本层的 PDF 必须本机直读、**不调模型**。

    起因（2026-09-28 真机）：代码先把 PDF 当 base64 image 发出去，被 API 以
    `Invalid base64 image` 拒掉才回退本机 —— 白等一次失败调用、note 里留一句吓人的报错，
    而且与合规文件写的「数电发票 PDF 全程本机解析」不符（字节其实已经上传过、只是被拒）。
    这条按源码顺序断言：文本层的 early return 必须在构造模型请求**之前**。
    """
    import inspect
    src = inspect.getsource(recognize_receipt)
    early = src.index("if local_text:\n        return parse_from_text_layer")
    model = src.index("base64.b64encode")
    assert early < model, "文本层直读必须排在模型调用之前"
