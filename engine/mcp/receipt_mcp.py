#!/usr/bin/env python3
"""CashLens 票据识别 MCP 服务器（采集模块）
工具：recognize_receipt —— 图片/PDF（发票/小票/消费截图）→ 结构化交易
协议：stdio JSON-RPC（纯标准库，无第三方依赖）

两条采集通道（2026-09-24 加）：
  1. 本机 PDF 文本层：数电发票 PDF 自带版式文本层，用 macOS Quartz/PDFKit 抽取，
     local_only=True 时全程本机解析，图像不出本机。同时充当云端结果的号码校验基准。
  2. 云端 VLM 识图：通用，覆盖拍照/截图；图像经云端 LLM 视觉 API，
     返回体带 _cloud_uploaded 标记，调用方须向用户明示。
"""
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.request
from pathlib import Path

# macOS 原生 PDF 文本层抽取（Quartz / PDFKit 随系统提供，非第三方 pip 依赖；非 macOS 自动降级）
try:
    from Foundation import NSURL as _NSURL
    from Quartz import PDFDocument as _PDFDocument

    _PDF_TEXT_AVAILABLE = True
except Exception:  # noqa: BLE001 非 macOS 或缺少 PyObjC 桥，降级为纯云端通道
    _PDF_TEXT_AVAILABLE = False

# ─── 模型档位：仅在未配置档位时用环境变量回退 ───────────
_DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
_DEFAULT_MODEL = os.environ.get("LLM_MODEL", "doubao-seed-2-0-lite-260428")
_DEFAULT_API_KEY = os.environ.get("LLM_API_KEY", "")

# 档位配置文件：与 backend/app/services/model_config.py 同一份，格式同源。
# 票据识别跑在独立 MCP 进程里，不 import 后端包，所以直接读文件。
_DATA_DIR = Path(os.environ.get("CASH_DATA_DIR") or
                 (Path(__file__).resolve().parents[2] / "data"))
_MODEL_CONFIG_PATH = _DATA_DIR / "model_config.json"


def load_runtime_config() -> dict:
    """读当前模型档位配置。

    tier = cloud（云端，数据出本机）/ local（本地模型，数据不出本机）/ none（不用模型）。
    返回 {tier, base_url, model, api_key, vision, usable}。
    """
    tier = "cloud"
    base_url = _DEFAULT_BASE_URL
    model = _DEFAULT_MODEL
    key = _DEFAULT_API_KEY
    vision = True
    try:
        if _MODEL_CONFIG_PATH.exists():
            cfg = json.loads(_MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and cfg.get("tier") in ("cloud", "local", "none"):
                tier = cfg["tier"]
                slot = cfg.get(tier) if tier in ("cloud", "local") else None
                if isinstance(slot, dict):
                    base_url = str(slot.get("base_url") or base_url)
                    model = str(slot.get("model") or model)
                    key = str(slot.get("api_key") or key)
                    vision = bool(slot.get("declared_vision", True))
    except (OSError, json.JSONDecodeError):
        pass  # 配置坏了就按默认云端走，不因配置问题让识别整体失败

    if tier == "none":
        return {"tier": "none", "base_url": "", "model": "", "api_key": "",
                "vision": False, "usable": True}
    # 本地端点通常不需要密钥（Ollama 等），云端必须有
    usable = bool(base_url and model and (key or tier == "local"))
    return {"tier": tier, "base_url": base_url.rstrip("/"), "model": model,
            "api_key": key, "vision": vision, "usable": usable}

# 数电票（全电发票）号码：20 位纯数字
INVOICE_NO_LEN = 20
# 行政区划代码前两位（取号码第 3 至 4 位）。只作软校验提示，不作为拒绝依据
_REGION_PREFIX = {
    "11", "12", "13", "14", "15",
    "21", "22", "23",
    "31", "32", "33", "34", "35", "36", "37",
    "41", "42", "43", "44", "45", "46",
    "50", "51", "52", "53", "54",
    "61", "62", "63", "64", "65",
    "71", "81", "82",
}

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
  "text_lines": ["图里的文字，一行一条"],
  "confidence": 0-1,
  "evidence": "recognition"
}

