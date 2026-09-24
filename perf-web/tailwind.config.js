/** @type {import('tailwindcss').Config} */

// 明暗主题实现要点（P3-3）：
// 颜色一律走 CSS 变量（`rgb(var(--x) / <alpha-value>)`），变量在 style.css 中按
// `:root`（深色，默认）与 `[data-theme='light']` 两套定义。这样：
//   1) 组件里所有 `bg-bg-base text-fg-main` 等类名无需改动，切主题只切换根节点属性；
//   2) 透明度修饰符（`bg-bg-card/70`）仍可用——变量存 R G B 三段裸数值，由
//      Tailwind 的 `<alpha-value>` 注入，不会退化成不支持透明的 hex。
// 不要把这些值改回 hex：一旦写死颜色，light 主题整套样式会失效。
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`

export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // 覆盖内置 white：组件里大量 `bg-white/5`、`border-white/10` 原本是
        // 「深色底上的提亮叠层」，在浅色主题下会彻底不可见（白叠白）。
        // 把它指向 `--overlay` 语义变量：深色下是白、浅色下是黑，
        // 于是 138 处叠层用法无需逐个改写即可双主题可用。
        white: v('overlay'),
        // 深色科技风：深蓝黑底 + 荧光青绿强调（具体色值见 style.css 的 CSS 变量）
        bg: {
          base: v('bg-base'),
          card: v('bg-card'),
          panel: v('bg-panel')
        },
        neon: {
          green: v('neon-green'),
          blue: v('neon-blue'),
          teal: v('neon-teal'),
          purple: v('neon-purple')
        },
        fg: {
          main: v('fg-main'),
          sub: v('fg-sub'),
          dim: v('fg-dim')
        },
        // 霓虹强调色上的前景：无论明暗都取深色。
        // 之前用 text-bg-base 表达「按钮上的字」，浅色主题下 --bg-base 变近白，
        // 白字压在绿色按钮上对比度骤降（登录按钮即可见）。此变量与底色解耦。
        'on-neon': v('on-neon')
      },
      fontFamily: {
        sans: ['PingFang SC', 'Microsoft YaHei', 'system-ui', 'sans-serif']
      },
      boxShadow: {
        glass: 'var(--shadow-glass)',
        neon: 'var(--shadow-neon)'
      },
      keyframes: {
        float: {
          '0%, 100%': { transform: 'translateY(0)' },
          '50%': { transform: 'translateY(-6px)' }
        },
        pulseGlow: {
          '0%, 100%': { boxShadow: '0 0 12px rgba(0,229,160,0.15)' },
          '50%': { boxShadow: '0 0 28px rgba(0,229,160,0.45)' }
        },
        shake: {
          '0%, 100%': { transform: 'translateX(0)' },
          '20%, 60%': { transform: 'translateX(-6px)' },
          '40%, 80%': { transform: 'translateX(6px)' }
        }
      },
      animation: {
        float: 'float 4s ease-in-out infinite',
        pulseGlow: 'pulseGlow 3s ease-in-out infinite',
        shake: 'shake 0.4s ease-in-out'
      }
    }
  },
  plugins: []
}
