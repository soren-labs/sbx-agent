/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// SOR-266: stamp the deployed bundle with the git SHA it was built from
// (VITE_BUILD_SHA at build time) so production HTML proves asset identity.
export default defineConfig(({ mode }) => ({
  plugins: [
    react(),
    {
      name: "sbx-build-meta",
      transformIndexHtml: (html) =>
        html.replaceAll(
          "__SBX_BUILD_SHA__",
          loadEnv(mode, "", "VITE_").VITE_BUILD_SHA ?? "",
        ),
    },
  ],
  server: {
    port: 5174,
    proxy: {
      // Single unified business API; no /v1, /v2 or /hosted routes.
      "/api": {
        target: loadEnv(mode, ".", "").SBX_API_PROXY_TARGET ?? "http://127.0.0.1:8800",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
}));
