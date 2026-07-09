import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Билд раздаётся FastAPI под /app/ (и под /desk/app/ за Caddy со срезом префикса) —
// база относительная, чтобы один билд работал под любым префиксом.
// Dev-сервер проксирует /api и /app/ws на бэкенд :8000.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true, ws: true },
    },
  },
});
