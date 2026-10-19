import type { NextConfig } from "next";

// 本地开发：/api/* 代理到 CashLens FastAPI 后端（cd backend && python3 -m uvicorn app.main:app --port 8001）
const API_TARGET = process.env.CASH_API || "http://127.0.0.1:8001";

/* 服务器部署走静态导出：CASH_STATIC_EXPORT=1 pnpm build
   产物 out/ 交给 nginx，服务器上不跑 Node；/api 由 nginx 反代到 FastAPI。
   Next 16 文档「Static Exports → Unsupported Features」明确写了 rewrites 不支持，
   所以导出模式下不注册它（本地 dev / next start 仍照旧）。 */
const STATIC_EXPORT = process.env.CASH_STATIC_EXPORT === "1";

const nextConfig: NextConfig = {
  ...(STATIC_EXPORT ? { output: "export" as const } : {}),
  // 关掉左下角的开发调试浮标（Dev Tools Indicator）：录屏与演示时不该出现。
  // 依据 Next 16 文档 devIndicators：设为 false 后，编译与运行时错误仍会照常弹出。
  devIndicators: false,
  // 导出模式下**整个键都不出现**：只返回空数组仍会被 Next 判成「配置了 rewrites」并告警。
  ...(STATIC_EXPORT
    ? {}
    : {
        async rewrites() {
          return [{ source: "/api/:path*", destination: `${API_TARGET}/api/:path*` }];
        },
      }),
};

export default nextConfig;
