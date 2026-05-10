/** @type {import('tailwindcss').Config} */
export default {
  content: ["./src/**/*.{astro,md,mdx,html,ts,tsx,js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        rock: {
          50: "#fafaf7",
          100: "#f4f3ec",
          200: "#e7e5d9",
          300: "#d2cfbb",
          400: "#a8a28b",
          500: "#7a7466",
          600: "#5c5749",
          700: "#403c33",
          800: "#2a2823",
          900: "#1f1d18",
          950: "#13110d",
        },
        amber: {
          50: "#fffbeb",
          100: "#fef3c7",
          200: "#fde68a",
          300: "#fcd34d",
          400: "#fbbf24",
          500: "#f59e0b",
          600: "#d97706",
          700: "#b45309",
          800: "#92400e",
          900: "#78350f",
        },
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      maxWidth: {
        "container": "72rem",
        "prose": "44rem",
      },
      keyframes: {
        "fade-in-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "shimmer": {
          "0%": { backgroundPosition: "0% 50%" },
          "100%": { backgroundPosition: "100% 50%" },
        },
      },
      animation: {
        "fade-in-up": "fade-in-up .6s ease-out both",
        "shimmer": "shimmer 8s ease infinite",
      },
    },
  },
  plugins: [],
};