规则：
1. 金额从票据中提取，保留两位小数；无法识别金额时 amount 填 0 并 confidence 低于 0.3
2. 看不出是支出还是收入时默认 expense
3. 图片内容模糊或不是票据时，confidence 填 0，并在 note 里说明"无法识别"
4. merchant 填开票方（销售方）。发票上左右并排出现两个公司名时，取销售方，不要取购买方
5. 发票号码是 20 位纯数字：先从左到右逐位念一遍，再写入 invoice_no；位数不对就留空并在 note 里说明
6. **如果不是发票/小票，而是聊天记录、转账截图、账单列表这类**（典型：微信群里"打车 186.79+闪送 110=296.79"这种报账对话）：
   - amount 保持 0、items 留空、confidence 填 0，note 写"不是票据，是聊天/文字截图"
   - 把图里的文字**逐行原样**抄进 text_lines（一行一条，不要合并、不要改写、不要翻译）
   - **不要替用户挑金额、不要算合计、不要判断哪笔该报** —— 只抄原文，选哪笔由用户决定
7. 如果图是发票/小票，text_lines 填空数组 []
8. 只输出 JSON"""


def log(msg: str):
    sys.stderr.write(f"[receipt-mcp] {msg}\n")
    sys.stderr.flush()


def send(msg: dict):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ─── 本机 PDF 文本层 ────────────────────────────────────
def extract_pdf_text_layer(pdf_path: str) -> str | None:
    """用 macOS Quartz/PDFKit 抽 PDF 版式文本层。非 macOS 或抽取失败返回 None。

    注意：文本流的先后顺序**不等于**视觉上的左右分栏，因此本函数只当"字符来源"用，
    不做字段归属判断（归属交给 VLM 或人工）。
    """
    if not _PDF_TEXT_AVAILABLE:
        return None
    try:
        doc = _PDFDocument.alloc().initWithURL_(_NSURL.fileURLWithPath_(pdf_path))
        if doc is None:
            return None
        text = doc.string() or ""
        return text if text.strip() else None
    except Exception as e:  # noqa: BLE001
        log(f"PDF 文本层抽取失败：{type(e).__name__}: {e}")
        return None


def _twenty_digit_candidates(text: str) -> list[str]:
    """抽出文本层里所有 20 位连续数字串（去重、保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for n in re.findall(r"(?<!\d)\d{20}(?!\d)", text or ""):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _structurally_plausible(nums: list[str], date: str = "") -> list[str]:
    """从 20 位候选里筛出结构上像发票号码的（区划代码在册、年份与开票日期一致）。

    用于排除同为 20 位的其他数字串，例如销方银行账号（其第 3 至 4 位为 05，
    不在行政区划代码白名单内）。此处不写任何真实票面数字，参见 tests 里的合成样例。
    """
    out: list[str] = []
    for n in nums:
        if n[2:4] not in _REGION_PREFIX:
            continue
        # 号码前两位应与开票年份后两位一致（如 2026 年发票以 26 开头）
        if len(date) >= 4 and date[:2].isdigit() and n[:2] != date[2:4]:
            continue
        out.append(n)
    return out


# ─── 发票号码校验与双源交叉核对 ─────────────────────────
def validate_invoice_no(no: str) -> dict:
    """数电票号码校验：位数是硬校验（不匹配即不得自动入账），年份与区划是软校验（只提示）。"""
    digits = re.sub(r"\D", "", no or "")
    if not digits:
        return {"ok": False, "no": "", "reason": "号码为空", "warnings": []}
    if len(digits) != INVOICE_NO_LEN:
        return {"ok": False, "no": digits, "warnings": [],
                "reason": f"位数不符：读到 {len(digits)} 位，应为 {INVOICE_NO_LEN} 位"}
    warnings: list[str] = []
    if digits[2:4] not in _REGION_PREFIX:
        warnings.append(f"第 3 至 4 位 {digits[2:4]} 不在行政区划代码白名单内，建议人工核对")
    return {"ok": True, "no": digits, "warnings": warnings,
            "reason": f"格式校验通过（{INVOICE_NO_LEN} 位数字）"}


def _one_zero_inserted(short: str, long_: str) -> bool:
    """判断 long_ 是否恰为 short 中间插入一个 '0' 得到（长数字串被压缩一位的典型形态）。"""
    if len(long_) != len(short) + 1:
        return False
    for k, ch in enumerate(long_):
        if ch == "0" and long_[:k] + long_[k + 1:] == short:
            return True
    return False


