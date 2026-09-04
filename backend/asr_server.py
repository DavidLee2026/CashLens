#!/usr/bin/env python3
"""CashLens 本地语音识别服务（backend 雏形 · 单文件原型）

职责：同时提供「静态页面」与「语音识别 API」，让网页 demo 走真实本地 ASR 链路：
  页面录音（16kHz mono WAV）→ POST /api/asr → 本地 Qwen3-ASR 转写 → 返回文本

路由：
  GET  /           静态页面（docs/ 目录）
  GET  /api/health 存活检查
  POST /api/asr    WAV 音频 → {ok, text}

用法（在 09-代码/ 目录下）：
  python3 backend/asr_server.py          # 默认端口 8000
  python3 backend/asr_server.py 8001     # 指定端口

说明：本文件是语音采集模块的原型，后续并入正式 FastAPI 后端（backend/app/）。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

# ─── 配置（可用环境变量覆盖，便于不同机器）───
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs"))
CLI = os.environ.get("ASR_CLI", os.path.expanduser("~/dev/transcribe.cpp/build/bin/transcribe-cli"))
MODEL = os.environ.get("ASR_MODEL", os.path.expanduser("~/models/asr/Qwen3-ASR-1.7B-Q4_K_M.gguf"))
ASR_TIMEOUT = 180  # 秒


def run_asr(wav_bytes: bytes) -> dict:
    """调本地 Qwen3-ASR：WAV(16k mono) → 识别文本"""
    if not os.path.exists(CLI):
        return {"ok": False, "text": "", "error": f"ASR CLI 不存在: {CLI}"}
    if not os.path.exists(MODEL):
        return {"ok": False, "text": "", "error": f"ASR 模型不存在: {MODEL}"}

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        with os.fdopen(fd, "wb") as f:
            f.write(wav_bytes)
        r = subprocess.run(
            [CLI, "-m", MODEL, tmp],
            capture_output=True, text=True, timeout=ASR_TIMEOUT,
        )
        # transcribe.cpp 把识别文本输出到 stdout，形如 "text: <内容>"
        text = ""
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            m = re.match(r"^text:\s*(.*)$", line)
            if m:
                text = (text + " " + m.group(1)).strip()
        if r.returncode != 0 or not text:
            tail = (r.stderr or r.stdout or "")[-300:]
            return {"ok": False, "text": "", "error": f"转录失败（rc={r.returncode}）: {tail}"}
        return {"ok": True, "text": text}
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "error": "转录超时"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "error": f"{type(e).__name__}: {e}"}
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/api/health":
            body = json.dumps({"ok": True, "asr": "qwen3-local"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path == "/api/asr":
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                self._json({"ok": False, "text": "", "error": "空请求"}, 400)
                return
            wav = self.rfile.read(length)
            resp = run_asr(wav)
            self._json(resp, 200)
            return
        self._json({"ok": False, "error": "未知路由"}, 404)

    def _json(self, obj: dict, code: int):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默请求日志
        pass


if __name__ == "__main__":
    if not os.path.exists(ROOT):
        print(f"错误：静态目录不存在 {ROOT}")
        sys.exit(1)
    print(f"CashLens ASR 服务启动: http://localhost:{PORT}/docs/  (API: /api/asr)")
    print(f"  模型: {os.path.basename(MODEL)}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
