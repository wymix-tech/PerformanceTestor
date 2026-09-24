import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import StressForm from '../StressForm.vue'

/**
 * 「从模板填充」的往返正确性（P2-3）。
 *
 * 模板存的是 buildPayload 产物，填充时必须能无损回填到表单——
 * 尤其是请求头/断言（对象 ↔ 文本互转）与多接口场景步骤，
 * 这两处一旦不互逆，用户从模板填充后会静默丢配置。
 */
describe('StressForm 模板填充', () => {
  function form() {
    return mount(StressForm, { props: { scenarioEnabled: true, prepareDataEnabled: false } })
  }

  it('fillFromPayload 回填单接口参数', async () => {
    const w = form()
    w.vm.fillFromPayload({
      targetUrl: 'http://127.0.0.1:8081/api/orders',
      method: 'POST',
      engineType: 'http',
      concurrency: 80,
      durationSec: 45,
      warmupSec: 15,
      qpsLimit: 500,
      stageConcurrencies: [10, 50, 100]
    })
    await w.vm.$nextTick()

    const s = w.vm.snapshot()
    expect(s.targetUrl).toBe('http://127.0.0.1:8081/api/orders')
    expect(s.method).toBe('POST')
    expect(s.concurrency).toBe(80)
    expect(s.durationSec).toBe(45)
  })

  it('请求头与断言往返不丢（对象 ↔ 文本互逆）', async () => {
    const w = form()
    w.vm.fillFromPayload({
      targetUrl: 'http://x/api',
      headers: { Authorization: 'Bearer t', 'X-Tenant-Id': '1' },
      assertions: [
        { source: 'status', op: 'eq', expected: '200' },
        { source: 'json', key: 'data.code', op: 'eq', expected: '0' }
      ]
    })
    await w.vm.$nextTick()

    const payload = w.vm.buildPayload() as Record<string, any>
    expect(payload.headers).toEqual({ Authorization: 'Bearer t', 'X-Tenant-Id': '1' })
    expect(payload.assertions).toEqual([
      { source: 'status', op: 'eq', expected: '200' },
      { source: 'json', key: 'data.code', op: 'eq', expected: '0' }
    ])
  })

  it('scenario 模板回填后切到多接口模式并还原步骤', async () => {
    const w = form()
    w.vm.fillFromPayload({
      targetUrl: 'http://x',
      scenario: [
        { name: '登录', url: 'http://x/login', method: 'POST', weight: 1 },
        { name: '下单', alias: 'order', url: 'http://x/order', method: 'POST', weight: 3,
          headers: { 'X-Trace': 'abc' },
          extract: [{ var: 'token', source: 'body', path: 'data.token' }] }
      ]
    })
    await w.vm.$nextTick()

    const payload = w.vm.buildPayload() as Record<string, any>
    expect(payload.scenario).toHaveLength(2)
    expect(payload.scenario[0].name).toBe('登录')
    expect(payload.scenario[1].alias).toBe('order')
    expect(payload.scenario[1].weight).toBe(3)
    expect(payload.scenario[1].headers).toEqual({ 'X-Trace': 'abc' })
    expect(payload.scenario[1].extract).toEqual([{ var: 'token', source: 'body', path: 'data.token' }])
  })

  it('无 scenario 的模板回填后保持单接口模式（不残留上次的场景步骤）', async () => {
    const w = form()
    w.vm.fillFromPayload({
      targetUrl: 'http://x',
      scenario: [{ name: 'a', url: 'http://x/a', method: 'GET', weight: 1 }]
    })
    await w.vm.$nextTick()
    // 再填一个无 scenario 的模板：必须清空场景，避免"填充后仍跑旧场景"
    w.vm.fillFromPayload({ targetUrl: 'http://y/api' })
    await w.vm.$nextTick()

    const payload = w.vm.buildPayload() as Record<string, any>
    expect(payload.scenario).toBeUndefined()
    expect(payload.targetUrl).toBe('http://y/api')
  })

  it('空/非法入参不抛错（模板坏数据不应让页面崩）', async () => {
    const w = form()
    expect(() => w.vm.fillFromPayload(null)).not.toThrow()
    expect(() => w.vm.fillFromPayload(undefined)).not.toThrow()
    expect(() => w.vm.fillFromPayload('bad' as any)).not.toThrow()
  })
})
