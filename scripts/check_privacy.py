#!/usr/bin/env python3
"""提交前隐私闸：拦住「真实客户数据被提交进仓库」。

## 为什么需要（2026-10-13 立）

本轮之前，真实客户公司名 / 项目名 / 商户名 / 协作方姓名曾经出现在 **14 个已跟踪
文件**里，并且已经推到了公开仓库；靠 `git filter-repo` 重写 **100 个提交** 才清干净，
而且事后仍有残留（GitHub 上旧对象还能按 SHA 直访，删库才能彻底解决）。

结论：**隐私纪律必须前移到「提交之前」**——事后补救永远有窗口。

## 三条设计要点（改代码前先读，别改坏）

1. **黑名单不进仓库。** 真实名清单放在仓库之外（默认 `../.privacy/real-names.txt`），
   本脚本只带「怎么查」，不带「查什么」。清单缺失时 **fail-closed**（退出码 2）——
   宁可拦住正常提交，也不许静默放行。
2. **两类检查都要。** 黑名单命中只能拦「已知客户」；通用模式（20 位发票号、微信导出
   文件名、手机号、身份证号、中文公司名）才能拦住「下一个还没进清单的客户」。
3. **可解释、可放行。** 命中时打印 `文件:行号` 与命中原因；确属正当用途的，在该行
   行尾写 `privacy-ok` 显式放行（留痕，不静默）。

## 用法

    python3 scripts/check_privacy.py                # 查暂存区（hook 默认这么用）
    python3 scripts/check_privacy.py --all          # 查全部已跟踪文件（事后审计）
    python3 scripts/check_privacy.py --message-file .git/COMMIT_EDITMSG   # 连提交信息一起查

退出码：0 通过 · 1 命中隐私 · 2 配置异常（黑名单缺失/不可读）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# 行尾写这个标记 = 显式放行本行（要留痕，不静默）
ALLOW_MARK = "privacy-ok"

# 通用模式：拦「还没进黑名单的下一个客户」
PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("invoice", "发票号码（20 位数字）", re.compile(r"(?<!\d)\d{20}(?!\d)")),
    ("wechat_file", "微信导出文件名", re.compile(r"微信图片_\d{8}")),
    ("mobile", "手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("idcard", "身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
]

# 文件级豁免：文件头若干行内写 `privacy-guard: allow=invoice,mobile` 可跳过对应通用模式。
# 用途窄：只给「发票号解析器」这类以 20 位数字为测试主体的文件用。
# 黑名单命中**不可**被文件级豁免（那是硬拦）。
FILE_ALLOW_RE = re.compile(r"privacy-guard:\s*allow=([A-Za-z0-9_,\s]+)")


def file_allowed_rules(text: str) -> set[str]:
    head = "\n".join(text.splitlines()[:15])
    m = FILE_ALLOW_RE.search(head)
    if not m:
        return set()
    return {x.strip() for x in m.group(1).split(",") if x.strip()}

# 中文公司名单独处理，不能用简单正则 + 负向断言：
#   「某连锁便利店有限公司」这种占位符，`(?<!某)` 会从「锁」开始重新匹配从而绕过。
#   改成先抓整个公司名 token，再看 token 前几个字里有没有「某」。
COMPANY_RE = re.compile(r"[\u4e00-\u9fa5]{0,12}(?:有限公司|有限责任公司|股份有限公司)")


# 法律/统计类通用术语，不是公司名（否则「一人有限责任公司」「全国一人有限责任公司」会误报）
GENERIC_COMPANY_TOKENS = {
    "有限公司", "有限责任公司", "股份有限公司", "一人有限责任公司",
}
GENERIC_PREFIXES = ("全国", "中国", "我国", "中小微", "小微")


def find_company_like(line: str) -> str | None:
    """返回第一个「像真实公司名」的片段；占位符与通用术语放行。"""
    for m in COMPANY_RE.finditer(line):
        token = m.group(0)
        core = token
        for pre in GENERIC_PREFIXES:
            if core.startswith(pre):
                core = core[len(pre):]
                break
        if core in GENERIC_COMPANY_TOKENS:
            continue  # 术语：「有限公司」「一人有限责任公司」这类不是公司名
        if "某" in token[:3]:
            continue  # 规定占位符：某文化传播有限公司 / 某连锁便利店有限公司
        return token
    return None

# 这些类型一律不许进仓库（唯一例外：samples/ 下的合成/第三方样例）
BLOCK_EXT = {
    ".pdf", ".ofd", ".docx", ".xlsx", ".xls", ".csv",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".mp4", ".mov", ".heic",
}
ALLOW_PATH_PREFIX = ("samples/",)

# 内容免扫目录：第三方**公开**样例（出处与「不含真实用户数据」声明见该目录 SOURCE.md）。
# 窄到具体目录、不整包放行 samples/ —— 免得以后有人把真数据丢进 samples/ 也一路通过。
CONTENT_EXEMPT_PREFIX = ("samples/bill_format_external/",)

DEFAULT_DENYLIST = "../.privacy/real-names.txt"
ENV_DENYLIST = "CASHLENS_PRIVACY_DENYLIST"


# ─────────────────────────── 黑名单 ───────────────────────────
def compile_denylist(path: Path) -> list[tuple[str, re.Pattern[str]]]:
    """把清单编译成 (显示名, 正则)。中文按子串、含字母数字的按整词、re: 原样。"""
    rules: list[tuple[str, re.Pattern[str]]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            rules.append((line, re.compile(line[3:])))
        elif re.fullmatch(r"[A-Za-z0-9_\-]+", line):
            # 纯字母数字：整词匹配，避免三位项目代号命中更长的日期 / 编号串
            rules.append((line, re.compile(rf"(?<![A-Za-z0-9]){re.escape(line)}(?![A-Za-z0-9])", re.I)))
        else:
            rules.append((line, re.compile(re.escape(line))))
    return rules


def load_denylist(explicit: str | None) -> list[tuple[str, re.Pattern[str]]]:
    raw = explicit or os.environ.get(ENV_DENYLIST) or DEFAULT_DENYLIST
    path = Path(raw)
    if not path.is_absolute():
        # 相对路径按「仓库根」解析，保证从任何目录调用都一致
        path = git_root() / path
    if not path.is_file():
        print(
            f"✗ 隐私闸配置异常：黑名单不存在 → {path}\n"
            f"  这是 fail-closed 设计（宁可拦正常提交，也不静默放行）。\n"
            f"  修法：确认 .privacy/real-names.txt 在仓库的上一级，"
            f"或用 --denylist / 环境变量 {ENV_DENYLIST} 指定路径。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return compile_denylist(path)


# ─────────────────────────── git 辅助 ───────────────────────────
def git_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if out.returncode != 0:
        print("✗ 隐私闸：当前目录不在 git 仓库里", file=sys.stderr)
        raise SystemExit(2)
    return Path(out.stdout.strip())


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True,
    )
    return [p for p in out.stdout.splitlines() if p.strip()]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return [p for p in out.stdout.splitlines() if p.strip()]


# ─────────────────────────── 扫描 ───────────────────────────
def path_blocked(rel: str) -> str | None:
    """按路径/后缀判断是否禁入。返回原因或 None。"""
    if any(rel.startswith(p) for p in ALLOW_PATH_PREFIX):
        return None
    ext = Path(rel).suffix.lower()
    if ext in BLOCK_EXT:
        return f"文件类型 {ext} 一律不入库（合成/第三方样例请放 samples/）"
    return None


def scan_text(rel: str, text: str, rules: list[tuple[str, re.Pattern[str]]],
              allowed: set[str] | None = None) -> list[tuple[str, int, str, str]]:
    """返回 [(文件, 行号, 来源说明, 命中内容)]。allowed 为文件级豁免的通用模式标识。"""
    allowed = allowed or set()
    hits: list[tuple[str, int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARK in line:
            continue
        for label, pat in rules:
            m = pat.search(line)
            if m:
                hits.append((rel, lineno, f"黑名单·{label}", m.group(0)))
        for key, label, pat in PATTERNS:
            if key in allowed:
                continue
            m = pat.search(line)
            if m:
                hits.append((rel, lineno, f"通用·{label}", m.group(0)))
        company = find_company_like(line)
        if company:
            hits.append((rel, lineno, "通用·中文公司名", company))
    return hits


def scan_file(rel: str, rules) -> list[tuple[str, int, str, str]]:
    root = git_root()
    path = root / rel
    if not path.exists():
        return []  # 已删除的文件不用查
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []  # 二进制文件由 path_blocked 负责
    return scan_text(rel, text, rules, file_allowed_rules(text))


# ─────────────────────────── 主流程 ───────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提交前隐私闸")
    ap.add_argument("--all", action="store_true", help="查全部已跟踪文件（事后审计）")
    ap.add_argument("--denylist", help=f"黑名单路径（默认 {DEFAULT_DENYLIST}）")
    ap.add_argument("--message-file", help="同时检查提交信息文件（如 .git/COMMIT_EDITMSG）")
    args = ap.parse_args(argv)

    rules = load_denylist(args.denylist)

    files = tracked_files() if args.all else staged_files()
    if not files and not args.message_file:
        return 0  # 没有待提交内容

    hits: list[tuple[str, int, str, str]] = []
    for rel in files:
        reason = path_blocked(rel)
        if reason:
            hits.append((rel, 0, "路径/类型", reason))
            continue
        if rel.startswith(CONTENT_EXEMPT_PREFIX):
            continue  # 第三方公开样例，内容不扫（见 CONTENT_EXEMPT_PREFIX 注释）
        hits.extend(scan_file(rel, rules))

    if args.message_file:
        mf = Path(args.message_file)
        if mf.is_file():
            hits.extend(
                (args.message_file, ln, src, got)
                for (_f, ln, src, got) in scan_text(
                    "<提交信息>", mf.read_text(encoding="utf-8", errors="replace"), rules
                )
            )

    if not hits:
        return 0

    print(f"\n✗ 隐私闸拦下 {len(hits)} 处，提交已中止\n")
    for rel, lineno, src, got in hits[:40]:
        where = f"{rel}:{lineno}" if lineno else rel
        print(f"  {where:<52} [{src}]  {got}")
    if len(hits) > 40:
        print(f"  … 另有 {len(hits) - 40} 处")

    print(
        "\n怎么修（任选其一）：\n"
        "  1) 换成中性占位符：示例项目 A / 示例项目B / 某文化传播有限公司 / 微信图片_示例\n"
        "  2) 确属正当用途：在该行行尾写 privacy-ok 并说明原因（要留痕，不静默放行）\n"
        "  3) 客户真材料不要进仓库：放 data/（已被忽略）或仓库外的隔离数据目录 CASH_DATA_DIR\n"
        "  4) 合成数据放 samples/（该目录允许二进制与 csv）\n"
        "\n注：`git commit --no-verify` 可以绕过本闸——绕过即视为主动放弃这条纪律。\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
