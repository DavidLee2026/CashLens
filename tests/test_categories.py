"""分类体系单测：唯一权威表 + 报销别名 + 旧分类名归一化。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import categories as c  # noqa: E402


def test_categories_are_unique_and_ordered():
    assert len(set(c.CATEGORIES)) == len(c.CATEGORIES)
    assert "餐饮" in c.CATEGORIES and "交通" in c.CATEGORIES and "住宿" in c.CATEGORIES
    assert c.UNCONFIRMED_CATEGORY in c.CATEGORIES
    # 「不计收支」是标记不是分类，不能混进分类清单
    assert c.NEUTRAL_CATEGORY not in c.CATEGORIES


def test_match_category_by_purpose_not_merchant():
    """按用途判，不按商户判：同一家便利店，买早餐算餐饮、买纸巾算购物。"""
    assert c.match_category("云南711便利店买饭") == "餐饮"
    assert c.match_category("云南711便利店", "早餐加午餐") == "餐饮"
    assert c.match_category("云南711便利店", "买纸巾") == "购物"


def test_match_category_common_cases():
    assert c.match_category("打车去上海机场") == "交通"
    assert c.match_category("高德打车") == "交通"
    assert c.match_category("昆明恒隆广场汉堡王") == "餐饮"
    assert c.match_category("酒店住宿费") == "住宿"
    assert c.match_category("交社保") == "社保税费"
    assert c.match_category("给客户发红包") == "人情"
    assert c.match_category("DeepSeek API 费用") == "经营"
    assert c.match_category("") == c.UNCONFIRMED_CATEGORY
    assert c.match_category("一个完全陌生的商户名") == c.UNCONFIRMED_CATEGORY


def test_report_aliases_map_to_first_level():
    """用户嘴里的「XX报销」要能落到一级分类。"""
    assert c.match_category("交通报销") == "交通"
    assert c.match_category("餐饮报销") == "餐饮"
    assert c.match_category("办公费") == "经营"
    assert c.match_category("住宿费") == "住宿"


def test_normalize_legacy_category_names():
    """账本里可能存着旧分类名，要能归一化到新体系（否则老账目取不到税目候选）。"""
    assert c.normalize_category("设计") == "经营"
    assert c.normalize_category("房租") == "居住"
    assert c.normalize_category("其他") == c.UNCONFIRMED_CATEGORY
    assert c.normalize_category("") == c.NEUTRAL_CATEGORY
    assert c.normalize_category("不计收支") == c.NEUTRAL_CATEGORY
    # 已经是一级分类名的原样返回
    assert c.normalize_category("餐饮") == "餐饮"


def test_platform_map_and_unknown():
    assert c.match_platform("餐饮美食") == "餐饮"
    assert c.match_platform("交通出行") == "交通"
    assert c.match_platform("商户消费") is None      # 通道类型，不含类别信息
    assert c.match_platform("投资理财") is None
    assert c.match_platform("没见过的平台分类") is None


def test_rigid_and_business_flags():
    assert c.is_rigid("社保缴费") is True
    assert c.is_rigid("房租") is True
    assert c.is_rigid("打车去机场") is False
    assert c.is_business("DeepSeek API 费用") is True
    assert c.is_business("超市买菜") is False


def test_income_category():
    assert c.match_income_category("客户支付尾款") == "接单"
    assert c.match_income_category("发工资了") == "工资"
    assert c.match_income_category("退款到账") == "退款"
    assert c.match_income_category("一笔说不清的钱") == "其他收入"


def test_prompt_lines_cover_all_categories():
    line = c.expense_category_line()
    for cat in c.CATEGORIES:
        assert cat in line
    assert c.UNCONFIRMED_CATEGORY in line
    assert c.NEUTRAL_CATEGORY not in line  # 不进提示词，它不是分类
