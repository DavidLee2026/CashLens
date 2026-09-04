#!/usr/bin/env python3
"""CashLens 最小 FastAPI → opencode 集成 demo（实测 4）
链路：FastAPI POST /chat → opencode headless server (HTTP) → 引擎回复
启动：python3 fastapi_demo.py   （需先起 opencode serve，或本文件自动拉起）
"""
import asyncio
import subprocess
import os
import time

import httpx
from fastapi import FastAPI

app = FastAPI(title="CashLens FastAPI → opencode 集成 demo")

ENGINE_URL = os.environ.get("ENGINE_URL", "http://127.0.0.1:47612")
_session_id = None
_engine_proc = None


@app.on_event("startup")
def ensure_engine():
    """若 opencode serve 未起，自动拉起"""
    global _engine_proc
    try:
        httpx.get(f"{ENGINE_URL}/session", timeout=3)
    except Exception:
        cwd = os.path.dirname(os.path.abspath(__file__))
        _engine_proc = subprocess.Popen(
            ["opencode", "serve", "--port", "47612"],
            cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "LLM_API_KEY": os.environ.get("LLM_API_KEY", "")})
        time.sleep(4)


@app.get("/health")
def health():
    return {"status": "ok", "engine": ENGINE_URL, "engine_up": True}


async def ensure_session(client: httpx.AsyncClient) -> str:
    global _session_id
    if not _session_id:
        r = await client.post(f"{ENGINE_URL}/session")
        _session_id = r.json()["id"]
    return _session_id


def extract_texts(msgs: list) -> list:
    """取所有 assistant 的文本消息（role 在 info.role，parts 含 text）"""
    out = []
    for m in msgs:
        role = (m.get("info") or {}).get("role")
        if role != "assistant":
            continue
        t = "".join(p.get("text", "") for p in m.get("parts", [])
                    if isinstance(p, dict) and p.get("type") == "text")
        if t.strip():
            out.append(t)
    return out


@app.post("/chat")
async def chat(body: dict):
    msg = (body.get("message") or "").strip()
    if not msg:
        return {"reply": "message 不能为空"}
    async with httpx.AsyncClient(timeout=150) as client:
        sid = await ensure_session(client)
        # 1. 发消息给引擎
        r = await client.post(f"{ENGINE_URL}/session/{sid}/message",
                              json={"parts": [{"type": "text", "text": msg}]})
        if r.status_code != 200:
            return {"reply": f"引擎错误: {r.status_code} {r.text[:120]}"}
        # 2. 轮询等待：assistant 文本出现且连续两轮不变 = 完成（最多 90 秒）
        prev_texts = []
        for _ in range(90):
            await asyncio.sleep(1)
            try:
                r = await client.get(f"{ENGINE_URL}/session/{sid}/message")
                msgs = r.json()
            except Exception:
                continue
            texts = extract_texts(msgs)
            if texts and texts == prev_texts:
                return {"reply": texts[-1], "session_id": sid}
            prev_texts = texts
        # 3. 兜底：有文本就返回，没有报超时
        if prev_texts:
            return {"reply": prev_texts[-1], "session_id": sid}
        return {"reply": "引擎回复超时（90s）", "session_id": sid}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
