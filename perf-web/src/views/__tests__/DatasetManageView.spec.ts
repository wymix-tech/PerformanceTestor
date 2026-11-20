import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import DatasetManageView from '../DatasetManageView.vue'

/**
 * 数据集管理页：列表/过滤/游标翻页/详情/进度/取消流程（桩掉 datasetApi 在受控数据下验证）。
 * 游标分页口径（B7 续批次 6）：cursor 传上页 nextCursor，hasMore=false 末页，「加载更多」追加。
 * 取消口径（B7 续批次 3）：operator/reason 必填；accepted=false 为幂等返回按成功处理。
 */
const page1Items = [
  {
    id: '7',
    definitionId: 'orders-ds@1',
    definitionVersion: '1',
    environmentId: 'dev-env',
    status: 'GENERATING',
    actualRows: null,
    actualStorageBytes: null,
    schemaFingerprint: null,
    validationSummary: null,
    errorMsg: null,
    transitionLog: [],
    createTime: '2026-01-15T14:30:00'
  },
  {
    id: '8',
    definitionId: 'users-ds@1',
    definitionVersion: '1',
    environmentId: 'dev-env',
    status: 'READY',
    actualRows: 4800,
    actualStorageBytes: 102400,
    schemaFingerprint: 'abc',
    validationSummary: { layers: 5 },
    errorMsg: null,
    transitionLog: [],
    createTime: '2026-01-15T15:00:00'
  },
  {
    id: '9',
    definitionId: 'legacy-ds@legacy',
    definitionVersion: 'legacy',
    environmentId: 'dev-env',
    status: 'CANCELLED',
    actualRows: null,
    actualStorageBytes: null,
    schemaFingerprint: null,
    validationSummary: null,
    errorMsg: null,
    transitionLog: [],
    createTime: '2026-01-15T16:00:00'
  }
]
const page2Items = [
  {
    id: '6',
    definitionId: 'items-ds@1',
    definitionVersion: '1',
    environmentId: 'dev-env',
    status: 'FAILED',
    actualRows: null,
    actualStorageBytes: null,
    schemaFingerprint: null,
    validationSummary: null,
    errorMsg: 'load failed',
    transitionLog: [],
    createTime: '2026-01-15T13:00:00'
  }
]

const listMock = vi.fn()
const detailMock = vi.fn()
const progressMock = vi.fn()
const cancelMock = vi.fn()

vi.mock('@/api', () => ({
  datasetApi: {
    list: (...args: unknown[]) => listMock(...args),
    detail: (...args: unknown[]) => detailMock(...args),
    progress: (...args: unknown[]) => progressMock(...args),
    cancel: (...args: unknown[]) => cancelMock(...args)
  }
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ username: 'tester' })
}))

