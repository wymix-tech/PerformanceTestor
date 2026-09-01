import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import StressTaskList from '../StressTaskList.vue'

/**
 * taskStatusClass / fmtTime / stressApi 依赖：桩掉后在受控数据下验证渲染与交互。
 */
const tasks = [
  {
    id: 101,
    taskName: '订单接口压测',
    targetUrl: 'http://127.0.0.1:8081/api/orders',
    appName: 'demo-app',
    appVersion: '1.2.0',
    status: 'finished',
    concurrency: 20,
    durationSec: 30,
    engineType: 'http',
    finishTime: '2026-01-15T14:30:00'
  },
  {
    id: 102,
    taskName: '',
    targetUrl: 'http://127.0.0.1:8081/api/users',
    appName: '',
    appVersion: '',
    status: 'running',
    concurrency: 10,
    durationSec: 20,
    engineType: 'http',
    finishTime: null
  }
]

const listMock = vi.fn()
vi.mock('../../../api', () => ({
  stressApi: {
    list: (...args: unknown[]) => listMock(...args)
  }
}))
vi.mock('../../../utils/format', () => ({
  taskStatusClass: (s: string) => `status-${s}`,
  fmtTime: (t: string | null) => (t ? 'formatted-time' : '')
}))

describe('StressTaskList', () => {
  beforeEach(() => {
    listMock.mockReset()
    listMock.mockResolvedValue({ data: tasks })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders fetched tasks', async () => {
    const wrapper = mount(StressTaskList)
    await flushPromises()

    expect(listMock).toHaveBeenCalledWith({ limit: 20 })
    const rows = wrapper.findAll('[data-test="task-row"]')
    expect(rows).toHaveLength(2)
    expect(wrapper.text()).toContain('订单接口压测')
  })

  it('falls back to target URL when task name is empty', async () => {
    const wrapper = mount(StressTaskList)
    await flushPromises()

    expect(wrapper.text()).toContain('/api/users')
  })

  it('shows app name and version badges only when present', async () => {
    const wrapper = mount(StressTaskList)
    await flushPromises()

    expect(wrapper.text()).toContain('demo-app')
    expect(wrapper.text()).toContain('v1.2.0')
    // 第二个任务无归属信息，不应出现额外徽章
    expect(wrapper.findAll('.bg-neon-blue\\/10')).toHaveLength(1)
  })

  it('renders status badge via shared helper', async () => {
    const wrapper = mount(StressTaskList)
    await flushPromises()

    expect(wrapper.html()).toContain('status-finished')
    expect(wrapper.html()).toContain('status-running')
  })

  it('emits select with task id on row click', async () => {
    const wrapper = mount(StressTaskList)
    await flushPromises()

    await wrapper.findAll('[data-test="task-row"]')[0].trigger('click')
    expect(wrapper.emitted('select')).toEqual([[101]])
  })

  it('shows empty hint when no tasks', async () => {
    listMock.mockResolvedValue({ data: [] })
    const wrapper = mount(StressTaskList)
    await flushPromises()

    expect(wrapper.text()).toContain('暂无压测任务')
  })

  it('exposes tasks and refresh to parent', async () => {
    const wrapper = mount(StressTaskList)
    await flushPromises()

    expect((wrapper.vm as any).tasks).toHaveLength(2)
    await (wrapper.vm as any).refresh()
    expect(listMock).toHaveBeenCalledTimes(2)
  })

  it('stops polling after unmount', async () => {
    vi.useFakeTimers()
    const wrapper = mount(StressTaskList)
    await flushPromises()
    const callsAfterMount = listMock.mock.calls.length

    wrapper.unmount()
    vi.advanceTimersByTime(10_000)

    // 卸载后不应再发起请求（避免内存泄漏与无效请求）
    expect(listMock.mock.calls.length).toBe(callsAfterMount)
  })

  it('survives a failing list request', async () => {
    listMock.mockRejectedValue(new Error('network down'))
    const wrapper = mount(StressTaskList)
    await flushPromises()

    // 加载失败不应导致组件崩溃
    expect(wrapper.text()).toContain('暂无压测任务')
  })
})
