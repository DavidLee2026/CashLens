import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "CashLens · 本地工作台",
  description: "CashLens 经营现金流可视化与决策辅助工具（编排式智能体），真数据对话工作台（本地优先）",
  /* PWA（2026-10-20）：先只做「能装到桌面/主屏 + 独立窗口打开」，不接管离线。
     为什么不做 Service Worker 离线缓存：这个应用的每一屏都要向后端取真账本，
     缓存住的只有空壳；更麻烦的是我们刚吃过「线上还是旧产物」的苦 —— SW 会把
     「代码新鲜度」这件事再搅浑一次。要离线，先离线账本（那是数据层的事），不是先缓存页面。 */
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icon-192.png", apple: "/apple-touch-icon.png" },
  appleWebApp: { capable: true, title: "CashLens", statusBarStyle: "default" },
  formatDetection: { telephone: false },
};

/* 浏览器地址栏/状态栏配色与 manifest 的 theme_color 保持一致（brand indigo） */
export const viewport: Viewport = {
  themeColor: "#635bff",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