def crosscheck_invoice_no(vlm_no: str, local_text: str) -> dict:
    """用本机 PDF 版式文本层交叉核对发票号码。

    只在「文本层恰有一个 20 位候选，且它等于 VLM 结果中间插入一个 0」时才采纳。
    这是有独立证据源的修正，不是补位猜测；候选不唯一或形态不符一律不采纳。
    """
    short = re.sub(r"\D", "", vlm_no or "")
    if len(short) != INVOICE_NO_LEN - 1:
        return {"ok": False, "no": "",
                "reason": f"VLM 读到的号码 {len(short)} 位，不满足差一位的核对前提"}
    hits = [n for n in _twenty_digit_candidates(local_text) if _one_zero_inserted(short, n)]
    if len(hits) == 1:
        return {"ok": True, "no": hits[0],
                "reason": f"本机 PDF 文本层核对通过：VLM 少读 1 个 0，已按文本层修正（原值 {short}）"}
    if not hits:
        return {"ok": False, "no": "", "reason": "文本层中找不到与 VLM 结果差一位的 20 位号码"}
    return {"ok": False, "no": "",
            "reason": f"文本层有 {len(hits)} 个候选同时满足差一位，无法判定，交人工"}


def _apply_invoice_checks(result: dict, local_text: str | None) -> None:
    """就地为识别结果打号码校验标记：能证实的证实，证不了的交人工，绝不补位猜位。"""
    raw_no = result.get("invoice_no") or ""
    check = validate_invoice_no(raw_no)
    warnings = list(check.get("warnings") or [])

    date = str(result.get("date") or "")
    if check["ok"] and len(date) >= 4 and date[:2].isdigit() and check["no"][:2] != date[2:4]:
        warnings.append(f"号码前两位 {check['no'][:2]} 与开票日期年份 {date[:4]} 不符，建议人工核对")

    if check["ok"]:
        result["invoice_no"] = check["no"]
        result["invoice_no_check"] = {"ok": True, "reason": check["reason"],
                                      "source": "vlm_self", "warnings": warnings}
        return

    # 位数不符：用本机 PDF 文本层做双源交叉核对（有独立证据，不算猜）
    if local_text:
        cross = crosscheck_invoice_no(check["no"] or raw_no, local_text)
        if cross["ok"]:
            result["invoice_no"] = cross["no"]
            result["_corrected_from"] = raw_no
            result["invoice_no_check"] = {"ok": True, "reason": cross["reason"],
                                          "source": "pdf_text_layer_crosscheck",
                                          "warnings": warnings}
            return

    # 修不了：如实标记，转人工确认
    result["invoice_no"] = check["no"] or raw_no
    result["invoice_no_check"] = {"ok": False, "reason": check["reason"],
                                  "source": "vlm_self", "warnings": warnings}
    result["needs_human"] = sorted(set(result.get("needs_human") or []) | {"invoice_no"})
    prev = (result.get("note") or "").strip()
    tip = f"发票号码存疑（{check['reason']}），请对照原件人工核对后再入账"
    result["note"] = f"{prev}；{tip}" if prev else tip


def _item_names(text: str) -> list[str]:
    """数电票的项目名称写作「*大类*具体名目」（如「*生产生活服务*住宿费」）。

    ⚠️ 这是**自包含的一个 token**，不依赖左右分栏 —— 所以"文本流顺序不可靠"
    这条不适用于它：判分类只需要这个名目本身，不需要知道它在版面上的位置。
    （2026-09-28 修：此前把分类和商户一起当成"分栏不可靠"留空，导致一张票面
      明写着「*生产生活服务*住宿费」、还带着 20 位发票号码的住宿发票被判成「待确认」。
      读票面是引擎层的事，判分类是后端 `categories.py` 的事，两者不该一起放弃。）
    """
    return re.findall(r"\*[^*\n]{1,14}\*([^\s¥\d*]{1,20})", text or "")


