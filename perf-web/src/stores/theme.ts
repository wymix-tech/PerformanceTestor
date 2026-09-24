import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { clearChartThemeCache } from '@/utils/chartTheme'

/** 明暗主题（P3-3）持久化键，与 auth store 同风格直读 localStorage */
export const THEME_KEY = 'perf_theme'
/** 跟随系统时的主题别名：不落具体 dark/light，跟随偏好实时变化 */
export const THEME_SYSTEM = 'system' as const

export type ThemePreference = 'dark' | 'light' | typeof THEME_SYSTEM
/** 解析后真正生效的主题（只会是 dark / light） */
export type ResolvedTheme = 'dark' | 'light'

/**
 * 读系统偏好。
 * 兼容 SSR / 老浏览器：window.matchMedia 不存在时回落 dark（本项目默认主题），
 * 避免测试环境（jsdom 未桩 matchMedia）直接抛错。
 */
export function systemPrefersDark(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return true
  return window.matchMedia('(prefers-color-scheme: light)').matches === false
}

/** 把偏好解析为具体主题：system → 系统偏好，其余原样 */
export function resolveTheme(pref: ThemePreference, prefersDark = systemPrefersDark()): ResolvedTheme {
  if (pref === THEME_SYSTEM) return prefersDark ? 'dark' : 'light'
  return pref
}

/** 从 localStorage 读偏好；非法值一律回落 system（不抛错、不白屏） */
export function readStoredTheme(): ThemePreference {
  if (typeof localStorage === 'undefined') return THEME_SYSTEM
  const raw = localStorage.getItem(THEME_KEY)
  return raw === 'dark' || raw === 'light' || raw === 'system' ? raw : THEME_SYSTEM
}

/**
 * 把解析后的主题写到 <html data-theme>，并同步 .dark class
 * （Tailwind 暗色变体与第三方组件常按 .dark 判断，两者都写更稳）。
 */
export function applyTheme(theme: ResolvedTheme, doc: Document = document) {
  const root = doc.documentElement
  root.setAttribute('data-theme', theme)
  root.classList.toggle('dark', theme === 'dark')
  root.style.colorScheme = theme
}

export const useThemeStore = defineStore('theme', () => {
  const preference = ref<ThemePreference>(readStoredTheme())
  const prefersDark = ref(systemPrefersDark())

  const theme = computed<ResolvedTheme>(() => resolveTheme(preference.value, prefersDark.value))
  /** 供模板/测试用的简写布尔量 */
  const isDark = computed(() => theme.value === 'dark')

  /** 主题版本号：每次切换自增，供图表 computed 依赖以便重算颜色缓存 */
  const version = ref(0)

  /** 应用到 DOM 并（可选）持久化；system 也落库，便于刷新后保持「跟随系统」 */
  function commit() {
    applyTheme(theme.value)
    // CSS 变量已换值，图表颜色缓存必须失效；version 自增同时触发依赖它的图表重算
    clearChartThemeCache()
    version.value++
  }

  function setPreference(pref: ThemePreference) {
    preference.value = pref
    if (typeof localStorage !== 'undefined') localStorage.setItem(THEME_KEY, pref)
    commit()
  }

  /** 明暗二态切换：当前生效为 dark → light，否则 → dark（显式落库，退出跟随系统） */
  function toggle() {
    setPreference(theme.value === 'dark' ? 'light' : 'dark')
  }

  /** 系统偏好变化时仅当处于 system 模式才跟随 */
  function onSystemChange(dark: boolean) {
    prefersDark.value = dark
    if (preference.value === THEME_SYSTEM) commit()
  }

  /** 初始化：应用一次并挂系统偏好监听，返回解绑函数（供组件 onUnmounted 调用） */
  function init(): () => void {
    commit()
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
    const mql = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = (e: MediaQueryListEvent) => onSystemChange(e.matches)
    // 老 Safari 只有 addListener；两者都兼容
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', handler)
      return () => mql.removeEventListener('change', handler)
    }
    if (typeof (mql as MediaQueryList).addListener === 'function') {
      ;(mql as MediaQueryList).addListener(handler)
      return () => (mql as MediaQueryList).removeListener(handler)
    }
    return () => {}
  }

  return { preference, theme, isDark, version, setPreference, toggle, onSystemChange, init, commit }
})
