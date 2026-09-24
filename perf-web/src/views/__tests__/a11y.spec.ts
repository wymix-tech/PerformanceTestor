import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

/**
 * 无障碍静态回查（P3-2）。
 *
 * 无障碍不像主题/文案那样有单一断言点，且大多「缺失」在视觉上不可见
 * （读屏用户才会遇到）。这里对关键交互件做静态源码回查，防止后续重构
 * 把已加的 aria 属性删掉而无任何提示。
 *
 * 刻意不追求「全站 aria 覆盖率」——那需要逐组件渲染断言，成本远高于收益；
 * 这里只锁「已明确要求」的几类：表格标签、展开态、状态播报、跳转链接。
 */
const here = dirname(fileURLToPath(import.meta.url))
function read(rel: string): string {
  return readFileSync(resolve(here, rel), 'utf-8')
}

describe('无障碍静态约束', () => {
  it('所有 <table> 都带 aria-label（读屏需知道表格用途）', () => {
    const files = [
      '../OptimizeView.vue',
      '../StressCenterView.vue',
      '../../components/stress/StressReport.vue',
      '../../components/stress/SchedulePanel.vue',
      '../../components/stress/StressTrendChart.vue',
      '../../components/monitor/JvmAlertRulePanel.vue',
      '../../components/stress/StressTaskList.vue',
      '../../components/stress/CapacityAdvicePanel.vue'
    ]
    for (const f of files) {
      const src = read(f)
      const tables = src.match(/<table\b[^>]*>/g) || []
      for (const t of tables) {
        expect(t, `${f} 中 <table> 缺少 aria-label: ${t}`).toContain('aria-label')
      }
    }
  })

  it('TraceTree 展开按钮带 aria-expanded', () => {
    const src = read('../../components/TraceTree.vue')
    expect(src).toContain('aria-expanded')
  })

  it('动态状态徽章用 role="status" 播报', () => {
    expect(read('../ProjectManageView.vue')).toContain('role="status"')
    expect(read('../../components/stress/StressTaskList.vue')).toContain('role="status"')
  })

  it('主布局提供跳到主内容的 skip link 且主内容可聚焦', () => {
    const src = read('../../layouts/MainLayout.vue')
    expect(src).toContain('skip-link')
    expect(src).toContain('id="main-content"')
    expect(src).toContain('tabindex="-1"')
  })

  it('全局样式定义了可见的键盘焦点环（:focus-visible）', () => {
    const css = readFileSync(resolve(here, '../../style.css'), 'utf-8')
    expect(css).toContain(':focus-visible')
    expect(css).toContain('outline')
  })

  it('侧边导航带 aria-label 且当前项标 aria-current', () => {
    const src = read('../../layouts/MainLayout.vue')
    expect(src).toContain(':aria-label="t(\'a11y.mainNav\')"')
    expect(src).toContain('aria-current')
  })

  it('登录页错误提示为 role="alert"，输入框与 label 关联', () => {
    const src = read('../LoginView.vue')
    expect(src).toContain('role="alert"')
    expect(src).toContain('for="login-username"')
    expect(src).toContain('id="login-username"')
  })

  it('图标仅作为装饰时带 aria-hidden', () => {
    const src = read('../../layouts/MainLayout.vue')
    expect(src).toContain('aria-hidden="true"')
  })
})
