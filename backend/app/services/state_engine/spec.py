"""状态引擎参数与常量（对应 02-脑暴/财务状态引擎设计-v0.2定稿.md）"""

SCHEMA_VERSION = 2

# 事件类型
EVENT_TYPES = ("income", "expense", "refund", "transfer", "adjustment", "assumption", "resolution")

# 证据强度定稿（flow > receipt > voice > guided > self_report > guess）
EVIDENCE_WEIGHTS = {
    "flow": 1.00,
    "receipt": 0.85,
    "voice": 0.75,
    "guided": 0.45,
    "self_report": 0.10,
    "guess": 0.00,
}

# 现金流可信度衰减
DECAY_BASE = 0.9
DEFAULT_STABILITY_DAYS = 30  # 推荐值：保守，实测后校准
CONFIDENCE_FLOOR = 0.0

# 财务健康度收敛更新（v0.1 初稿沿用 0.35）
LEARNING_RATE = 0.35
INITIAL_HEALTH = 0.5

# 现金流区间预测
FORECAST_WINDOW_DAYS = 90      # 取过去 90 天节奏
BAND_Z = 1.65                  # 90% 置信带近似（正态）

# 状态标签
LABEL_UNKNOWN = "unknown"
LABEL_LEARNING = "learning"
LABEL_FRAGILE = "fragile"
LABEL_REVIEW_DUE = "review_due"
LABEL_STABLE = "stable"
LABEL_MISCONCEPTION = "misconception"

# 方向：哪些事件强化健康（收入类）
HEALTH_POSITIVE_TYPES = ("income", "refund")

# 决策前置检查
PREREQUISITES = ("cash_runway", "receivable_cycle")
