#!/usr/bin/env python3
"""CashLens 最小 MCP 服务器：查询现金流状态（实测 2 用）"""
import json
import sys

# 手写最小 stdio JSON-RPC MCP 服务器（不依赖 SDK，纯标准库）
# 协议：initialize 握手 → notifications/initialized → tools/list → tools/call


def log(msg):
    sys.stderr.write(f"[mcp-cashflow] {msg}\n")
    sys.stderr.flush()


def send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


TOOL_DEF = {
    "name": "get_cashflow_status",
    "description": "查询当前现金流状态：健康度(0-1)、现金流可信度(0-1)、未来30天缺口。当用户问「现金流怎么样」「下个月会不会缺钱」「状态如何」时调用。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "as_of": {"type": "string", "description": "查询时点，默认 now"}
        },
        "required": []
    }
}


def tool_get_cashflow_status(args: dict) -> dict:
    """返回模拟的状态引擎数据（实测阶段用静态数据）"""
    return {
        "health": 0.72,
        "confidence": 0.65,
        "labels": ["learning"],
        "gap_30d": {"exists": False, "amount": 0.0},
        "cashflow_30d": {"min_balance": 3200.50, "expected_income": 15000.0, "expected_expense": 11200.0},
        "as_of": args.get("as_of", "now"),
        "source": "mcp-demo-v1"
    }


def main():
    pending_request_id = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"无法解析: {line[:80]}")
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cashflow-mcp", "version": "0.1.0"}
                }
            })
        elif method == "notifications/initialized":
            pass  # 客户端通知初始化完成
        elif method == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": [TOOL_DEF]}
            })
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "get_cashflow_status":
                result = tool_get_cashflow_status(args)
                send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                        "isError": False
                    }
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": f"未知工具: {name}"})}],
                        "isError": True
                    }
                })
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            log(f"未处理方法: {method}")


if __name__ == "__main__":
    main()
