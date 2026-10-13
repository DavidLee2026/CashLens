"""外部账单格式样例的回归测试（2026-09-28 立）。

**为什么要有这个文件**：项目自有的 90 笔真实账单只能测出"David 遇到过的情况"，
测不出格式与商户名的多样性。这两份第三方公开样例（见
`samples/bill_format_external/SOURCE.md`）一跑，就打出三个只靠自有数据
永远发现不了的洞 —— 尤其有一个是**分类逻辑写了两份**导致的静默绕过。

三处修复分别钉在下面：
  ① 「文具/收纳」曾把个人消费判成**经营**（可抵成本口径，危害不止分类）
  ② 歧义平台值「生活服务」曾被一刀切成**居住**（洗衣 ≠ 房租水电）
  ③ `parse_bill_csv.normalize_record` 曾**自行实现**一遍平台映射，
     绕过 `categories.normalize_category` 里的歧义判断 —— 这条最容易复发，
     所以既断言行为、也断言"只调用权威函数"。”

测试跑的是**真实的解析脚本**（`parse_bill_csv.py`），不是 mock —— 这样连列映射、
编码、汇总头跳过一起验了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "backend"))
sys.path.insert(0, str(_ROOT / "scripts"))

from app.services import categories as c  # noqa: E402

_SAMPLES = _ROOT / "samples" / "bill_format_external"
_WECHAT = _SAMPLES / "wechat_raw.csv"
_ALIPAY = _SAMPLES / "alipay_raw.csv"


# ── ① 「文具/收纳」不再落经营 ────────────────────────────────

def test_stationery_is_shopping_not_business():
    """外部样例真身：宿舍收纳盒的商品说明里含「文具」，曾被判成经营。"""
    desc = "化妆品收纳盒学生宿舍文具桌面零食塑料收纳箱家用杂物筐储物盒子"
    assert c.match_category("稻草**店", desc) == "购物"


def test_real_office_supplies_still_business():
    """修法不能把真正的办公采购一起改掉：经营侧靠「办公/打印/复印」抓住。"""
    assert c.match_category("办公用品店", "办公文具一批") == "经营"
    assert c.match_category("打印店", "打印复印") == "经营"
    # 纯文具（无办公语境）按商品名归购物
    assert c.match_category("文具店", "中性笔 3 支") == "购物"
    # 「文具」必须已经不在经营词表里，否则上面第二组会随描述文本漂移
    business_words = dict(c.KEYWORD_RULES)["经营"]
    assert "文具" not in business_words
    assert "文具" in dict(c.KEYWORD_RULES)["购物"]


# ── ② 歧义平台值必须关键词确认 ──────────────────────────────

def test_ambiguous_platform_value_needs_keyword():
    """「生活服务」太宽：没有确认词时不下结论，绝不硬塞「居住」。"""
    assert c.normalize_category("生活服务", "UjingDevice", "U净洗烘") == c.UNCONFIRMED_CATEGORY
    # 有确认词时才认平台值
    assert c.normalize_category("生活服务", "物业公司", "房租") == "居住"
    assert c.normalize_category("生活服务", "电力公司", "电费") == "居住"


def test_exact_platform_value_still_trusted():
    """精确平台值不受影响（回归护栏）：交通出行/文化休闲/转账红包 直接采纳。"""
    assert c.normalize_category("交通出行", "哈尔滨市城市通智能卡有限责任公司", "地铁") == "交通"
    assert c.normalize_category("文化休闲", "哔哩哔哩会员购", "bilibili票务") == "娱乐"
    assert c.normalize_category("转账红包", "lxt(***)", "转账") == "人情"


def test_ambiguous_set_only_holds_ambiguous_values():
    """放进歧义表的必须是真歧义值 —— 否则会把本来精确的映射降级成关键词。"""
    assert "生活服务" in c.PLATFORM_NEEDS_KEYWORD
    # 精确值不许进歧义表
    for precise in ("餐饮美食", "交通出行", "酒店旅游", "转账红包"):
        assert precise not in c.PLATFORM_NEEDS_KEYWORD


# ── ③ 解析器只调用权威函数（防"逻辑写两份"复发）──────────────

def test_parser_does_not_reimplement_platform_mapping():
    """护栏：`normalize_record` 必须调用 `categories.normalize_category`。

    起因就是它自己写了一遍「查表 → 关键词 → 兜底」，于是 categories 里新加的
    歧义判断在这条通道上**静默失效**（表现是外部样例仍然判成「居住」）。
    表引用统一了、逻辑没统一 = 只统一了半张表，所以这条按源码钉住。
    """
    src = (_ROOT / "scripts" / "parse_bill_csv.py").read_text(encoding="utf-8")
    body = src[src.index("def normalize_record"):]
    body = body[: body.index("\ndef ", 10)]
    assert "_cat.normalize_category(" in body, "normalize_record 必须走权威分类函数"
    assert "for name, words in _CATEGORY_RULES" not in body, "不得再自行实现关键词下沉"


# ── 端到端：真实解析脚本跑真实样例 ───────────────────────────

@pytest.fixture(scope="module")
def parsed(tmp_path_factory) -> list[dict]:
    """用真实脚本解析两份外部样例，返回产出的账本草稿。

    `parse_bill_csv.main()` 无参数、自己解析 `sys.argv`，所以这里补 argv
    （脚本没为库调用留入口，是它的现状；补 argv 比改造脚本风险小）。
    产出目录用 tmp_path，**不写仓库 data/**（那是用户真实数据目录）。
    """
    import parse_bill_csv as pb  # noqa: E402  （scripts/ 已加入 sys.path）

    out = tmp_path_factory.mktemp("ext_bill_out")
    argv = [str(_WECHAT), str(_ALIPAY), "--out", str(out)]
    saved = sys.argv
    sys.argv = ["parse_bill_csv.py", *argv]
    try:
        rc = pb.main()
    finally:
        sys.argv = saved
    assert rc in (0, None), f"解析脚本返回非 0：{rc}"
    rows = [json.loads(l) for l in (out / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows, "解析没有产出任何记录 —— 列映射或编码可能坏了"
    return rows


def test_external_samples_parse_to_expected_count(parsed):
    """两份样例共 12 笔支出（微信 2 + 支付宝 10），合计 ¥942.50。

    这个数字是**实测基准**，不是估计；样例一旦更新，这里应当同时更新。
    """
    assert len(parsed) == 12
    assert all(r["direction"] == "expense" for r in parsed)
    assert sum(r["amount_cents"] for r in parsed) == 94250


def test_external_samples_classification_end_to_end(parsed):
    """端到端分类断言：三个曾经判错的商户，各自落在正确的类别上。"""
    by_cp = {}
    for r in parsed:
        by_cp.setdefault(r["counterparty"], []).append(r)

    # ① 收纳盒 → 购物（曾判经营）
    assert [r["category"] for r in by_cp["稻草**店"]] == ["购物"], "收纳盒必须归购物"
    # ② 洗衣 → 待确认（曾判居住）
    assert [r["category"] for r in by_cp["UjingDevice"]] == [c.UNCONFIRMED_CATEGORY], "洗衣不得判成居住"
    # 交通与娱乐这两条是"不该被改坏"的对照
    assert all(r["category"] == "交通" for r in by_cp["哈尔滨市城市通智能卡有限责任公司"])
    assert [r["category"] for r in by_cp["哔哩哔哩会员购"]] == ["娱乐"]


def test_parsed_rows_have_traceable_source(parsed):
    """每笔都要有判定来源，便于质量追溯（platform / keyword / fallback）。"""
    for r in parsed:
        assert r["norm_source"] in ("platform", "keyword", "fallback")
    # 歧义值未被确认的必须记 keyword（不能记成 platform，那是谎报来源）
    ujing = next(r for r in parsed if r["counterparty"] == "UjingDevice")
    assert ujing["norm_source"] == "keyword"
