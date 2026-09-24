@echo off
chcp 65001 >nul
setlocal
title CashLens 一键启动（后端 8001 / 前端 3000）

rem ===========================================================================
rem  CashLens 一键启动：双击本文件，同时拉起后端 API 与前端工作台
rem  后端  FastAPI  ->  http://127.0.0.1:8001
rem  前端  Next.js  ->  http://localhost:3000
rem  停止  关闭弹出的「CashLens 后端」「CashLens 前端」两个窗口即可
rem ===========================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo.
echo  ============================================================
echo    CashLens 一键启动
echo    后端 API     http://127.0.0.1:8001
echo    前端工作台   http://localhost:3000
echo  ============================================================
echo.

rem ---------- 1. 查找可用的 Python ----------
set "PY="
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  py -3 --version >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  echo  [错误] 没有找到可用的 Python 3。
  echo         请安装 Python 3.10 或更新版本，安装时勾选 Add Python to PATH。
  echo         下载地址： https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)
echo  [OK] Python 命令： %PY%

rem ---------- 2. 检查后端依赖 ----------
%PY% -c "import fastapi, uvicorn, pydantic" >nul 2>nul
if errorlevel 1 (
  echo  [错误] 缺少后端依赖 fastapi / uvicorn / pydantic。
  echo         请先执行下面这一行安装，然后重新双击本文件：
  echo.
  echo             %PY% -m pip install fastapi uvicorn pydantic
  echo.
  pause
  exit /b 1
)
echo  [OK] 后端依赖齐备

rem ---------- 3. 检查前端运行环境 ----------
where pnpm >nul 2>nul
if errorlevel 1 (
  echo  [错误] 没有找到 pnpm。
  echo         请先安装 Node.js 20 或更新版本（ https://nodejs.org ），
  echo         再执行： npm install -g pnpm
  echo.
  pause
  exit /b 1
)
echo  [OK] 前端包管理器 pnpm 就绪

rem ---------- 4. 首次运行自动安装前端依赖 ----------
set "NEXT_CMD=%ROOT%\frontend\node_modules\.bin\next.cmd"
if not exist "%NEXT_CMD%" (
  echo.
  echo  [提示] 未检测到前端依赖，正在执行 pnpm install，请稍等（首次约需 1 至 3 分钟）...
  pushd "%ROOT%\frontend"
  call pnpm install
  if errorlevel 1 (
    popd
    echo.
    echo  [错误] pnpm install 失败。请检查网络后，手动在 frontend 目录执行 pnpm install。
    pause
    exit /b 1
  )
  popd
)
if not exist "%NEXT_CMD%" (
  echo  [错误] 前端依赖仍不完整（缺少 node_modules\.bin\next.cmd）。
  echo         请手动进入 frontend 目录执行 pnpm install 后再试。
  pause
  exit /b 1
)
echo  [OK] 前端依赖就绪

rem ---------- 5. 首次运行自动生成 .env（不填 Key 时走本地规则兜底）----------
if not exist "%ROOT%\.env" (
  if exist "%ROOT%\.env.example" (
    copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
    echo  [提示] 已由 .env.example 生成 .env；未填 LLM Key 时对话走本地规则兜底
  )
)

rem ---------- 6. 端口占用提醒（只提示，不阻塞）----------
netstat -ano | findstr ":8001" | findstr /i "LISTENING" >nul 2>nul
if not errorlevel 1 echo  [警告] 端口 8001 已被占用，后端可能起不来（或已有实例在运行）。
netstat -ano | findstr ":3000" | findstr /i "LISTENING" >nul 2>nul
if not errorlevel 1 echo  [警告] 端口 3000 已被占用，前端可能提示已有实例在运行。

rem ---------- 7. 同时启动后端与前端 ----------
echo.
echo  [1/2] 正在启动后端 API（新窗口：CashLens 后端）...
start "CashLens 后端 :8001" /d "%ROOT%\backend" cmd /k "%PY% -m uvicorn app.main:app --port 8001"

set "FRONTEND_CMD=node_modules\.bin\next.cmd dev"
if not exist "%NEXT_CMD%" set "FRONTEND_CMD=pnpm dev"

echo  [2/2] 正在启动前端工作台（新窗口：CashLens 前端，首次编译约需 10 至 30 秒）...
start "CashLens 前端 :3000" /d "%ROOT%\frontend" cmd /k "%FRONTEND_CMD%"

rem ---------- 8. 等后端就绪后再打开浏览器 ----------
where curl >nul 2>nul
if errorlevel 1 goto FIXED_WAIT

echo.
echo  等待后端就绪（最多 30 秒）...
set /a TRIES=0
:WAIT_BACKEND
curl -s -o nul --max-time 2 http://127.0.0.1:8001/api/health >nul 2>nul
if not errorlevel 1 goto BACKEND_READY
set /a TRIES+=1
if %TRIES% GEQ 30 goto BACKEND_SLOW
timeout /t 1 /nobreak >nul
goto WAIT_BACKEND

:BACKEND_READY
echo  [OK] 后端已就绪
goto OPEN_BROWSER

:BACKEND_SLOW
echo  [提示] 后端 30 秒内没有响应，请查看「CashLens 后端」窗口里的报错信息。
goto OPEN_BROWSER

:FIXED_WAIT
echo.
echo  等待 12 秒后打开浏览器...
timeout /t 12 /nobreak >nul

:OPEN_BROWSER
timeout /t 3 /nobreak >nul
start "" "http://localhost:3000"

echo.
echo  ============================================================
echo    两个服务已分别在独立窗口运行：
echo      后端 API     http://127.0.0.1:8001/api/health
echo      前端工作台   http://localhost:3000
echo    浏览器会自动打开工作台；若页面提示连不上后端，刷新一次即可。
echo    停止服务：关闭「CashLens 后端」与「CashLens 前端」两个窗口。
echo    本窗口可以直接关闭。
echo  ============================================================
echo.
pause
endlocal
