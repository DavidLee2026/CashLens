"""记账 SKILL · LLM 主解析（OpenAI 兼容统一网关，厂商可插拔）

对应 02-脑暴/金额语义层分工规格-20260905.md：
SKILL(LLM) 主解析 → 规则层兜底。输出统一 draft schema（amount_cents 用分）。
隐私口径：自由对话文本会上云（页面明示）；金额/状态仍由本地 state_engine 真计算。
成本纪律：仅在配置了 LLM_API_KEY 时启用；失败/超时/未配置 → 调用方回退规则层。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/app/services/llm_skill.py → 仓库根
ENV_PATH = REPO_ROOT / ".env"

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"  # 火山方舟（OpenAI 兼容）
_DEFAULT_MODEL = "doubao-seed-2-0-lite-260428"

_SYSTEM_PROMPT = """你是 CashLens 的记账员与财务对话助手（语义理解层）。用户的一句话可能是：记账、查账（最近流水/收支）、问现金流，或闲聊/咨询产品。

必须只输出一个合法 JSON 对象（不要 markdown 解释、不要代码围栏），结构：
{"actions":[], "reply":"给用户的一句简短中文回复"}
actions 只能使用以下白名单（不能发明其他 kind）：

1. record 记账：{"kind":"record","direction":"income|expense","amount_cents":整数金额(以分计),"channel":"wechat|alipay|bank|cash|receipt|voice|manual","category":"见分类表","counterparty":"对手方(可空)","note":"要点"}
   - 金额以分为单位：299 元 = 29900；一句话可含多笔（每笔一个 record action）。
   - 方向：有显式收入信号（收到/客户支付/进账/尾款到账/退款）→ income；否则 expense。
   - 分类表：接单 / 工资 / 餐饮 / 交通 / 房租 / 购物 / 娱乐 / 其他（尽量选接近的）。
   - 「报销 X 元」含义 = 垫付的支出：记 expense（如交通/餐饮），note 注明"报销垫付"，不要记成收入。
   - 不要 claim「待确认入账」：当前没有确认流程，记账即入账（证据 0.75 主动记录）。

2. 数据查询类（数值全部由系统真计算/真账本回答，你只负责触发；reply 可留空）：
   {"kind":"ask_cashflow"}      问现金流/预测/缺口/下个月会不会缺钱
   {"kind":"ask_latest_income"} 最近一笔收入是多少
   {"kind":"ask_latest_expense"} 最近一笔支出
   {"kind":"ask_spending"}      这个月花了多少/钱花哪了/分类汇总
   {"kind":"ask_recent"}        最近流水/最近几笔

3. 其余全部 = 纯聊天/咨询：actions 为空数组，把回答写进 reply。

【产品能力白名单 —— 回答功能/导入/报销问题时只准引用以下事实，禁止承诺不存在的功能】
- 记账：口述/语音一句话记账（当前对话就是）；微信/支付宝官方账单 CSV 解析能力已就绪（后端/脚本通道）。
- CSV 导入：用户可说明导出「微信/支付宝官方账单 CSV」；解析通道已有，但工作台页面拖拽上传尚未上线——如实告知「目前界面还没有拖拽入口，CSV 解析在后台通道/后续版本」，不要假装页面能传文件。
- 票据/发票：票据/截图 OCR 识别通道存在，识别结果需人工确认（证据 0.85）；报销应当保留发票/票据凭证（不能只口头说就当有票）。
- 实时查询：最近流水、最近一笔收入/支出、本月支出分类、现金流 30 天区间（真数据）。
- 尚无：待确认入账流程、上传界面、多用户/登录。

【诚实纪律】
- 绝不编造任何账目金额或现金流数值；与账有关的问题优先选数据查询 action，让系统给真数。
- reply 措辞简短、口语化、不啰嗦；不确定时可引导用户"记一笔"或"问我现金流/最近流水"。
"""


def _load_env() -> None:
    """把仓库根 .env 读进环境（已存在的值不覆盖；只读，不输出任何值）。"""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"')


def is_configured() -> bool:
    _load_env()
    return bool(os.environ.get("LLM_API_KEY"))


def _model() -> str:
    return os.environ.get("LLM_MODEL") or _DEFAULT_MODEL


def _base_url() -> str:
    return (os.environ.get("LLM_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def chat_complete(user_text: str, context: str = "", timeout: int = 30) -> str:
    """调 LLM 返回文本（OpenAI 兼容 chat/completions）。失败抛异常由调用方兜底。"""
    _load_env()
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("LLM_API_KEY 未配置")
    system = _SYSTEM_PROMPT
    if context:
        system += f"\n\n当前系统上下文（只作参考，勿照抄数值）：\n{context}"
    body = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }
    req = urllib.request.Request(
        f"{_base_url()}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """从模型输出中稳健提取 JSON（容忍 ```json 围栏与前后杂字）。"""
    if not text:
        raise ValueError("空输出")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出中无 JSON 对象")
    return json.loads(text[start : end + 1])


def parse_accounting(text: str, context: str = "") -> dict | None:
    """LLM 主解析：返回 {actions, reply}；任何失败返回 None（调用方回退规则层）。"""
    try:
        content = chat_complete(text, context)
        obj = extract_json(content)
        if not isinstance(obj.get("actions"), list):
            raise ValueError("缺 actions")
        return {"actions": obj["actions"], "reply": str(obj.get("reply", ""))}
    except Exception:
        return None
