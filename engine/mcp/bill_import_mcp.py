#!/usr/bin/env python3
"""CashLens 账单导入 MCP 服务器（采集模块 · 通道 A + 对账去重）
工具：import_bill_csv —— 解析支付宝/微信官方导出 CSV → 统一交易模型 + 重复检测
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csv_import as ci


def log(msg: str):
    sys.stderr.write(f"[bill-mcp] {msg}\n")
    sys.stderr.flush()


def send(msg: dict):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


TOOL_DEF = {
    "name": "import_bill_csv",
    "description": "导入支付宝或微信官方导出的账单 CSV 文件，解析为结构化交易并检测跨渠道重复。当用户说「导入账单」「导支付宝账单」「导微信账单」「上传 CSV」或提供 CSV 文件路径时调用。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "支付宝/微信官方导出 CSV 的文件路径"}
        },
        "required": ["file_path"]
    }
}


def import_bill(file_path: str) -> dict:
    if not os.path.exists(file_path):
        return {"error": f"文件不存在: {file_path}"}
    result = ci.parse_csv_file(file_path)
    if result.get("error"):
        return result
    dup = ci.find_duplicates(result["txns"])
    return {
        "channel": result["channel"],
        "count": result["count"],
        "total_income": round(sum(t["amount"] for t in result["txns"] if t["type"] == "income"), 2),
        "total_expense": round(sum(t["amount"] for t in result["txns"] if t["type"] == "expense"), 2),
        "duplicate_groups": len(dup),
        "txns": result["txns"],
        "duplicates": dup
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
                "serverInfo": {"name": "bill-mcp", "version": "0.1.0"}
            }})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [TOOL_DEF]}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "import_bill_csv":
                result = import_bill(args.get("file_path", ""))
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
