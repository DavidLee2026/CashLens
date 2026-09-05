"""记账 SKILL（LLM 解析层）辅助函数测试 —— 不做网络调用。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import llm_skill  # noqa: E402


def test_extract_json_plain():
    obj = llm_skill.extract_json('{"actions": [], "reply": "好的"}')
    assert obj == {"actions": [], "reply": "好的"}


def test_extract_json_with_fence_and_noise():
    text = '```json\n{"actions": [{"kind": "record", "amount_cents": 29900}], "reply": "已记"}\n```'
    obj = llm_skill.extract_json(text)
    assert obj["actions"][0]["amount_cents"] == 29900


def test_extract_json_prefix_noise():
    obj = llm_skill.extract_json('好，我记一下：{"actions":[],"reply":"ok"} 完')
    assert obj["reply"] == "ok"


def test_extract_json_raises_on_empty():
    try:
        llm_skill.extract_json("（没有输出）")
        assert False, "应当抛错"
    except ValueError:
        pass


def test_parse_accounting_returns_none_without_key(monkeypatch):
    # 模拟无 .env / 无 Key：parse_accounting 直接回 None（调用方回退规则层），且不发起网络
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(llm_skill, "ENV_PATH", Path("/nonexistent-cashlens-env"))
    assert llm_skill.parse_accounting("打车花了 28") is None
