/** @type {import('tailwindcss').Config} */
// ESM export is mandatory — package.json declares "type": "module", so a
// `module.exports` here throws ReferenceError at Vite startup.
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#FAF8F4",
        ink: "#1B1B2F",
        // DEFAULT + dark flatten to `bg-coral` / `bg-coral-dark`, and the
        // opacity modifier still works (hover:bg-coral/5) — exactly the two
        // button variants the design system allows.
        coral: { DEFAULT: "#FF5A36", dark: "#E14D26" },
        pine: "#2F6F5E",
        gold: "#FFC857",
        muted: "#6B7280",
        line: "#E5E1D8",
        card: "#FFFFFF",
      },
      fontFamily: {
        // Overriding `sans` makes preflight set Inter globally via
        // `html { font-family: theme(fontFamily.sans) }` — no class needed
        // on <body>. display/mono stay opt-in per element.
        display: ['"Space Grotesk"', "ui-sans-serif", "system-ui", "sans-serif"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        tile: "0 10px 30px -12px rgba(27,27,47,0.18)",
        lift: "0 14px 34px -14px rgba(27,27,47,0.25)",
        glow: "0 0 0 6px rgba(255,90,54,0.15)",
      },
    },
  },
  plugins: [],
};
