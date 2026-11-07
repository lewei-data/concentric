import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: {
    outDir: "dist",
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8848",
      "/login.html": "http://127.0.0.1:8848",
      "/avatars": "http://127.0.0.1:8848",
    },
  },
});
