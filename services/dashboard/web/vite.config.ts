import { defineConfig } from "vite";
import preact from "@preact/preset-vite";
import path from "node:path";

// Vite config for the Plynf dashboard SPA.
//
// Build target: services/dashboard/src/plinth_dashboard/static_vite/
// This is served by the dashboard FastAPI service at /static/. The
// existing vanilla-JS bundle lives at static/app.js and stays in place
// until the migration is complete — both can coexist behind feature flags.
//
// Dev mode: vite serves on port 5173 and proxies /api → the live
// dashboard backend (port 7424) so we can develop against real data.

export default defineConfig({
  plugins: [preact()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
      "@/components": path.resolve(__dirname, "src/components"),
      "@/routes": path.resolve(__dirname, "src/routes"),
      "@/lib": path.resolve(__dirname, "src/lib"),
    },
  },
  build: {
    outDir: "../src/plinth_dashboard/static_vite",
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks: {
          preact: ["preact", "preact-router", "@preact/signals"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api":     { target: "http://127.0.0.1:7424", changeOrigin: true },
      "/healthz": { target: "http://127.0.0.1:7424", changeOrigin: true },
    },
  },
  test: {
    environment: "happy-dom",
    globals: true,
  },
});