def _merchant_candidates(text: str) -> list[str]:
    """文本层里"像主体名称"的 token（公司 / 酒店 / 宾馆 / 民宿 / 客栈 / 店 …）。

    只作**候选**返回，不认定谁是卖方：买方与卖方的名字可能挤在同一行、
    顺序不等于视觉分栏。由后端用"分类能否对上"做交叉核对后再定（见 intake 的
    `_merchant_by_category`），对不上就留空交人工。
    """
    out: list[str] = []
    for w in re.split(r"[\s;:：,，、]+", text or ""):
        if len(w) >= 4 and re.search(r"(公司|酒店|宾馆|旅馆|民宿|客栈|商店|店|中心)$", w) and w not in out:
            out.append(w)
    return out


def parse_from_text_layer(text: str, source_path: str) -> dict:
    """从 PDF 版式文本层本机解析（隐私模式：图像不出本机）。

    抽文本层里位置确定的：发票号码、开票日期、价税合计，外加票面项目名目
    （`items`，判分类用）与主体名称候选（`merchant_candidates`，供后端交叉核对）。
    买卖方**顺序不可靠**（文本流顺序不等于视觉左右分栏），所以本函数不认定
    谁是卖方 —— 它只把读到的原文交出去，由后端判。
    """
    nums = _twenty_digit_candidates(text)

    date = ""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    amount = 0.0
    m = re.search(r"价税合计[\s\S]{0,120}?¥\s*([\d,]+\.\d{2})", text)
    if m:
        amount = float(m.group(1).replace(",", ""))

    # 20 位数字串不止一个（销方银行账号也是 20 位），先按票号结构筛，再要求唯一
    plausible = _structurally_plausible(nums, date)
    invoice_no = plausible[0] if len(plausible) == 1 else ""

    items = _item_names(text)
    candidates = _merchant_candidates(text)

    check = validate_invoice_no(invoice_no)
    needs_human = ["merchant"]
    if not items:
        needs_human.append("category")
    if len(plausible) != 1:
        needs_human.append("invoice_no")
        check = {"ok": False, "no": invoice_no, "warnings": [],
                 "reason": (f"文本层中结构合理的号码候选有 {len(plausible)} 个"
                            f"（原始 20 位候选 {len(nums)} 个），无法判定")}

    # 提示语按"实际读到了什么"写，不写死：读到了就别吓唬用户说要人工确认
    missing = []
    if len(plausible) != 1:
        missing.append("发票号码")
    if not candidates:
        missing.append("商户")
    if not items:
        missing.append("分类")
    note = "本机 PDF 文本层解析（图像未出本机）"
    note += ("：" + "、".join(missing) + "需人工确认") if missing else \
            ("：已读到票面项目 " + "、".join(items[:3]))

    return {
        "type": "expense",
        "amount": amount,
        "date": date,
        "merchant": "",
        "category": "",
        "invoice_no": invoice_no,
        "items": items,
        "merchant_candidates": candidates,
        "note": note,
        "confidence": 0.9 if (invoice_no and amount and date) else 0.4,
        "evidence": "recognition",
        "_raw_image": source_path,
        "_cloud_uploaded": False,
        "_local_text_layer": True,
        "needs_human": sorted(set(needs_human)),
        "invoice_no_check": {"ok": check["ok"], "reason": check["reason"],
                             "source": "pdf_text_layer",
                             "warnings": list(check.get("warnings") or [])},
    }


