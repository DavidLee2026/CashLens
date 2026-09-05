import type { NextConfig } from "next";

// 本地开发：/api/* 代理到 CashLens FastAPI 后端（cd backend && python3 -m uvicorn app.main:app --port 8001）
const API_TARGET = process.env.CASH_API || "http://127.0.0.1:8001";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_TARGET}/api/:path*` }];
  },
};

export default nextConfig;
