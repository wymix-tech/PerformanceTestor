import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import FlameGraph from '../FlameGraph.vue'

describe('FlameGraph', () => {
  it('解析折叠栈并渲染总量与画布', () => {
    const wrapper = mount(FlameGraph, {
      props: {
        folded: 'a.B;m.C 3\nm.D 1\n坏行',
        totalLabel: '样本'
      }
    })
    // 合法行总量 = 4（坏行被忽略）
    expect(wrapper.text()).toContain('4 样本')
    const canvas = wrapper.find('canvas')
    expect(canvas.exists()).toBe(true)
  })

  it('空折叠栈显示空态', () => {
    const wrapper = mount(FlameGraph, { props: { folded: '' } })
    expect(wrapper.text()).toContain('暂无折叠栈数据')
  })

  it('初始渲染无放大按钮', () => {
    const wrapper = mount(FlameGraph, { props: { folded: 'a.B;m.C 3\nm.D 1' } })
    expect(wrapper.find('canvas').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('返回全景')
  })
})
