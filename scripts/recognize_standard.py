#!/usr/bin/env python3
"""豆包录音文件识别（标准版 2.0 · submit + query）客户端

实测 2026-09-04：Doubao Seed ASR AUC 2.0（volc.seedasr.auc）提交→轮询→返回文本 ✅
流程：POST submit（带音频，X-Api-Request-Id 即 task_id）→ POST query（同 task_id，空 body）轮询
资源 ID：volc.seedasr.auc = 豆包录音文件识别模型 2.0（新版）
        volc.bigasr.auc  = 豆包录音文件识别模型 1.0
        volc.bigasr.auc_turbo = 1.0 极速版（一次请求出结果，未开通）
文档：任务提交 docs.volcengine.com/docs/6561/2606791 · 结果查询 docs.volcengine.com/docs/6561/2606792

用法：
  export ASR_XAPI_KEY="<语音技术控制台 API Key>"
  python3 recognize_standard.py <音频文件> [--resource volc.seedasr.auc] [--interval 3] [--timeout 300]
"""
import argparse
import base64
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request

BASE = "https://openspeech.bytedance.com/api/v3/auc/bigmodel"
RESOURCE_2_0 = "volc.seedasr.auc"   # 豆包录音文件识别 2.0（已开通）
RESOURCE_1_0 = "volc.bigasr.auc"    # 豆包录音文件识别 1.0


def post(path: str, resource: str, payload: dict, task_id: str):
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": os.environ.get("ASR_XAPI_KEY", ""),
        "X-Api-Resource-Id": resource,
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
    }
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.headers.get("X-Api-Status-Code"), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return f"HTTP{e.code}", json.loads(e.read().decode() or "{}")


def recognize(audio_path: str, resource: str, interval: int, timeout: int) -> int:
    key = os.environ.get("ASR_XAPI_KEY", "").strip()
    if not key:
        print("❌ 缺少 API Key：export ASR_XAPI_KEY=\"<语音技术 API Key>\"")
        return 1
    if not os.path.exists(audio_path):
        print(f"❌ 音频不存在: {audio_path}")
        return 1

    b64 = base64.b64encode(open(audio_path, "rb").read()).decode("utf-8")
    task_id = str(uuid.uuid4())
    payload = {"user": {"uid": key}, "audio": {"data": b64},
               "request": {"model_name": "bigmodel", "enable_punc": True}}

    print(f"提交任务（{resource}）...")
    code, body = post("/submit", resource, payload, task_id)
    if code != "20000000":
        print(f"❌ 提交失败 [{code}]: {json.dumps(body, ensure_ascii=False)[:300]}")
        return 1

    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        n += 1
        time.sleep(interval)
        code, body = post("/query", resource, {}, task_id)
        result = body.get("result") or {}
        text = (result.get("text") or "").strip()
        if text:
            print(f"✅ 第 {n} 次轮询拿到结果（耗时约 {n * interval}s）")
            print(f"识别文本: {text}")
            return 0
        if n % 5 == 0:
            print(f"   轮询中... 第 {n} 次 (code={code})")
    print("⏰ 超时未拿到结果")
    return 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="豆包录音文件识别标准版 2.0 客户端")
    p.add_argument("audio", help="音频文件（WAV/MP3/OGG 等）")
    p.add_argument("--resource", default=RESOURCE_2_0, help="资源 ID（默认 2.0）")
    p.add_argument("--interval", type=int, default=3, help="轮询间隔秒（默认 3）")
    p.add_argument("--timeout", type=int, default=300, help="最大等待秒（默认 300）")
    a = p.parse_args()
    sys.exit(recognize(a.audio, a.resource, a.interval, a.timeout))
