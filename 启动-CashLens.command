#!/bin/bash
# ===========================================================================
#  CashLens 一键启动（macOS 双击版）
#
#  用法：在 Finder 里双击本文件即可（不需要打开终端敲命令）
#  它会做四件事：
#    1. 自检 python3 / pnpm / 后端依赖 / 前端依赖（缺了会提示或自动安装）
#    2. 同时启动后端 API（默认 8001）与前端工作台（默认 3000）
#    3. 等后端与前端就绪后，自动用浏览器打开工作台
#    4. 把两个服务的日志写进 09-代码/.logs/ 目录
#
#  已经有一个实例在跑时：不会重复启动，直接复用它并打开浏览器
#  （Next.js 的 dev server 同一目录只允许一个实例，重复启动会被它拒绝）
#
#  停止：在本窗口按 Control + C，或者直接关闭本窗口
#  端口：可用环境变量覆盖，例如 CASH_PORT=8101 CASH_WEB_PORT=3200
#  调试：设 CASH_NO_OPEN=1 可以不自动打开浏览器
# ===========================================================================

set -u

cd "$(dirname "$0")" || { echo "无法进入脚本所在目录"; read -r -p "按回车键关闭..."; exit 1; }
ROOT="$(pwd)"

BACK_PORT="${CASH_PORT:-8001}"
WEB_PORT="${CASH_WEB_PORT:-3000}"
LOG_DIR="$ROOT/.logs"
BACK_LOG="$LOG_DIR/backend.log"
FRONT_LOG="$LOG_DIR/frontend.log"
BACK_PID=""
FRONT_PID=""

# 退出时只停掉「本次由本脚本启动」的进程，复用的已有实例不动
cleanup() {
  if [ -n "$BACK_PID" ] || [ -n "$FRONT_PID" ]; then
    echo ""
    echo "正在停止本次启动的服务..."
    [ -n "$BACK_PID" ] && kill "$BACK_PID" 2>/dev/null
    [ -n "$FRONT_PID" ] && kill "$FRONT_PID" 2>/dev/null
    wait 2>/dev/null
    echo "已停止。"
  fi
}
trap cleanup EXIT INT TERM HUP

pause_exit() {
  echo ""
  read -r -p "按回车键关闭本窗口..." _
  exit 1
}

port_busy() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

echo ""
echo "============================================================"
echo "  CashLens 一键启动（macOS）"
echo "  后端 API     http://127.0.0.1:$BACK_PORT"
echo "  前端工作台   http://localhost:$WEB_PORT"
echo "============================================================"
echo ""

# ---------- 1. 查找可用的 Python 3 ----------
PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
fi
if [ -z "$PY" ]; then
  echo "[错误] 没有找到 python3。"
  echo "       请先安装 Python 3：https://www.python.org/downloads/"
  pause_exit
fi
echo "[OK] Python：$($PY --version 2>&1)"

# ---------- 2. 检查后端依赖 ----------
if ! "$PY" -c "import fastapi, uvicorn, pydantic" >/dev/null 2>&1; then
  echo "[错误] 缺少后端依赖 fastapi / uvicorn / pydantic。"
  echo "       请先执行下面这一行，然后重新双击本文件："
  echo ""
  echo "           $PY -m pip install fastapi uvicorn pydantic"
  echo ""
  pause_exit
fi
echo "[OK] 后端依赖齐备"

# ---------- 3. 检查 pnpm ----------
if ! command -v pnpm >/dev/null 2>&1; then
  echo "[错误] 没有找到 pnpm。"
  echo "       请先安装 Node.js 20 或更新版本（https://nodejs.org），"
  echo "       再执行：npm install -g pnpm"
  pause_exit
fi
echo "[OK] 前端包管理器 pnpm 就绪"

# ---------- 4. 首次运行自动安装前端依赖 ----------
NEXT_BIN="$ROOT/frontend/node_modules/.bin/next"
if [ ! -x "$NEXT_BIN" ]; then
  echo ""
  echo "[提示] 未检测到前端依赖，正在执行 pnpm install（首次约需 1 至 3 分钟）..."
  ( cd "$ROOT/frontend" && pnpm install ) || {
    echo "[错误] pnpm install 失败，请检查网络后重试。"
    pause_exit
  }
fi
if [ ! -x "$NEXT_BIN" ]; then
  echo "[错误] 前端依赖仍不完整（缺少 frontend/node_modules/.bin/next）。"
  echo "       请手动进入 frontend 目录执行 pnpm install 后再试。"
  pause_exit
fi
echo "[OK] 前端依赖就绪"

