import { describe, it, expect, beforeEach } from 'vitest'
import { chartColor, clearChartThemeCache, CHART_COLOR_VARS } from '../chartTheme'

/**
 * 图表主题色（P3-3）单测。
 *
 * chartColor 从 CSS 变量读值换算成 rgb()。风险点：
 *  - 未定义 / 段数不足 / 非数值的变量必须回落可用色，而不是 "rgb(NaN)"；
 *  - alpha 的 rgb/rgba 分支边界（1 与 <1）；
 *  - 主题切换后缓存能失效重算。
 */
describe('chartColor', () => {
  beforeEach(() => {
    clearChartThemeCache()
    for (const v of Object.values(CHART_COLOR_VARS)) document.documentElement.style.removeProperty(v)
  })

  it('读已定义的 CSS 变量并输出 rgb()', () => {
    document.documentElement.style.setProperty('--fg-sub', '138 148 166')
    clearChartThemeCache()
    expect(chartColor('fgSub')).toBe('rgb(138, 148, 166)')
  })

  it('支持透明度 → rgba()', () => {
    document.documentElement.style.setProperty('--overlay', '255 255 255')
    clearChartThemeCache()
    expect(chartColor('overlay', 0.15)).toBe('rgba(255, 255, 255, 0.15)')
  })

  it('alpha=1 时输出 rgb() 而非 rgba()', () => {
    document.documentElement.style.setProperty('--overlay', '0 0 0')
    clearChartThemeCache()
    expect(chartColor('overlay', 1)).toBe('rgb(0, 0, 0)')
  })

  it('alpha > 1 也走 rgb 分支', () => {
    document.documentElement.style.setProperty('--overlay', '10 20 30')
    clearChartThemeCache()
    expect(chartColor('overlay', 2)).toBe('rgb(10, 20, 30)')
  })

  it('alpha 略小于 1 走 rgba 分支', () => {
    document.documentElement.style.setProperty('--overlay', '10 20 30')
    clearChartThemeCache()
    expect(chartColor('overlay', 0.999)).toBe('rgba(10, 20, 30, 0.999)')
  })

  it('每个语义 key 都能解出合法 rgb()（覆盖 CHART_COLOR_VARS 全部条目）', () => {
    for (const k of Object.keys(CHART_COLOR_VARS)) {
      clearChartThemeCache()
      const out = chartColor(k)
      expect(out, k).toMatch(/^rgb\(\d+, \d+, \d+\)$/)
    }
  })

  it('变量缺失时回落可用色，不产生 NaN', () => {
    clearChartThemeCache()
    const out = chartColor('fgSub')
    expect(out).toMatch(/^rgb\(\d+, \d+, \d+\)$/)
    expect(out).not.toContain('NaN')
  })

  it('段数不足时回落兜底色', () => {
    document.documentElement.style.setProperty('--fg-sub', 'onlyoneword')
    clearChartThemeCache()
    expect(chartColor('fgSub')).toBe('rgb(128, 148, 166)')
  })

  it('非数值段回落兜底色（不产生 NaN）', () => {
    document.documentElement.style.setProperty('--fg-sub', 'a b c')
    clearChartThemeCache()
    const out = chartColor('fgSub')
    expect(out).toBe('rgb(128, 148, 166)')
  })

  it('多余空白/多段只取前三段', () => {
    document.documentElement.style.setProperty('--fg-sub', '  7   8   9   10  ')
    clearChartThemeCache()
    expect(chartColor('fgSub')).toBe('rgb(7, 8, 9)')
  })

  it('未知 key 使用兜底色', () => {
    clearChartThemeCache()
    expect(chartColor('not-a-key')).toBe('rgb(128, 148, 166)')
  })

  it('清缓存后能读到新变量值（主题切换场景）', () => {
    document.documentElement.style.setProperty('--fg-sub', '1 2 3')
    clearChartThemeCache()
    expect(chartColor('fgSub')).toBe('rgb(1, 2, 3)')
    // 缓存命中：改变量不重算
    document.documentElement.style.setProperty('--fg-sub', '4 5 6')
    expect(chartColor('fgSub')).toBe('rgb(1, 2, 3)')
    // 清缓存后重算
    clearChartThemeCache()
    expect(chartColor('fgSub')).toBe('rgb(4, 5, 6)')
  })
})