# ─── 核心：图片/PDF → 结构化交易 ────────────────────────
def _extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象。

    容忍两件实测会遇到的事（2026-09-24）：
      1. markdown 围栏包裹
      2. JSON 后面还跟了一句解释（原实现直接 json.loads 会抛 Extra data 崩掉）
    """
    s = str(text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        return json.loads(s[i:j + 1])
    raise ValueError("模型输出里找不到 JSON 对象")


def _pdf_text_fallback(local_text: str | None, image_path: str, why: str) -> dict | None:
    """模型通道失败时的兜底：带文本层的 PDF 改用本机文本层解析。

    起因（2026-09-24 实测）：本地模型档下把 PDF 当图片发给 Ollama，返回
    `invalid image input`，整张数电发票读不出来。数电发票自带版式文本层，
    本机直读即可，不该因为模型通道不支持 PDF 就整体失败。
    只对带文本层的 PDF 生效，其它情况返回 None（保持原来的报错行为）。
    """
    if not local_text:
        return None
    out = parse_from_text_layer(local_text, image_path)
    note = str(out.get("note") or "").strip()
    out["note"] = (note + f"（模型通道失败，已回退本机文本层：{why[:80]}）").strip()
    out["_fallback"] = "pdf_text_layer"
    return out


def recognize_receipt(image_path: str, local_only: bool = False) -> dict:
    """读图片或 PDF → 结构化交易 JSON。

    档位决定数据流向：
      none  → 不调用任何模型，PDF 走本机文本层，拍照与截图明确拒绝
      local → 调本机端点，数据不出本机
      cloud → 调云端端点，图像出本机（返回体带 _cloud_uploaded 标记供调用方明示用户）
    local_only=True 时强制走本机文本层，忽略档位。
    无论哪条路径，发票号码都要过格式硬校验，有 PDF 文本层时再做双源交叉核对。
    """
    if not os.path.exists(image_path):
        return {"error": f"图片不存在: {image_path}"}

    cfg = load_runtime_config()

    # 本机文本层（仅 PDF 可得）：本机解析的数据源，也是模型结果的校验基准
    is_pdf = image_path.lower().endswith(".pdf")
    local_text = extract_pdf_text_layer(image_path) if is_pdf else None

    # 带文本层的 PDF：本机直读，**不调模型**。
    # 起因（2026-09-28 真机）：一张数电票文本层里明明写着「*生产生活服务*住宿费」和
    # 20 位发票号码，却被判成「待确认」—— 因为代码先把 PDF 当 base64 image 发了出去，
    # 被 API 以 `Invalid base64 image` 拒掉，才回退本机。代价是白等一次失败调用、
    # note 里留一句吓人的报错，而且与合规文件写的「数电发票 PDF 全程本机解析」不符
    # （字节其实已经上传过、只是被拒了）。数电票自带版式文本层，本机直读更快也更准，
    # 没有任何理由先走模型通道 —— 模型通道读 PDF 本来就不成立（local/cloud 两边都拒）。
    if local_text:
        return parse_from_text_layer(local_text, image_path)

    if local_only or cfg["tier"] == "none":
        why = "当前档位是「不用模型」" if cfg["tier"] == "none" else "本机解析模式"
        return {"error": f"{why}：需要带文本层的 PDF；拍照或截图请改用云端或本地模型档位。"}

    if not cfg["usable"]:
        return {"error": f"当前档位「{cfg['tier']}」配置不完整："
                         f"请补全端点地址、模型名或密钥后再试"}

    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": RECOGNIZE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            ]
        }],
        "max_tokens": 2000,
        # 固定采样温度：票据识别要的是确定性，不是创造力。
        # 实测（2026-09-24）同一张图两次跑出不同结果，就是没定这个值导致的。
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{cfg['base_url']}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key'] or 'local'}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        result = _extract_json(content)
        result["_raw_image"] = image_path
        result["_model_tier"] = cfg["tier"]
        # 只有云端档数据才出本机；本地模型档不上传
        result["_cloud_uploaded"] = (cfg["tier"] == "cloud")
        result["_local_text_layer"] = bool(local_text)
        _apply_invoice_checks(result, local_text)
        return result
    except urllib.error.HTTPError as e:
        detail = f"API HTTP {e.code}: {e.read().decode()[:200]}"
        return _pdf_text_fallback(local_text, image_path, detail) or {"error": detail}
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:200]}"
        return _pdf_text_fallback(local_text, image_path, detail) or {"error": detail}


# ─── MCP stdio 协议 ─────────────────────────────────────
TOOL_DEF = {
    "name": "recognize_receipt",
    "description": "识别发票/小票/消费截图的图片或 PDF，提取结构化交易信息（金额/日期/商户/分类/发票号码）。当用户说「识别这张发票」「拍个票据记账」「帮我看看这个收据」或提供图片、PDF 路径时调用。",
    "inputSchema": {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "票据图片或 PDF 的本地文件路径"},
            "local_only": {"type": "boolean",
                           "description": "仅本机解析、图像不上传云端（输入须为带文本层的 PDF）；商户与分类会留空待人工确认"}
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
                "serverInfo": {"name": "receipt-mcp", "version": "0.2.0"}
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
                result = recognize_receipt(args.get("image_path", ""),
                                           bool(args.get("local_only", False)))
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
