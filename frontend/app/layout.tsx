import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "CashLens · 本地工作台",
  description: "CashLens 经营现金流可视化与决策辅助工具（编排式智能体），真数据对话工作台（本地优先）",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
