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

_SYSTEM_PROMPT = """你是 CashLens 的记账员（财务语义理解层）。用户会说一句话：可能是「记账」「问现金流」或「闲聊」。

必须只输出一个合法 JSON 对象（不要 markdown 解释），结构：
{"actions":[], "reply":"给用户的一句简短中文回复"}

actions 元素类型：
1. 记账：{"kind":"record","direction":"income|expense","amount_cents":整数(金额以分计),"channel":"wechat|alipay|bank|cash|receipt|voice|manual","category":"简短中文分类","counterparty":"对手方(可空)","note":"原话要点","needs_confirm":true}
   - 规则：金额以分为单位（299 元 = 29900）；一句话可含多笔；无法确定方向时方向取 income 的显式信号（收到/客户支付/进账），否则 expense。
2. 问现金流：{"kind":"ask_cashflow"}（仅当用户问下月/现金流/缺钱等，数值一律不要编，由系统真计算回答，你只需触发）
3. 纯聊天/不确定：actions 为空，reply 给出友好回答或引导（可提醒可记账）。

诚实纪律：绝不编造账目数字或现金流数值；金额只来自用户这句话。
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
