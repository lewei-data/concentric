import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 后端（prototype/server.py）默认监听 8848。
// 3D 场景、three.js、头像、素材都在「服务根」，dev / preview 下必须透传给后端，
// 否则会被 Vite 的 SPA fallback 成 index.html，iframe 里只剩工作台外壳并最终超时。
const BACKEND = "http://127.0.0.1:8848";
const proxy = {
  "/api": BACKEND,
  "/login": BACKEND,
  "/login.html": BACKEND,
  "/legacy": BACKEND,
  "/avatars": BACKEND,
  "/office3d": BACKEND,
  "/meeting3d": BACKEND,
  "/vendor": BACKEND,
  "/faces": BACKEND,
  "/materials": BACKEND,
};

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: {
    outDir: "dist",
    // public/ 下挂着 downloads 软链（指向 4.2G 下载目录），另有 private_obsidian 指向
    // conring/users（私有，只经鉴权路由下发，绝不走静态兜底）。
    // 默认的 copyPublicDir 会跟随软链递归复制，构建会直接撑爆磁盘；
    // 故不复制，改由 server.py 运行时直接从 web/public 下发。
    copyPublicDir: false,
  },
  server: { proxy },
  preview: { proxy },
});
