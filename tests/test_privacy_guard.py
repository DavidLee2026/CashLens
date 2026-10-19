"""提交前隐私闸的护栏测试（2026-10-13 立）。

背景：真实客户公司名 / 项目名 / 商户名 / 协作方姓名曾经进过 14 个已跟踪文件
并推到公开仓库，靠重写 100 个提交才清干净。本文件锁住闸门的**行为**，
防止有人把它改松：中文按子串、含字母数字按整词（不误伤长数字）、通用模式能拦
「还没进黑名单的客户」、清单缺失必须 fail-closed。

⚠️ 本文件自身的纪律（很重要）：
  1. **夹具里不许出现真实客户名**——用虚构的「风禾」「777」这类占位值，
     否则闸门会拦住它自己，也会把客户名二次写回仓库。
  2. 凡是**故意**长得像隐私数据的夹具（手机号、身份证、发票号、真实公司名形态），
     行尾必须写 `privacy-ok` —— 这正是闸门规定的显式放行机制，顺便一起测了。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_privacy.py"

_spec = importlib.util.spec_from_file_location("check_privacy", SCRIPT)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _rules(tmp_path: Path, body: str):
    f = tmp_path / "deny.txt"
    f.write_text(body, encoding="utf-8")
    return cp.compile_denylist(f)


# ── 黑名单匹配语义 ────────────────────────────────────────
def test_cjk_term_matches_as_substring(tmp_path):
    rules = _rules(tmp_path, "风禾\n")
    hits = cp.scan_text("x.py", "实弹来源：风禾文化", rules)
    assert len(hits) == 1 and hits[0][3] == "风禾"


def test_numeric_code_does_not_hit_longer_numbers(tmp_path):
    """代号只能在独立出现时命中；20260777 这种日期/编号不许误伤。"""
    rules = _rules(tmp_path, "777\n")
    assert cp.scan_text("x.py", 'project="777"', rules), "独立出现应命中"
    assert cp.scan_text("x.py", "777项目", rules), "后接中文也算独立出现"
    assert not cp.scan_text("x.py", "时间戳 20260777 与 2026077712", rules), "长数字里不许命中"


def test_regex_rule_is_used_verbatim(tmp_path):
    rules = _rules(tmp_path, r"re:(?<!\d)888(?!\d)" + "\n")
    assert cp.scan_text("x.py", "888 那批活动", rules)
    assert not cp.scan_text("x.py", "1888000", rules)


# ── 通用模式：拦「还没进黑名单的下一个客户」──────────────────
@pytest.mark.parametrize("text,label", [
    ("发票号 10102000000012345678", "发票号码"),  # privacy-ok 测试夹具：20 位假发票号
    ("rel_path: 微信图片_20250101120000.jpg", "微信导出文件名"),  # privacy-ok 测试夹具：假导出文件名
    ("联系电话 13800130000", "手机号"),  # privacy-ok 测试夹具：假手机号
    ("身份证 110101199003070000", "身份证号"),  # privacy-ok 测试夹具：假身份证号
    ("北京天工科技有限公司", "中文公司名"),  # privacy-ok 测试夹具：虚构公司名
])
def test_generic_patterns_catch_unknown_client(text, label):
    hits = cp.scan_text("x.py", text, [])
    assert any(label in src for _f, _l, src, _g in hits), f"未拦住：{text}"


def test_placeholder_company_name_is_allowed():
    """占位符「某…有限公司」是规定写法，不能反过来被闸门拦死（含长前缀的情况）。"""
    assert not cp.scan_text("x.py", "商户：某文化传播有限公司", [])
    assert not cp.scan_text("x.py", "商户：某连锁便利店有限公司", [])
    assert not cp.scan_text("x.py", "某文化传播有限公司（买方）与某酒店管理有限公司（卖方）", [])


def test_privacy_ok_escape_requires_trace():
    text = "联系电话 13800130000  # privacy-ok 合成样例，非真实号码"  # privacy-ok 测试夹具：验证放行机制
    assert not cp.scan_text("x.py", text, [])


# ── 路径/类型规则 ────────────────────────────────────────
def test_binary_blocked_outside_samples_only():
    assert cp.path_blocked("foo/receipt.pdf"), "pdf 必须拦"
    assert cp.path_blocked("app/shot.png"), "图片必须拦"
    assert cp.path_blocked("data/ledger.csv"), "csv 必须拦"
    assert cp.path_blocked("samples/test_receipt.png") is None, "samples/ 是合成样例区"
    assert cp.path_blocked("frontend/app/page.tsx") is None


# ── fail-closed ─────────────────────────────────────────
def test_missing_denylist_fails_closed(tmp_path):
    with pytest.raises(SystemExit) as e:
        cp.load_denylist(str(tmp_path / "nope.txt"))
    assert e.value.code == 2, "清单缺失必须退出码 2，不许静默通过"


def test_commit_message_is_scanned(tmp_path):
    rules = _rules(tmp_path, "风禾\n")
    hits = cp.scan_text("<提交信息>", "修：风禾那批材料的报销表解析", rules)
    assert hits and hits[0][2].startswith("黑名单")


# ── 真实仓库当前必须干净（这条最重要）─────────────────────
def test_current_repo_has_no_real_client_names():
    deny = REPO.parent / ".privacy" / "real-names.txt"
    if not deny.is_file():
        pytest.skip("黑名单不在仓库上一级（换机器后需重建）")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--all"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert r.returncode == 0, "仓库里又出现真实客户名了：\n" + r.stdout + r.stderr
