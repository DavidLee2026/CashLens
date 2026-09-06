"""会话记忆（多轮自由对话）单测 —— 纯内存，不触网。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import session_mem  # noqa: E402


def test_add_and_context(monkeypatch):
    monkeypatch.setattr(session_mem, "_sessions", {})
    session_mem.touch("s1")
    assert session_mem.context_of("s1") == ""
    session_mem.add_turn("s1", "我最近一笔收入？", "你最近一笔收入是 ¥300.00。", "最近收入 ¥300")
    ctx = session_mem.context_of("s1")
    assert "我最近一笔收入" in ctx and "¥300" in ctx


def test_turn_cap(monkeypatch):
    monkeypatch.setattr(session_mem, "_sessions", {})
    for i in range(session_mem.MAX_TURNS + 5):
        session_mem.add_turn("cap", f"第{i}句", f"答{i}")
    ctx = session_mem.context_of("cap")
    assert "第0句" not in ctx  # 超出窗口被裁剪
    assert "第" in ctx


def test_clear(monkeypatch):
    monkeypatch.setattr(session_mem, "_sessions", {})
    session_mem.add_turn("c1", "hi", "hello")
    session_mem.clear("c1")
    assert session_mem.context_of("c1") == ""
