#!/usr/bin/env python3
"""CashLens 票据识别 MCP 服务器（采集模块）
工具：recognize_receipt —— 图片（发票/小票/消费截图）→ 火山方舟 doubao-seed-2.0 识图 → 结构化交易
协议：stdio JSON-RPC（纯标准库，无第三方依赖）
"""
import base64
import json
import mimetypes
import os
import sys
import urllib.request

# ─── 常量 ───────────────────────────────────────────────
BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
MODEL = os.environ.get("LLM_MODEL", "doubao-seed-2-0-lite-260428")
API_KEY = os.environ.get("LLM_API_KEY", "")

RECOGNIZE_PROMPT = """你是一个票据识别助手。请识别这张图片中的票据/小票/消费截图信息，只输出一个 JSON（不要 markdown 代码块，不要多余文字）：

{
  "type": "expense|income",
  "amount": 数字,
  "date": "YYYY-MM-DD",
  "merchant": "商户名称",
  "category": "餐饮|交通|购物|房租|其他",
  "invoice_no": "发票号码(没有则空)",
  "items": [{"name": "商品名", "amount": 数字}],
  "note": "备注",
  "confidence": 0-1,
  "evidence": "recognition"
}

规则：
1. 金额从票据中提取，保留两位小数；无法识别金额时 amount 填 0 并 confidence 低于 0.3
2. 看不出是支出还是收入时默认 expense
3. 图片内容模糊或不是票据时，confidence 填 0，并在 note 里说明"无法识别"
4. 只输出 JSON"""


def log(msg: str):
    sys.stderr.write(f"[receipt-mcp] {msg}\n")
    sys.stderr.flush()


def send(msg: dict):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ─── 核心：图片 → 结构化交易 ────────────────────────────
def recognize_receipt(image_path: str) -> dict:
    """读图片 → 调火山方舟识图 → 结构化交易 JSON"""
    if not API_KEY:
        return {"error": "LLM_API_KEY 未配置"}
    if not os.path.exists(image_path):
        return {"error": f"图片不存在: {image_path}"}

    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": RECOGNIZE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]
        }],
        "max_tokens": 2000
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        # 提取 JSON（去掉可能的 markdown 包裹）
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content)
        result["_raw_image"] = image_path
        return result
    except urllib.error.HTTPError as e:
        return {"error": f"API HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ─── MCP stdio 协议 ─────────────────────────────────────
TOOL_DEF = {
    "name": "recognize_receipt",
    "description": "识别发票/小票/消费截图的图片，提取结构化交易信息（金额/日期/商户/分类）。当用户说「识别这张发票」「拍个票据记账」「帮我看看这个收据」或提供图片路径时调用。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "票据图片的本地文件路径"}
        },
        "required": ["image_path"]
    }
}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "receipt-mcp", "version": "0.1.0"}
            }})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [TOOL_DEF]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "recognize_receipt":
                result = recognize_receipt(args.get("image_path", ""))
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "isError": False
                }})
            else:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": f"未知工具: {name}"})}],
                    "isError": True
                }})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            log(f"未处理方法: {method}")


if __name__ == "__main__":
    main()
