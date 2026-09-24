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
