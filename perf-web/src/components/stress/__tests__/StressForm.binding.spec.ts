import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import StressForm from '../StressForm.vue'

/**
 * 绑定还原锁定测试（B7 续批次 5）：「再来一次」fillFrom 回填历史任务时经
 * GET /api/stress/tasks/{id}/dataset 反查还原数据集绑定（B7 续批次 2 落地的
 * restoreBinding，此处锁定其三态口径防回归）：
 * - 反查命中且实例仍在 READY 列表 → 回填（snapshot().dataset 携带 instanceId）；
 * - 未绑定（data=null）或绑定失效（实例已被清理/降级离开 READY 列表）→ 复位不绑定；
 * - 反查失败 → 静默保持不绑定，不阻断回填主链路。
 */
const readyList = [
  { id: '7', definitionId: 'orders-ds@1', status: 'READY', actualRows: 4800 },
  { id: '8', definitionId: 'users-ds@1', status: 'READY', actualRows: 100 }
]

const datasetListMock = vi.fn()
const stressDatasetMock = vi.fn()

vi.mock('@/api', () => ({
  datasetApi: {
    list: (...args: unknown[]) => datasetListMock(...args)
  },
  stressApi: {
    dataset: (...args: unknown[]) => stressDatasetMock(...args)
  }
}))

/** 历史任务（wire 契约不含 dataset_*，绑定只能走反查） */
function historyTask(overrides: Record<string, any> = {}) {
  return {
    id: 101,
    targetUrl: 'http://127.0.0.1:8081/api/orders',
    method: 'GET',
    engineType: 'http',
    concurrency: 20,
    durationSec: 30,
    ...overrides
  }
}

async function mountForm() {
  const wrapper = mount(StressForm)
  await flushPromises()
  return wrapper
}

describe('StressForm 绑定还原（fillFrom → restoreBinding）', () => {
  beforeEach(() => {
    // 桩返回 datasetApi.list 归一化后的 CursorPage 形态（normalizePage 之后）
    datasetListMock.mockReset().mockResolvedValue({
      items: readyList, nextCursor: null, hasMore: false
    })
    stressDatasetMock.mockReset().mockResolvedValue({
      code: 0,
      data: { taskId: 101, datasetInstanceId: '7', datasetDefinitionVersion: '1' }
    })
  })

  it('restores binding when reverse lookup hits a READY instance', async () => {
    const wrapper = await mountForm()

    wrapper.vm.fillFrom(historyTask())
    await flushPromises()

    expect(stressDatasetMock).toHaveBeenCalledWith(101)
    // 游标分页单页拉满（size=100）：绑定下拉仅取 READY，首屏即全量口径
    expect(datasetListMock).toHaveBeenCalledWith({ status: 'READY', size: 100 })
    const snap = wrapper.vm.snapshot()
    expect(snap.dataset).toEqual({ instanceId: '7' })
  })

  it('keeps no binding when task has none (data=null)', async () => {
    const wrapper = await mountForm()

    stressDatasetMock.mockResolvedValue({ code: 0, data: null })
    wrapper.vm.fillFrom(historyTask())
    await flushPromises()

    expect(wrapper.vm.snapshot().dataset).toBeUndefined()
  })

  it('resets binding when instance left the READY list', async () => {
    const wrapper = await mountForm()

    // 反查仍返回历史绑定，但实例已被清理/降级，不在 READY 列表 → 复位
    stressDatasetMock.mockResolvedValue({
      code: 0,
      data: { taskId: 101, datasetInstanceId: '999' }
    })
    wrapper.vm.fillFrom(historyTask())
    await flushPromises()

    expect(wrapper.vm.snapshot().dataset).toBeUndefined()
  })

  it('stays unbound silently when reverse lookup fails', async () => {
    const wrapper = await mountForm()

    stressDatasetMock.mockRejectedValue(new Error('network down'))
    wrapper.vm.fillFrom(historyTask())
    await flushPromises()

    // 失败不抛错、不阻断回填主链路（其余参数已回填）
    const snap = wrapper.vm.snapshot()
    expect(snap.dataset).toBeUndefined()
    expect(snap.targetUrl).toBe('http://127.0.0.1:8081/api/orders')
    expect(snap.concurrency).toBe(20)
  })

  it('carries binding into schedule template snapshot (SchedulePanel 预填链路)', async () => {
    const wrapper = await mountForm()

    // 调度触发走同一 create_and_run 绑定路径：snapshot 携带的绑定随模板
    // JSON 进入 SchedulePanel，调度触发时后端按同一绑定门禁解析
    wrapper.vm.fillFrom(historyTask())
    await flushPromises()

    const snap = wrapper.vm.snapshot()
    expect(JSON.parse(JSON.stringify(snap)).dataset).toEqual({ instanceId: '7' })
  })
})