describe('DatasetManageView', () => {
  let promptMock: { mockReturnValue: (v: string | null) => unknown }
  let confirmMock: { mockReturnValue: (v: boolean) => unknown }

  beforeEach(() => {
    // 桩返回 datasetApi.list 归一化后的 CursorPage 形态（normalizePage 之后，
    // 即组件消费边界——{items,nextCursor,hasMore}）
    listMock.mockReset().mockResolvedValue({
      items: page1Items, nextCursor: null, hasMore: false
    })
    detailMock.mockReset().mockResolvedValue({ code: 0, data: page1Items[0] })
    progressMock.mockReset().mockResolvedValue({
      code: 0,
      data: { taskId: 33, running: true, instanceStatus: 'GENERATING', errorMsg: null }
    })
    cancelMock.mockReset().mockResolvedValue({ code: 0, data: { accepted: true, state: 'CANCELLED' } })
    promptMock = vi.spyOn(window, 'prompt').mockReturnValue('回归前释放数据')
    confirmMock = vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.spyOn(window, 'alert').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  async function mountView() {
    const wrapper = mount(DatasetManageView)
    await flushPromises()
    return wrapper
  }

  it('renders first page with status badges', async () => {
    const wrapper = await mountView()

    expect(listMock).toHaveBeenCalledWith({ status: undefined, size: 50, cursor: undefined })
    const rows = wrapper.findAll('[data-test="dataset-row"]')
    expect(rows).toHaveLength(3)
    expect(wrapper.text()).toContain('orders-ds@1')
  })

  it('passes selected status filter to list api', async () => {
    const wrapper = await mountView()

    listMock.mockClear()
    await wrapper.find('[data-test="status-filter"]').setValue('READY')
    await flushPromises()

    expect(listMock).toHaveBeenCalledWith({ status: 'READY', size: 50, cursor: undefined })
  })

  it('appends next page via load-more and shows end hint on last page', async () => {
    listMock
      .mockResolvedValueOnce({ items: page1Items, nextCursor: 'c1', hasMore: true })
      .mockResolvedValueOnce({ items: page2Items, nextCursor: null, hasMore: false })
    const wrapper = await mountView()

    expect(wrapper.find('[data-test="load-more"]').exists()).toBe(true)
    await wrapper.find('[data-test="load-more"]').trigger('click')
    await flushPromises()

    // 翻页带上上一页游标，追加不重置
    expect(listMock).toHaveBeenLastCalledWith({ status: undefined, size: 50, cursor: 'c1' })
    expect(wrapper.findAll('[data-test="dataset-row"]')).toHaveLength(4)
    expect(wrapper.text()).toContain('items-ds@1')
    // 末页：按钮消失，出现已到最后一条
    expect(wrapper.find('[data-test="load-more"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('已到最后一条')
  })

  it('keeps loaded pages when append fails', async () => {
    listMock
      .mockResolvedValueOnce({ items: page1Items, nextCursor: 'c1', hasMore: true })
      .mockRejectedValueOnce(new Error('network down'))
    const wrapper = await mountView()

    await wrapper.find('[data-test="load-more"]').trigger('click')
    await flushPromises()

    // 翻页失败静默保旧数据（不闪空、不标记首屏失败态）
    expect(wrapper.findAll('[data-test="dataset-row"]')).toHaveLength(3)
    expect(wrapper.text()).not.toContain('实例列表加载失败')
  })

  it('marks load failure and keeps previous data on reset error', async () => {
    const wrapper = await mountView()

    listMock.mockRejectedValue(new Error('network down'))
    await (wrapper.vm as any).reload()
    await flushPromises()

    expect(wrapper.text()).toContain('实例列表加载失败')
    // 保留旧数据不闪空
    expect(wrapper.findAll('[data-test="dataset-row"]')).toHaveLength(3)
  })

  it('shows cancel button only on cancelable states', async () => {
    const wrapper = await mountView()

    // GENERATING 可取消；READY/CANCELLED 无取消出边不显示
    expect(wrapper.findAll('[data-test="cancel-btn"]')).toHaveLength(1)
  })

  it('loads detail and progress when selecting a running instance', async () => {
    const wrapper = await mountView()

    await wrapper.findAll('[data-test="dataset-row"]')[0].trigger('click')
    await flushPromises()

    expect(detailMock).toHaveBeenCalledWith('7')
    expect(progressMock).toHaveBeenCalledWith('7')
    expect(wrapper.text()).toContain('构建进度')
    expect(wrapper.text()).toContain('运行中')
  })

  it('skips progress polling for terminal instances', async () => {
    const wrapper = await mountView()

    detailMock.mockResolvedValue({ code: 0, data: page1Items[1] })
    await wrapper.findAll('[data-test="dataset-row"]')[1].trigger('click')
    await flushPromises()

    expect(progressMock).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="progress-panel"]').exists()).toBe(false)
    // READY 详情仍展示验证摘要与存储
    expect(wrapper.text()).toContain('验证摘要')
    expect(wrapper.text()).toContain('102400 B')
  })

  it('sends cancel with operator and audit reason, refreshes list on accepted', async () => {
    const wrapper = await mountView()

    await wrapper.findAll('[data-test="cancel-btn"]')[0].trigger('click')
    await flushPromises()

    expect(window.prompt).toHaveBeenCalled()
    expect(window.confirm).toHaveBeenCalled()
    expect(cancelMock).toHaveBeenCalledWith('7', { operator: 'tester', reason: '回归前释放数据' })
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('已发起取消'))
    // 取消后刷新列表（重置游标拉第一页）
    expect(listMock.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('treats accepted=false idempotent reply as success', async () => {
    const wrapper = await mountView()

    cancelMock.mockResolvedValue({ code: 0, data: { accepted: false, state: 'CANCELLED' } })
    await wrapper.findAll('[data-test="cancel-btn"]')[0].trigger('click')
    await flushPromises()

    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('幂等返回'))
  })

  it('surfaces backend rejection message on code!=0', async () => {
    const wrapper = await mountView()

    cancelMock.mockResolvedValue({ code: 1, message: 'VERIFYING 状态无取消出边' })
    await wrapper.findAll('[data-test="cancel-btn"]')[0].trigger('click')
    await flushPromises()

    expect(window.alert).toHaveBeenCalledWith('VERIFYING 状态无取消出边')
  })

  it('refuses to cancel without audit reason', async () => {
    const wrapper = await mountView()

    promptMock.mockReturnValue('   ')
    await wrapper.findAll('[data-test="cancel-btn"]')[0].trigger('click')
    await flushPromises()

    expect(cancelMock).not.toHaveBeenCalled()
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('取消理由必填'))
  })

  it('aborts cancel when user declines confirmation', async () => {
    const wrapper = await mountView()

    confirmMock.mockReturnValue(false)
    await wrapper.findAll('[data-test="cancel-btn"]')[0].trigger('click')
    await flushPromises()

    expect(cancelMock).not.toHaveBeenCalled()
  })

  it('aborts cancel when prompt dismissed', async () => {
    const wrapper = await mountView()

    promptMock.mockReturnValue(null)
    await wrapper.findAll('[data-test="cancel-btn"]')[0].trigger('click')
    await flushPromises()

    expect(cancelMock).not.toHaveBeenCalled()
  })

  it('polls selected instance detail but not the list', async () => {
    vi.useFakeTimers()
    const wrapper = mount(DatasetManageView)
    await vi.runOnlyPendingTimersAsync()
    // 首屏后列表不再自动轮询（经 刷新/加载更多 手动维护，避免翻页态被覆盖）
    const listCallsAfterMount = listMock.mock.calls.length

    await wrapper.findAll('[data-test="dataset-row"]')[0].trigger('click')
    await flushPromises()
    const detailCallsAfterSelect = detailMock.mock.calls.length

    await vi.advanceTimersByTimeAsync(15_000)

    // 轮询只刷选中实例详情，列表请求数不变
    expect(listMock.mock.calls.length).toBe(listCallsAfterMount)
    expect(detailMock.mock.calls.length).toBeGreaterThan(detailCallsAfterSelect)
  })

  it('stops polling after unmount', async () => {
    vi.useFakeTimers()
    const wrapper = mount(DatasetManageView)
    await vi.runOnlyPendingTimersAsync()

    await wrapper.findAll('[data-test="dataset-row"]')[0].trigger('click')
    await flushPromises()
    const detailCallsAfterSelect = detailMock.mock.calls.length

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(20_000)

    // 卸载后不应再发起请求（避免内存泄漏与无效请求）
    expect(detailMock.mock.calls.length).toBe(detailCallsAfterSelect)
  })
})
