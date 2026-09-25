import type { NextConfig } from "next";

// 本地开发：/api/* 代理到 CashLens FastAPI 后端（cd backend && python3 -m uvicorn app.main:app --port 8001）
const API_TARGET = process.env.CASH_API || "http://127.0.0.1:8001";

const nextConfig: NextConfig = {
  // 关掉左下角的开发调试浮标（Dev Tools Indicator）：录屏与演示时不该出现。
  // 依据 Next 16 文档 devIndicators：设为 false 后，编译与运行时错误仍会照常弹出。
  devIndicators: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_TARGET}/api/:path*` }];
  },
};

export default nextConfig;
