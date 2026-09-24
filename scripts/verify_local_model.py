#!/usr/bin/env python3
"""本地视觉模型实弹验证 —— 本机 Ollama 能不能认真实数电发票

背景：模型档位新增「本地模型」档（数据不出本机、零 API 成本），
但这一档必须有实测证据，否则就是纸上功能（R38：AI 自报不可信）。

方法：同一张真实数电发票（08-实测/530.pdf）
  1. 基准：本机 PDF 版式文本层（独立于任何视觉模型）
  2. 待测：渲染成 PNG 后交给本机 Ollama 的视觉模型识别
  3. 比对字段，并特别检查 20 位发票号码这个已知的稳定缺陷点

隐私：脚本内不硬编码任何真实票面数字，全部运行时从 PDF 读出；
渲染图写入临时目录，不落仓库。
⚠️ 运行输出会打印真实票面数字，仅供本地核对，请勿粘贴进公开材料。

用法：
  cd 09-代码 && python3 scripts/verify_local_model.py [模型名]
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "engine" / "mcp"))
import receipt_mcp as rc  # noqa: E402

REPO = HERE.parents[1]
PDF = REPO / "08-实测" / "530.pdf"

BASE = os.environ.get("LOCAL_LLM_BASE", "http://127.0.0.1:11434/v1")
MODEL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LOCAL_VL_MODEL", "qwen3-vl:30b-a3b")

_pass = 0
_fail = 0


def check(name: str, ok: bool, detail: str = ""):
    global _pass, _fail
    _pass += 1 if ok else 0
    _fail += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"　{detail}" if detail else ""))


def list_local_models() -> list[str]:
    """问 Ollama 要已下载的模型列表（去掉 /v1 后缀）。"""
    root = BASE[:-3] if BASE.endswith("/v1") else BASE
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def call_vision(image_path: str, timeout: int = 300) -> tuple[dict | None, float, str]:
    """调本机视觉模型识别票据。返回 (结果, 耗时秒, 错误)。"""
    import base64
    import mimetypes

    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": rc.RECOGNIZE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        f"{BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer local"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - t0
        content = data["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content), elapsed, ""
    except urllib.error.HTTPError as e:
        return None, time.time() - t0, f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
    except Exception as e:  # noqa: BLE001
        return None, time.time() - t0, f"{type(e).__name__}: {str(e)[:200]}"


def main() -> int:
    print("=" * 68)
    print(f"本地视觉模型实弹　模型={MODEL}　端点={BASE}")
    print("=" * 68)

    models = list_local_models()
    if not models:
        print("  本机 Ollama 未响应，跳过。请先启动 Ollama。")
        return 0
    print(f"  本机已装模型：{', '.join(models)}")
    if MODEL not in models:
        print(f"  ⚠️ 未找到 {MODEL}；可先用 `ollama pull {MODEL}` 下载，或传入其他模型名。")
        return 0

    if not PDF.exists():
        print(f"  找不到真实票据样本 {PDF}，跳过。")
        return 0

    text = rc.extract_pdf_text_layer(str(PDF))
    if not text:
        print("  无法抽出 PDF 文本层，无法建立基准，跳过。")
        return 0

    baseline = rc.parse_from_text_layer(text, str(PDF))
    truth_no = baseline["invoice_no"]
    truth_date = baseline["date"]
    truth_amount = baseline["amount"]
    print(f"\n  基准（PDF 本机文本层，与视觉模型无关）：")
    print(f"    发票号码 {truth_no}（{len(truth_no)} 位）")
    print(f"    开票日期 {truth_date}　价税合计 {truth_amount}")

    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "receipt.png"
        sys.path.insert(0, str(HERE))
        try:
            import render_pdf
            render_pdf.render_pdf(str(PDF), str(png), 3.0)
        except Exception as e:  # noqa: BLE001
            print(f"  渲染失败：{type(e).__name__}: {e}")
            return 1
        size_kb = png.stat().st_size / 1024
        print(f"\n  已渲染 {png.name}（{size_kb:.0f}KB，临时目录，用后即删）")
        print("  正在调用本机模型（首次加载 20GB 级模型可能需要一两分钟）…")

        result, elapsed, err = call_vision(str(png))

    if err:
        print(f"  调用失败：{err}")
        return 1

    print(f"  推理耗时：{elapsed:.1f} 秒\n")
    print("  ── 本地模型识别结果 ──")
    print(f"    type       {result.get('type')}")
    print(f"    amount     {result.get('amount')}")
    print(f"    date       {result.get('date')}")
    print(f"    merchant   {result.get('merchant')}")
    print(f"    category   {result.get('category')}")
    print(f"    invoice_no {result.get('invoice_no')}")
    print(f"    confidence {result.get('confidence')}")

    print("\n  ── 与基准比对 ──")
    got_no = str(result.get("invoice_no") or "")
    got_digits = "".join(ch for ch in got_no if ch.isdigit())
    check("开票日期正确", str(result.get("date")) == truth_date,
          f"{result.get('date')} vs {truth_date}")
    try:
        amt_ok = abs(float(result.get("amount") or 0) - float(truth_amount)) < 0.01
    except (TypeError, ValueError):
        amt_ok = False
    check("价税合计正确", amt_ok, f"{result.get('amount')} vs {truth_amount}")
    check("发票号码 20 位", len(got_digits) == 20, f"读到 {len(got_digits)} 位")
    check("发票号码与文本层一致", got_digits == truth_no)

    print("\n  ── 校验层是否兜住（这是换本地模型的关键保障）──")
    fixed = rc.crosscheck_invoice_no(got_digits, text)
    print(f"    号码校验：{rc.validate_invoice_no(got_digits)['reason']}")
    if got_digits == truth_no:
        print("    号码本身就对了，无需修正。")
    elif fixed["ok"]:
        print(f"    号码读错，但被本机文本层交叉核对修正：{fixed['reason']}")
    else:
        print(f"    号码读错且无法自动修正：{fixed['reason']}")
        print("    → 会标记 needs_human 转人工，不会带着错号码入账。")

    print()
    print("=" * 68)
    print(f"字段比对：{_pass} 通过 / {_fail} 失败　推理耗时 {elapsed:.1f}s")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
