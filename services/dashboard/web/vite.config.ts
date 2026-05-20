import { defineConfig } from "vite";
import preact from "@preact/preset-vite";
import { resolve } from "path";

/**
 * Vite config for the Plinth dashboard SPA.
 *
 * Output path is the dashboard service's static dir, so the built bundle
 * ships inside the Python package and is served by FastAPI at
 * `/dist/...`. The existing vanilla-JS app.js continues to serve `/`
 * during the migration; new routes (welcome, settings, tools-inventory)
 * are served from this build.
 *
 * Dev mode (npm run dev) starts Vite on :5173 with a proxy back to the
 * dashboard service on :7424 for /api/* — see server.proxy below.
 */
export default defineConfig({
  plugins: [preact()],
  root: __dirname,
  base: "/dist/",
  build: {
    outDir: resolve(__dirname, "../src/plinth_dashboard/static/dist"),
    emptyOutDir: true,
    target: "es2020",
    sourcemap: true,
    rollupOptions: {
      input: {
        welcome: resolve(__dirname, "welcome.html"),
        // Additional entries land here as routes migrate from app.js:
        //   settings: resolve(__dirname, "settings.html"),
        //   tools:    resolve(__dirname, "tools.html"),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.PLINTH_DASHBOARD_URL ?? "http://127.0.0.1:7424",
        changeOrigin: true,
      },
    },
  },
});
