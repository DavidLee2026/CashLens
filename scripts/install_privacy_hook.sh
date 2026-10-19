#!/usr/bin/env bash
# 安装「提交前隐私闸」到本仓库的 .git/hooks/pre-commit
#
# 为什么用安装脚本而不是直接提交 hook：
#   .git/hooks/ 不进版本库（git 自己也不跟踪它），所以换机器 / 重新 clone 后
#   hook 会消失。保留这个脚本 = 让「重新装上」成为一条命令，且装了什么可被审查。
#
# 用法：
#   bash scripts/install_privacy_hook.sh
#   python3 scripts/check_privacy.py --all      # 装完自检（查全部已跟踪文件）
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
hook="$repo_root/.git/hooks/pre-commit"

if [ ! -f "$repo_root/scripts/check_privacy.py" ]; then
  echo "✗ 找不到 scripts/check_privacy.py，请在仓库根目录执行" >&2
  exit 1
fi

mkdir -p "$(dirname "$hook")"

cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
# CashLens 提交前隐私闸
# 由 scripts/install_privacy_hook.sh 生成 —— 请不要手改这个文件，改安装脚本。
# 纪律：真实客户数据不入库（2026-10-13 立；起因见协作日志 2026-10-13 那篇）
set -euo pipefail
root=$(git rev-parse --show-toplevel)
exec python3 "$root/scripts/check_privacy.py" --message-file "$root/.git/COMMIT_EDITMSG"
HOOK

chmod +x "$hook"

echo "✓ 已安装 pre-commit 隐私闸：$hook"
echo "  自检中（扫描全部已跟踪文件）…"
if python3 "$repo_root/scripts/check_privacy.py" --all; then
  echo "✓ 自检通过：当前仓库没有命中黑名单"
else
  echo "⚠ 自检未通过：上面列出的文件需要先处理（这不影响 hook 已装好）" >&2
  exit 1
fi
