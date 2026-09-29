import type { Config } from "tailwindcss";

/**
 * TaskPilot theme — near-black canvas, amber accent, blue used sparingly.
 * Kept in one place so the poster and the product cannot drift apart.
 */
const config: Config = {
  darkMode: "class",
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        canvas: "#0A0A0A",
        surface: "#111111",
        raised: "#171717",
        line: "#262626",
        line2: "#333333",
        ink: "#FAFAFA",
        muted: "#A3A3A3",
        faint: "#525252",
        amber: {
          DEFAULT: "#FBBF24",
          soft: "#FCD34D",
          dim: "#78350F",
        },
        brand: {
          DEFAULT: "#3B82F6",
          dim: "#1E3A8A",
        },
        ok: "#4ADE80",
        danger: "#F87171",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      borderRadius: {
        xl: "0.875rem",
        "2xl": "1.125rem",
        "3xl": "1.5rem",
      },
      boxShadow: {
        amber: "0 0 0 1px rgba(251,191,36,0.35), 0 0 32px -8px rgba(251,191,36,0.35)",
        card: "0 1px 0 0 rgba(255,255,255,0.03) inset, 0 10px 30px -18px rgba(0,0,0,0.9)",
      },
      keyframes: {
        "fade-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        pulse2: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.45" },
        },
        sweep: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.35s ease-out both",
        pulse2: "pulse2 1.6s ease-in-out infinite",
        sweep: "sweep 2.2s linear infinite",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
