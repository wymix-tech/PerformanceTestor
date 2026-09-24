import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN'
import enUS from './locales/en-US'

/** 语言持久化键（与 theme/auth store 同风格直读 localStorage） */
export const LOCALE_KEY = 'perf_locale'

export const SUPPORTED_LOCALES = ['zh-CN', 'en-US'] as const
export type AppLocale = (typeof SUPPORTED_LOCALES)[number]

export const DEFAULT_LOCALE: AppLocale = 'zh-CN'

/** 非法/缺失值一律回落默认语言，不抛错 */
export function readStoredLocale(): AppLocale {
  if (typeof localStorage === 'undefined') return DEFAULT_LOCALE
  const raw = localStorage.getItem(LOCALE_KEY)
  return (SUPPORTED_LOCALES as readonly string[]).includes(raw ?? '') ? (raw as AppLocale) : DEFAULT_LOCALE
}

export function persistLocale(locale: AppLocale) {
  if (typeof localStorage === 'undefined') return
  localStorage.setItem(LOCALE_KEY, locale)
}

/** 同步 <html lang>，影响浏览器翻译与读屏发音 */
export function applyLocaleToDocument(locale: AppLocale, doc: Document = document) {
  doc.documentElement.setAttribute('lang', locale)
}

const i18n = createI18n({
  // legacy: false 才支持 setup 里 useI18n() 组合式用法
  legacy: false,
  locale: readStoredLocale(),
  fallbackLocale: DEFAULT_LOCALE,
  // 生产环境关闭「缺失 key」控制台告警噪音；开发下保留便于补文案
  missingWarn: import.meta.env?.DEV ?? false,
  fallbackWarn: import.meta.env?.DEV ?? false,
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS
  }
})

applyLocaleToDocument(readStoredLocale())

/** 切换语言：更新 i18n 实例 + 持久化 + 同步 html lang */
export function setLocale(locale: AppLocale) {
  const global = i18n.global as unknown as { locale: AppLocale }
  global.locale = locale
  persistLocale(locale)
  applyLocaleToDocument(locale)
}

export default i18n
