import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import {
  useThemeStore,
  resolveTheme,
  readStoredTheme,
  applyTheme,
  systemPrefersDark,
  THEME_KEY
} from '../theme'
import { clearChartThemeCache } from '../../utils/chartTheme'

/**
 * 明暗主题（P3-3）单测。
 *
 * 重点覆盖三类「只有真机/边界才暴露」的行为：
 *  1. system 偏好解析与 systemPrefersDark 在 matchMedia 缺失时的回落（jsdom 默认无 matchMedia）；
 *  2. 切换后 <html data-theme> 与 .dark class 必须同时同步（第三方组件按 .dark 判断）；
 *  3. localStorage 里存了非法值时不得抛错、必须回落 system。
 */
describe('theme store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
    document.documentElement.classList.remove('dark')
    clearChartThemeCache()
  })

  it('resolveTheme 把 system 解析为系统偏好', () => {
    expect(resolveTheme('system', true)).toBe('dark')
    expect(resolveTheme('system', false)).toBe('light')
  })

  it('resolveTheme 对显式 dark/light 原样返回，忽略系统偏好', () => {
    expect(resolveTheme('dark', false)).toBe('dark')
    expect(resolveTheme('light', true)).toBe('light')
  })

  it('systemPrefersDark 在无 matchMedia 时回落 dark，不抛异常', () => {
    // jsdom 默认不实现 matchMedia：必须走「回落到 dark」分支而不是抛错
    const original = window.matchMedia
    // @ts-expect-error 刻意删除以触发回落分支
    delete window.matchMedia
    try {
      expect(() => systemPrefersDark()).not.toThrow()
      expect(systemPrefersDark()).toBe(true)
    } finally {
      window.matchMedia = original
    }
  })

  it('systemPrefersDark 依据 matchMedia 结果判定', () => {
    const original = window.matchMedia
    window.matchMedia = ((q: string) => ({
      matches: q.includes('light'),
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false
    })) as unknown as typeof window.matchMedia
    try {
      // matches(light)=true → 非深色
      expect(systemPrefersDark()).toBe(false)
    } finally {
      window.matchMedia = original
    }
  })

  it('readStoredTheme 读合法值', () => {
    localStorage.setItem(THEME_KEY, 'light')
    expect(readStoredTheme()).toBe('light')
  })

  it('readStoredTheme 遇非法值回落 system', () => {
    localStorage.setItem(THEME_KEY, 'neon-pink')
    expect(readStoredTheme()).toBe('system')
  })

  it('readStoredTheme 无值时回落 system', () => {
    expect(readStoredTheme()).toBe('system')
  })

  it('applyTheme 同步 data-theme 与 .dark class', () => {
    applyTheme('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    applyTheme('light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('setPreference 落库并立即应用到 DOM', () => {
    const store = useThemeStore()
    store.setPreference('light')
    expect(localStorage.getItem(THEME_KEY)).toBe('light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
  })

  it('toggle 在明暗之间翻转并落库', () => {
    const store = useThemeStore()
    store.setPreference('dark')
    store.toggle()
    expect(store.theme).toBe('light')
    expect(localStorage.getItem(THEME_KEY)).toBe('light')
    store.toggle()
    expect(store.theme).toBe('dark')
  })

  it('toggle 会自增 version，供图表依赖重算颜色', () => {
    const store = useThemeStore()
    const before = store.version
    store.toggle()
    expect(store.version).toBe(before + 1)
  })

  it('commit 会清空图表颜色缓存（否则图表仍用旧主题色）', async () => {
    const mod = await import('../../utils/chartTheme')
    const spy = vi.spyOn(mod, 'clearChartThemeCache')
    const store = useThemeStore()
    store.commit()
    expect(spy).toHaveBeenCalled()
    spy.mockRestore()
  })

  it('onSystemChange 仅在 system 模式下跟随系统', () => {
    const store = useThemeStore()
    store.setPreference('system')
    store.onSystemChange(false)
    expect(store.theme).toBe('light')

    // 显式选择 dark 后，系统偏好变化不再影响
    store.setPreference('dark')
    store.onSystemChange(false)
    expect(store.theme).toBe('dark')
  })

  it('init 返回解绑函数且应用一次主题', () => {
    const store = useThemeStore()
    store.setPreference('light')
    const dispose = store.init()
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(dispose).toBeTypeOf('function')
    dispose()
  })

  it('init 在无 matchMedia 时返回 no-op 解绑函数，不抛异常', () => {
    const original = window.matchMedia
    // @ts-expect-error 删除以触发回落分支
    delete window.matchMedia
    try {
      const store = useThemeStore()
      const dispose = store.init()
      expect(dispose).toBeTypeOf('function')
      expect(() => dispose()).not.toThrow()
    } finally {
      window.matchMedia = original
    }
  })

  it('init 挂 addEventListener 监听：系统偏好变化在 system 模式下生效', () => {
    const original = window.matchMedia
    let captured: ((e: { matches: boolean }) => void) | null = null
    const removed: string[] = []
    window.matchMedia = ((q: string) => ({
      matches: false,
      media: q,
      onchange: null,
      addEventListener: (_: string, cb: (e: { matches: boolean }) => void) => { captured = cb },
      removeEventListener: (ev: string) => { removed.push(ev) },
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false
    })) as unknown as typeof window.matchMedia
    try {
      const store = useThemeStore()
      store.setPreference('system')
      const dispose = store.init()
      expect(captured).toBeTypeOf('function')
      captured!({ matches: false })
      expect(store.theme).toBe('light')
      dispose()
      expect(removed).toContain('change')
    } finally {
      window.matchMedia = original
    }
  })

  it('init 对仅支持旧 addListener 的浏览器走兼容分支', () => {
    const original = window.matchMedia
    let legacyListenerSet = false
    let legacyRemoved = false
    window.matchMedia = ((q: string) => ({
      matches: true,
      media: q,
      onchange: null,
      // 无 addEventListener → 触发老 Safari 分支
      addListener: () => { legacyListenerSet = true },
      removeListener: () => { legacyRemoved = true },
      dispatchEvent: () => false
    })) as unknown as typeof window.matchMedia
    try {
      const store = useThemeStore()
      store.setPreference('system')
      const dispose = store.init()
      expect(legacyListenerSet).toBe(true)
      dispose()
      expect(legacyRemoved).toBe(true)
    } finally {
      window.matchMedia = original
    }
  })

  it('applyTheme 同时写入 colorScheme 便于原生控件跟随', () => {
    applyTheme('light')
    expect(document.documentElement.style.colorScheme).toBe('light')
    applyTheme('dark')
    expect(document.documentElement.style.colorScheme).toBe('dark')
  })

  it('preference 为 system 时 isDark 跟随系统偏好', () => {
    const store = useThemeStore()
    store.setPreference('system')
    store.onSystemChange(true)
    expect(store.isDark).toBe(true)
    store.onSystemChange(false)
    expect(store.isDark).toBe(false)
  })
})