# ---------- 5. 首次运行自动生成 .env ----------
if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "[提示] 已由 .env.example 生成 .env；未填 LLM Key 时对话走本地规则兜底"
fi

# ---------- 6. 启动后端（端口已有服务则复用）----------
mkdir -p "$LOG_DIR"
export CASH_API="http://127.0.0.1:$BACK_PORT"
BACK_REUSE=""

echo ""
if port_busy "$BACK_PORT"; then
  BACK_REUSE="yes"
  echo "[1/2] 端口 $BACK_PORT 已经有服务在跑，直接复用它（不再启动新的后端）"
else
  echo "[1/2] 正在启动后端 API，日志：$BACK_LOG"
  ( cd "$ROOT/backend" && exec "$PY" -m uvicorn app.main:app --port "$BACK_PORT" ) >"$BACK_LOG" 2>&1 &
  BACK_PID=$!
fi

# ---------- 7. 启动前端（端口已有服务则复用）----------
WEB_REUSE=""
if port_busy "$WEB_PORT"; then
  WEB_REUSE="yes"
  echo "[2/2] 端口 $WEB_PORT 已经有服务在跑，直接复用它（不再启动新的前端）"
else
  echo "[2/2] 正在启动前端工作台，日志：$FRONT_LOG"
  ( cd "$ROOT/frontend" && exec "$NEXT_BIN" dev --port "$WEB_PORT" ) >"$FRONT_LOG" 2>&1 &
  FRONT_PID=$!
fi

# ---------- 8. 等后端就绪 ----------
OPEN_URL="http://localhost:$WEB_PORT"
echo ""
echo "等待后端就绪（最多 30 秒）..."
READY=""
for _ in $(seq 1 30); do
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$BACK_PORT/api/health"; then READY="yes"; break; fi
  sleep 1
done
if [ -n "$READY" ]; then
  echo "[OK] 后端已就绪"
else
  echo "[提示] 后端 30 秒内没有响应，请看日志：$BACK_LOG"
fi

# ---------- 9. 等前端就绪 ----------
WEB_READY=""
if [ -n "$WEB_REUSE" ]; then
  WEB_READY="yes"
else
  echo "等待前端编译完成（最多 60 秒）..."
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null -m 3 "http://127.0.0.1:$WEB_PORT"; then WEB_READY="yes"; break; fi
    if grep -q "already running" "$FRONT_LOG" 2>/dev/null; then break; fi
    sleep 1
  done
fi

# 前端起不来时，若发现是 Next 的单实例机制挡下的，就复用已有实例
if [ -z "$WEB_READY" ] && grep -q "already running" "$FRONT_LOG" 2>/dev/null; then
  EXISTING="$(grep -o 'http://localhost:[0-9]*' "$FRONT_LOG" | tail -1)"
  [ -n "$EXISTING" ] && OPEN_URL="$EXISTING"
  WEB_READY="yes"
  echo ""
  echo "[提示] 检测到 CashLens 工作台已经有一个实例在运行（Next.js 同一目录只允许一个实例），"
  echo "       本次不再启动新的前端，直接复用已有实例：$OPEN_URL"
  echo "       想重启它：先到跑着它的那个终端窗口按 Control + C。"
fi

if [ -n "$WEB_READY" ]; then
  echo "[OK] 前端可用：$OPEN_URL"
  if [ "${CASH_NO_OPEN:-0}" != "1" ]; then
    open "$OPEN_URL"
    echo "已用默认浏览器打开工作台。"
  fi
else
  echo "[提示] 前端 60 秒内没有响应，请看日志：$FRONT_LOG"
fi

# ---------- 10. 收尾 ----------
echo ""
echo "============================================================"
if [ -n "$BACK_PID" ] || [ -n "$FRONT_PID" ]; then
  echo "  本次启动的服务："
  [ -n "$BACK_PID" ] && echo "    后端 API     http://127.0.0.1:$BACK_PORT/api/health"
  [ -n "$FRONT_PID" ] && echo "    前端工作台   $OPEN_URL"
  echo ""
  echo "  看日志：  tail -f $BACK_LOG"
  echo "            tail -f $FRONT_LOG"
  echo "  停止服务：在本窗口按 Control + C，或直接关闭本窗口"
else
  echo "  两个服务都已在别处运行，本次只做了复用与打开浏览器，没有启动新进程。"
fi
echo "============================================================"
echo ""

if [ -z "$BACK_PID" ] && [ -z "$FRONT_PID" ]; then
  read -r -p "按回车键关闭本窗口..." _
  exit 0
fi

wait
