---
name: cashflow-forecast
description: 现金流预测与缺口预警——当用户问「现金流怎么样 / 下个月会不会缺钱 / 状态如何 / 能不能撑住」时使用。
---

# Cashflow Forecast（现金流预测）

## 触发时机

用户出现现金流相关意图：`现金流`、`下个月会不会缺钱`、`缺口`、`撑不撑得住`、`状态/健康度`、`预测`。

## 执行步骤

1. 调用 MCP 工具 `get_cashflow_status`（读本地事件账本 → state_engine 真计算），必要时按 `as_of` 查询历史时点；
2. 需要更长期限时使用后端 `/api/forecast?horizon_days=30|90` 的 90% 置信带；
3. 按以下口径输出，**禁止编造数字**。

## 输出口径（诚实纪律）

| 项 | 口径 |
|:--|:--|
| 状态标签 | unknown → 现金流不明；learning → 学习中；fragile → 现金流脆弱；stable → 稳健；misconception → 财务误区 |
| 期末余额 | 给**区间**（90% 置信带），不给假装精确的点值 |
| 可信度低 | 明示「现金流不明」，不放宽成精确预测 |
| 缺口 | `gap_30d.exists` + 金额（分）；存在缺口 → 附一句「提前备粮」建议 |
| 证据 | 回复中带 `as_of`，标注数据来源 = 本地事件账本 + 状态引擎 |

## 反例（禁止）

- 不用「大概 5 万」这种无区间点值；
- 不把「记了几笔账」等同于「现金流健康」（证据分级哲学）；
- 不凭语气猜测用户财务状态（guess = 0.00，不算证据）。

## 关联

- 数据源：`backend/app/services/state_engine/calc.py::forecast_cashflow`
- 供数 MCP：`engine/mcp/cashflow_mcp.py`（state-engine-real-v1）
