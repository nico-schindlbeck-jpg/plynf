/** @type {import('tailwindcss').Config} */
export default {
  content: ["./src/**/*.{astro,html,js,jsx,md,mdx,svelte,ts,tsx,vue}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: {
          0: "var(--bg-0)",
          1: "var(--bg-1)",
          2: "var(--bg-2)",
          3: "var(--bg-3)",
        },
        border: {
          DEFAULT: "var(--border)",
          strong: "var(--border-strong)",
        },
        ink: {
          primary: "var(--text-primary)",
          secondary: "var(--text-secondary)",
          muted: "var(--text-muted)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          glow: "var(--accent-glow)",
          subtle: "var(--accent-subtle)",
        },
      },
      fontFamily: {
        sans: [
          "Inter Variable",
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
        "display-sm": ["2rem", { lineHeight: "1.1", letterSpacing: "-0.025em" }],
        "display-md": ["3rem", { lineHeight: "1.05", letterSpacing: "-0.035em" }],
        "display-lg": ["4rem", { lineHeight: "1.04", letterSpacing: "-0.04em" }],
        "display-xl": ["5rem", { lineHeight: "1.02", letterSpacing: "-0.045em" }],
        "display-2xl": ["6rem", { lineHeight: "1.0", letterSpacing: "-0.05em" }],
      },
      letterSpacing: {
        tightest: "-0.04em",
        ultra: "-0.05em",
      },
      maxWidth: {
        prose: "65ch",
        page: "1200px",
        narrow: "880px",
      },
      boxShadow: {
        "card": "0 0 0 1px var(--border), 0 1px 2px rgba(0,0,0,0.4)",
        "card-hover":
          "0 0 0 1px var(--border-strong), 0 8px 32px rgba(0,0,0,0.45), 0 0 0 1px rgba(255,120,73,0.04)",
        "glow":
          "0 0 0 1px rgba(255,120,73,0.18), 0 8px 40px rgba(255,120,73,0.16)",
        "ring": "0 0 0 1px var(--accent), 0 0 0 4px rgba(255,120,73,0.18)",
      },
      animation: {
        "pulse-line": "pulse-line 3s ease-in-out infinite",
        "float-slow": "float-slow 8s ease-in-out infinite",
        "shimmer": "shimmer 2.5s linear infinite",
        "fade-up": "fade-up 0.6s ease-out forwards",
      },
      keyframes: {
        "pulse-line": {
          "0%, 100%": { strokeDashoffset: "0" },
          "50%": { strokeDashoffset: "-32" },
        },
        "float-slow": {
          "0%, 100%": { transform: "translateY(0px)" },
          "50%": { transform: "translateY(-6px)" },
        },
        "shimmer": {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};
