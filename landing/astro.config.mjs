import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";

export default defineConfig({
  site: "https://plynf.com",
  integrations: [
    tailwind({ applyBaseStyles: false }),
  ],
  output: "static",
  compressHTML: true,
  build: {
    inlineStylesheets: "auto",
  },
  vite: {
    build: {
      // Default esbuild minifier; switch to "lightningcss" if you `npm i -D lightningcss`.
      cssMinify: true,
    },
  },
});
