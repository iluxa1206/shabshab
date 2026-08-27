import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Билд раздаётся FastAPI под /app/ (и под /desk/app/ за Caddy со срезом префикса) —
// база относительная, чтобы один билд работал под любым префиксом.
// Dev-сервер проксирует /api и /app/ws на бэкенд :8000.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  // Smoke-проверки рендера (npm test): jsdom, потому что проверяем именно
  // МОНТИРОВАНИЕ — сборка молчит про ошибки, которые случаются только в рантайме
  // (27.08.2026 монитор упал белым экраном на обращении к const из мёртвой зоны).
  // Стартовый URL — под базой роутера (/app): с "/" <Router basename="/app">
  // не матчит ничего и молча рендерит пустоту (та же ловушка, что у дев-сервера).
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: "http://localhost/app/floaters" } },
    include: ["src/**/*.test.jsx"],
    setupFiles: ["./src/test-setup.js"],
    restoreMocks: true,
  },
  server: {
    proxy: {
      "/api": { target: process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000", changeOrigin: true, ws: true },
    },
  },
});
