/**
 * 图表主题辅助（P3-3）。
 *
 * ECharts 的 option 是纯对象，不会读 CSS，因此 3 处图表里原本写死了
 * `#8A94A6`（轴文字）/ `rgba(255,255,255,0.15)`（轴线）等深色主题专用色，
 * 浅色主题下轴标签会在白底上「隐形」。
 *
 * 做法：运行时从 :root / [data-theme] 上把 CSS 变量读出来换算成具体色值，
 * 供 option 使用。读取结果带缓存，主题切换后由调用方调用
 * {@link clearChartThemeCache} 失效重算（theme store 已接入）。
 *
 * 约定：CSS 变量一律为 `R G B` 三段裸数值（见 style.css），因此这里只做
 * 「按空白切分取前三段」这一种解析；不做 hex 兼容——两套写法并存会让
 * "浅色下值写成 hex 就静默失效" 成为可能，不如在写入侧就统一。
 */

/** 语义色名 → CSS 变量名（与 style.css 中定义保持一致，测试会逐条校验可解析） */
export const CHART_COLOR_VARS = {
  fgSub: '--fg-sub',
  fgMain: '--fg-main',
  fgDim: '--fg-dim',
  bgBase: '--bg-base',
  bgCard: '--bg-card',
  bgPanel: '--bg-panel',
  neonGreen: '--neon-green',
  neonBlue: '--neon-blue',
  overlay: '--overlay'
} as const

export type ChartColorKey = keyof typeof CHART_COLOR_VARS

/** 变量解析失败/缺失时的兜底色（中性灰，宁可难看也不出 NaN） */
const FALLBACK_RGB = [128, 148, 166] as const

let cache: Map<string, string> | null = null

/** 读取单个 CSS 变量的原始值，读不到返回空串 */
function readVar(name: string, root: HTMLElement): string {
  return getComputedStyle(root).getPropertyValue(name).trim()
}

/** 读取并缓存所有语义变量的原始值 */
function ensureCache(): Map<string, string> {
  if (cache) return cache
  const next = new Map<string, string>()
  // 无 document（SSR / 纯 node 单测）时不读 DOM，留空由 chartColor 走兜底
  if (typeof document !== 'undefined') {
    const root = document.documentElement
    for (const [key, cssVar] of Object.entries(CHART_COLOR_VARS)) {
      next.set(key, readVar(cssVar, root))
    }
  }
  cache = next
  return cache
}

/** 主题切换后必须调用，否则图表仍用旧主题色 */
export function clearChartThemeCache() {
  cache = null
}

/** 把 "R G B" 解析为三元组；段数不足或非数值时回落兜底色 */
function parseRgb(raw: string): [number, number, number] {
  const parts = raw.split(/\s+/).filter((p) => p !== '')
  if (parts.length < 3) return [FALLBACK_RGB[0], FALLBACK_RGB[1], FALLBACK_RGB[2]]
  const nums = parts.slice(0, 3).map((n) => Number(n))
  if (nums.some((n) => !Number.isFinite(n))) {
    return [FALLBACK_RGB[0], FALLBACK_RGB[1], FALLBACK_RGB[2]]
  }
  return [nums[0], nums[1], nums[2]]
}

/**
 * 取语义色的 `rgb(r, g, b)` 字符串（供 ECharts 直接使用）。
 * alpha < 1 时返回 `rgba(...)`；alpha >= 1 返回 `rgb(...)`。
 */
export function chartColor(key: ChartColorKey | string, alpha = 1): string {
  const raw = ensureCache().get(key) ?? ''
  const [r, g, b] = parseRgb(raw)
  return alpha < 1 ? `rgba(${r}, ${g}, ${b}, ${alpha})` : `rgb(${r}, ${g}, ${b})`
}
