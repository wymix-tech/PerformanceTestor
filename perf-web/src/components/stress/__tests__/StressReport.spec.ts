import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import StressReport from '../StressReport.vue'

/**
 * ECharts 图表打桩：jsdom 无 canvas 实现，真渲染会在 zrender 里抛
 * "Cannot read properties of null (reading 'clearRect')"。
 * 本用例只验证分步骤表格与告警卡片的渲染，图表内容无关，故整体替身掉。
 */
vi.mock('../../charts/MetricChart.vue', () => ({
  default: { name: 'MetricChart', template: '<div class="chart-stub" />' }
}))

const report = {
  task: { id: 4, taskName: 'verify', status: 'finished' },
  result: {
    totalRequests: 116504, qps: 14559.36, errorRate: 0,
    p50Ms: 0, p95Ms: 0, p99Ms: 0,
    warnings: ['占位符 {{p9.…}} 的步骤序号越界（共 2 步），渲染时将保留原文'],
    stepStats: [
      { stepName: 'step-1', totalRequests: 15749, errorCount: 0, errorRate: 0, qps: 0, avgMs: 0, maxMs: 5, renderMiss: 0 },
      { stepName: 'step-2', totalRequests: 16110, errorCount: 16110, errorRate: 100, qps: 0, avgMs: 0, maxMs: 5, renderMiss: 16110 }
    ]
  }
}

describe('StressReport 分步骤统计表格', () => {
  it('渲染分步骤表格并展示引用未命中列', () => {
    const w = mount(StressReport, { props: { report, samples: [] } })
    const txt = w.text()
    expect(txt).toContain('分步骤统计')
    expect(txt).toContain('step-1')
    expect(txt).toContain('step-2')
    // 存在未命中 → 展示「引用未命中」列
    expect(txt).toContain('引用未命中')
    expect(txt).toContain('16110')
    // 告警卡片
    expect(txt).toContain('配置告警')
  })

  it('老报告无 stepStats 时不渲染表格且不报错', () => {
    const w = mount(StressReport, {
      props: { report: { task: { id: 1, status: 'finished' }, result: { totalRequests: 10 } }, samples: [] }
    })
    expect(w.text()).not.toContain('分步骤统计')
    expect(w.text()).not.toContain('引用未命中')
  })
})
