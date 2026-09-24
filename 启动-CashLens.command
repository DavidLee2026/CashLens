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
#  端口被占用时不是无脑复用，而是先校验：
#    · 后端：必须确认是本仓库这一份（看账本路径）且版本够新（有 /api/categories），
#            否则判定为「别的副本」，自动换一个空闲端口启动本仓库后端
#    · 前端：Next.js 同一目录只允许一个实例，所以只能复用；复用前校验它的
#            代理指向的是不是本仓库后端，不一致会明确提示你关掉旧工作台
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
BACK_REUSE=""

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

# 端口上的后端能不能直接复用：必须是本仓库这一份，且带四类输入通道（新版本）
backend_reusable() {
  local port="$1" health code
  health="$(curl -s -m 3 "http://127.0.0.1:$port/api/health" 2>/dev/null)" || return 1
  case "$health" in
    *"$ROOT/data/"*) ;;
    *) return 1 ;;
  esac
  code="$(curl -s -o /dev/null -m 3 -w "%{http_code}" "http://127.0.0.1:$port/api/categories" 2>/dev/null)"
  [ "$code" = "200" ] || return 1
  return 0
}

# 前端 3000 的代理，最终打到的是不是本仓库后端
frontend_proxy_ok() {
  local port="$1" proxied
  proxied="$(curl -s -m 3 "http://127.0.0.1:$port/api/health" 2>/dev/null)" || return 1
  case "$proxied" in
    *"$ROOT/data/"*) return 0 ;;
    *) return 1 ;;
  esac
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

# ---------- 6. 后端端口：能复用就复用，是别的副本就让路 ----------
mkdir -p "$LOG_DIR"
START_BACK="yes"

if port_busy "$BACK_PORT"; then
  if backend_reusable "$BACK_PORT"; then
    BACK_REUSE="yes"
    START_BACK="no"
  else
    echo "[提示] 端口 $BACK_PORT 上跑的不是本仓库这一份后端（可能是另一个副本，"
    echo "       例如编辑器沙箱里的旧版本，它缺少四类输入通道等新能力）。"
    echo "       为它让路，本仓库后端改用下面这个端口。"
    NEW_PORT=""
    for p in 8002 8003 8004 8005 8010 8020; do
      if ! port_busy "$p"; then NEW_PORT="$p"; break; fi
    done
    if [ -z "$NEW_PORT" ]; then
      echo "[错误] 备用端口（8002 / 8003 / 8004 / 8005 / 8010 / 8020）也都被占用。"
      echo "       请先释放一个端口，或关掉占用 $BACK_PORT 的那个程序后重试。"
      pause_exit
    fi
    BACK_PORT="$NEW_PORT"
    echo ""
  fi
fi

export CASH_API="http://127.0.0.1:$BACK_PORT"

echo ""
if [ "$START_BACK" = "yes" ]; then
  echo "[1/2] 正在启动后端 API（端口 ${BACK_PORT}），日志：$BACK_LOG"
  ( cd "$ROOT/backend" && exec "$PY" -m uvicorn app.main:app --port "$BACK_PORT" ) >"$BACK_LOG" 2>&1 &
  BACK_PID=$!
else
  echo "[1/2] 端口 $BACK_PORT 上的后端就是本仓库这一份，直接复用（不再启动新的后端）"
fi

# ---------- 7. 前端：Next.js 只允许一个实例，只能复用，但要校验代理指向 ----------
WEB_REUSE=""
FRONT_PROXY_OK="yes"
if port_busy "$WEB_PORT"; then
  WEB_REUSE="yes"
  if ! frontend_proxy_ok "$WEB_PORT"; then
    FRONT_PROXY_OK="no"
  fi
  echo "[2/2] 端口 $WEB_PORT 上已有一个工作台实例（Next.js 同一目录只允许一个），直接复用它"
else
  echo "[2/2] 正在启动前端工作台（端口 ${WEB_PORT}），日志：$FRONT_LOG"
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
  WEB_REUSE="yes"
  frontend_proxy_ok "$WEB_PORT" || FRONT_PROXY_OK="no"
  echo ""
  echo "[提示] 检测到工作台已经有一个实例在运行（Next.js 同一目录只允许一个实例），"
  echo "       本次不再启动新的前端，直接复用已有实例：$OPEN_URL"
fi

# ---------- 10. 关键校验：前端最终连的是不是本仓库后端 ----------
if [ "$WEB_READY" = "yes" ] && [ "$FRONT_PROXY_OK" = "no" ]; then
  echo ""
  echo "⚠️  [重要] 这个工作台连的后端不是本仓库这一份（它的代理还指向旧端口上的另一个副本）。"
  echo "     这会导致页面拿不到项目、分类等新能力，看起来像「后端未连接」或界面错位。"
  echo ""
  echo "     处理办法（二选一）："
  echo "       1. 关掉正在跑的那个工作台（到它的终端窗口按 Control + C），再双击本脚本；"
  echo "       2. 或者干脆继续用旧的那一套，本脚本不做改动。"
  echo ""
  echo "     本仓库后端已在端口 $BACK_PORT 上跑着，日志：$BACK_LOG"
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

# ---------- 11. 收尾 ----------
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
  echo "  本次没有启动新进程，两个服务都复用了已有实例。"
fi
echo "============================================================"
echo ""

if [ -z "$BACK_PID" ] && [ -z "$FRONT_PID" ]; then
  read -r -p "按回车键关闭本窗口..." _
  exit 0
fi

wait
