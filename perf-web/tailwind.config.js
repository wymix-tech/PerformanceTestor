/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // 深色科技风：深蓝黑底 + 荧光青绿强调
        bg: {
          base: '#0B1220',
          card: '#111A2E',
          panel: '#182238'
        },
        neon: {
          green: '#00E5A0',
          blue: '#0EA5E9',
          teal: '#34D399',
          purple: '#A78BFA'
        },
        fg: {
          main: '#E6EDF7',
          sub: '#8A94A6',
          dim: '#C8D2E0'
        }
      },
      fontFamily: {
        sans: ['PingFang SC', 'Microsoft YaHei', 'system-ui', 'sans-serif']
      },
      boxShadow: {
        glass: '0 8px 32px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255,255,255,0.06)',
        neon: '0 0 20px rgba(0, 229, 160, 0.25)'
      },
      keyframes: {
        float: {
          '0%, 100%': { transform: 'translateY(0)' },
          '50%': { transform: 'translateY(-6px)' }
        },
        pulseGlow: {
          '0%, 100%': { boxShadow: '0 0 12px rgba(0,229,160,0.15)' },
          '50%': { boxShadow: '0 0 28px rgba(0,229,160,0.45)' }
        }
      },
      animation: {
        float: 'float 4s ease-in-out infinite',
        pulseGlow: 'pulseGlow 3s ease-in-out infinite'
      }
    }
  },
  plugins: []
}
