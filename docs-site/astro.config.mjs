// @ts-check
import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";
import mdx from "@astrojs/mdx";

// https://astro.build/config
export default defineConfig({
  site: "https://plynf.com",
  integrations: [
    tailwind({ applyBaseStyles: false }),
    mdx(),
  ],
  markdown: {
    shikiConfig: {
      themes: {
        light: "github-light",
        dark: "github-dark",
      },
      wrap: true,
    },
  },
  build: {
    inlineStylesheets: "auto",
  },
  vite: {
    ssr: {
      noExternal: ["@fontsource/*"],
    },
  },
});
