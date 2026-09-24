#!/usr/bin/env python3
"""发票号码校验与双源交叉核对 —— 实弹复测脚本

背景：2026-09-04 实弹发现，真实数电发票（08-实测/530.pdf）的 20 位发票号码
被云端 VLM 漏读 1 位（连续 0 段被压缩一位），且模型自评 confidence 仍为 1.0。

本脚本复测三层修复：
  1. 本机 PDF 文本层解析（图像不出本机）能否拿到完整的 20 位号码
  2. 双源交叉核对能否把 19 位错值修正回 20 位
  3. 不该采纳的情况能否一律拒绝（不猜、不编）

隐私约定：脚本内不硬编码任何真实票面数字，全部从 08-实测/530.pdf 运行时读出；
反例用例一律使用合成号码。本仓会推送到公开远端，而 .gitignore 只排除 *.pdf 与 data/，
管不到 .py 里的字符串。
⚠️ 运行输出会打印真实票面数字，仅供本地核对，请勿直接粘贴进公开材料。

用法：
  cd 09-代码 && python3 scripts/verify_invoice_no.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "mcp"))
import receipt_mcp as rc  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PDF = REPO / "08-实测" / "530.pdf"

# 合成号码：形态与真实缺陷一致，数字本身不是任何真实票据
SYN_TRUTH = "26312000005416152000"      # 20 位
SYN_WRONG = "2631200005416152000"       # 19 位，即 SYN_TRUTH 连续 0 段被压缩一位
SYN_BANK = "31050161413700000099"       # 20 位，第 3 至 4 位不在行政区划白名单

_pass = 0
_fail = 0


def check(name: str, got, want):
    global _pass, _fail
    ok = got == want
    _pass += 1 if ok else 0
    _fail += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        期望 {want!r}，实际 {got!r}")


def main() -> int:
    print("=" * 66)
    print("一、本机 PDF 文本层解析（问题3 的隐私路径：图像不出本机）")
    print("=" * 66)

    text = None
    truth = None
    if not PDF.exists():
        print(f"  跳过：找不到真实票据 {PDF}")
    else:
        text = rc.extract_pdf_text_layer(str(PDF))
        print(f"  文本层字符数：{len(text) if text else 0}")
        if text:
            cands = rc._twenty_digit_candidates(text)
            print(f"  文本层 20 位数字串候选数：{len(cands)}（含发票号码与销方银行账号）")
            res = rc.recognize_receipt(str(PDF), local_only=True)
            truth = res.get("invoice_no")
            print(f"  本机解析结果：invoice_no={truth}")
            print(f"                 date={res.get('date')}  amount={res.get('amount')}")
            print(f"                 _cloud_uploaded={res.get('_cloud_uploaded')}"
                  f"  needs_human={res.get('needs_human')}")
            check("本机拿到 20 位号码", len(truth or ""), 20)
            check("号码通过格式校验", res.get("invoice_no_check", {}).get("ok"), True)
            check("号码来自文本层候选", truth in cands, True)
            check("图像未出本机", res.get("_cloud_uploaded"), False)
            check("开票日期解析正确", res.get("date"), "2026-08-26")
            check("价税合计解析正确", res.get("amount"), 530.0)
            check("商户留空待人工确认（文本流顺序不可靠）", res.get("merchant"), "")

    print()
    print("=" * 66)
    print("二、双源交叉核对（问题2 主修复：少读 1 个 0 能否修正）")
    print("=" * 66)

    if text and truth and "0" in truth:
        # 动态构造"漏读一个 0"的错值，不硬编码真实数字
        cut = truth.index("0")
        wrong = truth[:cut] + truth[cut + 1:]
        print(f"  用真实文本层 + 动态删掉一位 0 构造错值（长度 {len(wrong)} 位）")
        cross = rc.crosscheck_invoice_no(wrong, text)
        print(f"  核对结果：ok={cross['ok']}")
        print(f"            {cross['reason']}")
        check("交叉核对把错值修回 20 位真值", cross["no"], truth)
    else:
        print("  跳过真实场景（无文本层），改跑合成场景")

    syn_text = f"发票号码：\n{SYN_TRUTH}\n开票日期：2026年08月26日\n"
    cross = rc.crosscheck_invoice_no(SYN_WRONG, syn_text)
    check("合成场景：19 位错值修正为 20 位", cross["no"], SYN_TRUTH)
    check("合成场景：判定为通过", cross["ok"], True)

    print()
    print("=" * 66)
    print("三、反例（不该采纳的必须拒绝，不猜不编）")
    print("=" * 66)
    bad = rc.crosscheck_invoice_no(SYN_TRUTH[:-1] + "1", syn_text)
    check("末位数字不符（非插入 0）不采纳", bad["ok"], False)
    # 含两个分离 0 段的 19 位串：插入位置不同会得到两个不同候选，必须拒绝
    amb = rc.crosscheck_invoice_no("1000200034567890123",
                                   "10000200034567890123 10002000034567890123")
    check("候选不唯一时不采纳", amb["ok"], False)
    check("位数差得太多时不采纳", rc.crosscheck_invoice_no("12345", syn_text)["ok"], False)
    check("银行账号形态的 20 位串被结构筛选排除",
          rc._structurally_plausible([SYN_TRUTH, SYN_BANK], "2026-08-26"), [SYN_TRUTH])

    print()
    print("=" * 66)
    print("四、号码格式校验")
    print("=" * 66)
    check("20 位数字通过", rc.validate_invoice_no(SYN_TRUTH)["ok"], True)
    check("19 位数字不通过", rc.validate_invoice_no(SYN_WRONG)["ok"], False)
    check("含空格自动归一化",
          rc.validate_invoice_no(f"{SYN_TRUTH[:4]} {SYN_TRUTH[4:8]} {SYN_TRUTH[8:]}")["no"],
          SYN_TRUTH)
    check("空号码不通过", rc.validate_invoice_no("")["ok"], False)
    check("区划白名单外给软提示",
          len(rc.validate_invoice_no(SYN_TRUTH[:2] + "99" + SYN_TRUTH[4:])["warnings"]), 1)

    print()
    print("=" * 66)
    print(f"结果：{_pass} 通过 / {_fail} 失败")
    print("=" * 66)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
