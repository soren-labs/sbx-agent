/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      // SOR-262 integration hook: point the dev server at a real control
      // plane with VITE_API_BASE / a proxy target.
      "/v2": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/v1": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
