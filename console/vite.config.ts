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
      // SOR-262 integration hook: point the dev server at a real control
      // plane with VITE_API_BASE / a proxy target.
      "/v2": { target: loadEnv(mode, ".", "").SBX_API_PROXY_TARGET ?? "http://127.0.0.1:8787", changeOrigin: true },
      "/v1": { target: loadEnv(mode, ".", "").SBX_API_PROXY_TARGET ?? "http://127.0.0.1:8787", changeOrigin: true },
      "/api": { target: loadEnv(mode, ".", "").SBX_API_PROXY_TARGET ?? "http://127.0.0.1:8787", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
}));
