/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Enterprise-grade neutral palette + semantic severity colors.
        ink: {
          50:  '#f7f8fa',
          100: '#eef1f5',
          200: '#dde2eb',
          300: '#c2cad9',
          400: '#8b95a8',
          500: '#5d6779',
          600: '#414a5c',
          700: '#2b3242',
          800: '#1c2230',
          900: '#0f1320',
          950: '#070912',
        },
        accent: {
          DEFAULT: '#4f8cff',
          hover:   '#3672e8',
          soft:    '#1a2542',
        },
        sev1: '#ef4444', // critical
        sev2: '#f59e0b', // high
        sev3: '#eab308', // warning
        sev4: '#3b82f6', // info
        ok:   '#22c55e',
        warn: '#f59e0b',
        bad:  '#ef4444',
      },
      fontFamily: {
        sans: ['"Inter"', '"Segoe UI"', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.06)',
        'card-dark': '0 1px 2px rgba(0,0,0,0.4), 0 4px 12px rgba(0,0,0,0.25)',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in':    'fadeIn 0.2s ease-out',
        'slide-up':   'slideUp 0.25s ease-out',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
};
