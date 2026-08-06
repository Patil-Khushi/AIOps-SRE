// ESM syntax is mandatory here — package.json declares "type": "module".
// Vite auto-discovers this file; do NOT add a css.postcss block to
// vite.config.js, that overrides discovery and silently disables Tailwind.
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
