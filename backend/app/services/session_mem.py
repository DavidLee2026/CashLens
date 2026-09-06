"""会话记忆（多轮自由 agent 用）——本地演示内存实现

- 每个 session_id 保留最近对话（用户原话 + 系统回答 + 上轮工具结果摘要）
- 用途：连续追问 / 跨上下文（如"那笔是哪天的？""上个月呢？"）
- 说明：内存态，后端重启即清空；正式版换 SQLite/文件持久化即可。

对外不暴露任何真实密钥；本模块只做上下文拼装。
"""

from __future__ import annotations

from datetime import datetime

MAX_TURNS = 10
_MAX_SESSIONS = 60

_sessions: dict[str, list[dict]] = {}


def touch(session_id: str) -> None:
    if session_id not in _sessions:
        _sessions[session_id] = []
    # 简单防无限增长：清最老的会话
    while len(_sessions) > _MAX_SESSIONS:
        _sessions.pop(next(iter(_sessions)), None)


def add_turn(session_id: str, user_text: str, answer: str, result_note: str = "") -> None:
    touch(session_id)
    turns = _sessions[session_id]
    turns.append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "user": user_text,
        "answer": answer,
        "result": result_note,
    })
    if len(turns) > MAX_TURNS:
        del turns[: len(turns) - MAX_TURNS]


def context_of(session_id: str) -> str:
    """把最近几轮压成给 LLM 的对话上下文（含上轮工具结果摘要）。"""
    turns = _sessions.get(session_id, [])
    if not turns:
        return ""
    lines = []
    for t in turns[-6:]:
        lines.append(f"用户: {t['user']}")
        if t.get("result"):
            lines.append(f"[工具结果] {t['result']}")
        lines.append(f"我答: {t['answer']}")
    return "\n".join(lines)


def clear(session_id: str) -> None:
    _sessions.pop(session_id, None)
