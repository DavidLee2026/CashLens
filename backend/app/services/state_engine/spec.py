"""状态引擎参数与常量（对应 02-脑暴/财务状态引擎设计-v0.2定稿.md）"""

SCHEMA_VERSION = 3  # v3：事件新增 project 字段（v2 事件无此字段，读取时按空串兼容，不需迁移）

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

# ─── 项目维度（v3 新增）─────────────────────────────────
# 设计约束（2026-09-24 拍板）：账本只存稳定 id（project_a / project_b ...），
# 显示名放 data/projects.json 的映射表，重命名只改映射，账本一字不动（守住账本不可变）。
PROJECT_ID_PREFIX = "project_"        # 默认 id 前缀
DEFAULT_PROJECT_LABEL = "Project"     # 未命名项目显示名前缀，如 Project A
UNASSIGNED_PROJECT = ""               # project 为空串 = 未归项目
MAX_DEFAULT_PROJECTS = 26             # 默认名兜底上限（A 至 Z）
