#!/usr/bin/env python3
"""豆包语音识别 · 录音文件识别极速版（Flash）测试客户端

用途：验证火山引擎「录音文件识别极速版」的配置与识别质量（复赛 Phase 2 预研）
文档：https://docs.volcengine.com/docs/6561/1631584?lang=zh
特性：一次请求即返回结果（无 submit/query 轮询）；音频 ≤2h/≤100MB；WAV/MP3/OGG OPUS

用法：
  export ASR_XAPI_KEY="<新版控制台语音 API Key>"
  python3 recognize_flash.py <音频文件>
  python3 recognize_flash.py <音频文件> --model bigmodel
"""
import argparse
import base64
import json
import os
import sys
import uuid
import urllib.error
import urllib.request

URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"  # 极速版资源 ID（需控制台开通）


def recognize(audio_path: str, model: str = "bigmodel") -> None:
    key = os.environ.get("ASR_XAPI_KEY", "").strip()
    if not key:
        print("❌ 缺少 API Key：请先 export ASR_XAPI_KEY=\"<语音识别 API Key>\"")
        sys.exit(1)
    if not os.path.exists(audio_path):
        print(f"❌ 音频不存在: {audio_path}")
        sys.exit(1)

    with open(audio_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "user": {"uid": key},
        "audio": {"data": b64},
        "request": {"model_name": model, "enable_punc": True},
    }
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": key,                 # 新版控制台：仅 X-Api-Key
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            code = resp.headers.get("X-Api-Status-Code")
            msg = resp.headers.get("X-Api-Message")
            logid = resp.headers.get("X-Tt-Logid")
            body = json.loads(resp.read().decode("utf-8"))
        print(f"状态码: {code} | 消息: {msg} | logid: {logid}")
        if code != "20000000":
            print(f"❌ 识别失败，响应: {json.dumps(body, ensure_ascii=False)[:500]}")
            return
        result = body.get("result") or {}
        text = result.get("text", "")
        dur = (body.get("audio_info") or {}).get("duration")
        print(f"音频时长: {dur} ms" if dur else "")
        print(f"识别文本: {text}")
    except urllib.error.HTTPError as e:
        print(f"❌ HTTP {e.code}: {e.read().decode('utf-8')[:400]}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ {type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="豆包语音识别极速版测试")
    p.add_argument("audio", help="音频文件路径（WAV/MP3/OGG OPUS）")
    p.add_argument("--model", default="bigmodel", help="模型名（默认 bigmodel）")
    a = p.parse_args()
    recognize(a.audio, a.model)
