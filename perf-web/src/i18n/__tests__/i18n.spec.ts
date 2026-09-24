import { describe, it, expect, beforeEach } from 'vitest'
import zhCN from '../locales/zh-CN'
import enUS from '../locales/en-US'
import { readStoredLocale, persistLocale, applyLocaleToDocument, LOCALE_KEY, SUPPORTED_LOCALES } from '../index'

/**
 * 文案国际化（P3-1）单测。
 *
 * 关键守卫：中英 key 集合必须完全一致——缺 key 时 vue-i18n 会静默回落中文，
 * 只有当真人切到英文看到中英混杂才会暴露，必须靠断言提前锁死。
 */
function flatten(obj: Record<string, unknown>, prefix = ''): string[] {
  const out: string[] = []
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') out.push(...flatten(v as Record<string, unknown>, key))
    else out.push(key)
  }
  return out.sort()
}

describe('i18n messages', () => {
  it('中英 key 集合完全一致', () => {
    expect(flatten(zhCN as Record<string, unknown>)).toEqual(flatten(enUS as Record<string, unknown>))
  })

  it('key 集合非空', () => {
    expect(flatten(zhCN as Record<string, unknown>).length).toBeGreaterThan(10)
  })

  it('不存在空字符串文案', () => {
    for (const [k, v] of Object.entries(flattenValues(zhCN))) {
      expect(v.trim(), `中文空文案: ${k}`).not.toBe('')
    }
    for (const [k, v] of Object.entries(flattenValues(enUS))) {
      expect(v.trim(), `英文空文案: ${k}`).not.toBe('')
    }
  })

  it('含占位符的 key 在中英两侧占位符一致', () => {
    const zh = flattenValues(zhCN)
    const en = flattenValues(enUS)
    for (const key of Object.keys(zh)) {
      const zhVars = [...zh[key].matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
      const enVars = [...en[key].matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
      expect(enVars, `占位符不一致: ${key}`).toEqual(zhVars)
    }
  })
})

function flattenValues(obj: Record<string, unknown>, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') Object.assign(out, flattenValues(v as Record<string, unknown>, key))
    else out[key] = String(v)
  }
  return out
}

describe('locale persistence', () => {
  beforeEach(() => localStorage.clear())

  it('读合法语言', () => {
    localStorage.setItem(LOCALE_KEY, 'en-US')
    expect(readStoredLocale()).toBe('en-US')
  })

  it('非法语言回落默认 zh-CN', () => {
    localStorage.setItem(LOCALE_KEY, 'fr-FR')
    expect(readStoredLocale()).toBe('zh-CN')
  })

  it('无值时回落默认 zh-CN', () => {
    expect(readStoredLocale()).toBe('zh-CN')
  })

  it('persistLocale 写入并同步 html lang', () => {
    persistLocale('en-US')
    expect(localStorage.getItem(LOCALE_KEY)).toBe('en-US')
    applyLocaleToDocument('en-US')
    expect(document.documentElement.getAttribute('lang')).toBe('en-US')
  })

  it('支持语言清单为 zh-CN / en-US', () => {
    expect([...SUPPORTED_LOCALES]).toEqual(['zh-CN', 'en-US'])
  })
})
