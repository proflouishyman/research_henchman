/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        surface: {
          DEFAULT: '#F9F9F8',
          card: '#FFFFFF',
          muted: '#F3F2F0',
        },
        border: {
          DEFAULT: '#E5E2DD',
          strong: '#D0CCC5',
        },
        accent: {
          DEFAULT: '#D97706',
          light: '#FEF3C7',
          hover: '#B45309',
        },
        ink: {
          DEFAULT: '#1C1917',
          secondary: '#78716C',
          muted: '#A8A29E',
        },
        status: {
          active: '#059669',
          blocked: '#DC2626',
          pending: '#9CA3AF',
          progress: '#D97706',
          complete: '#374151',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Menlo', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 3px 0 rgba(0,0,0,0.06), 0 1px 2px -1px rgba(0,0,0,0.04)',
        panel: '0 4px 16px 0 rgba(0,0,0,0.08), 0 1px 4px 0 rgba(0,0,0,0.04)',
        modal: '0 20px 60px 0 rgba(0,0,0,0.14), 0 4px 16px 0 rgba(0,0,0,0.08)',
      },
    },
  },
  plugins: [],
}
