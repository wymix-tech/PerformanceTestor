import { describe, it, expect } from 'vitest'
import {
  fmtTime,
  taskStatusClass,
  issueStatusClass,
  repoStatusClass
} from '../format'

/** 状态样式兜底值（与 format.ts 中 FALLBACK 一致） */
const FALLBACK = 'text-fg-sub bg-white/5'

describe('fmtTime', () => {
  it('formats a Date object into a readable string', () => {
    const out = fmtTime(new Date(2026, 0, 15, 14, 30, 0))
    expect(out).toBeTruthy()
    // 中文环境本地化输出含年/月/日三部分（具体分隔符随 ICU 版本，不作硬断言）
    expect(out).toContain('2026')
    expect(out).toContain('14:30:00')
  })

  it('accepts epoch millis', () => {
    const d = new Date(2026, 0, 15, 14, 30, 0)
    expect(fmtTime(d.getTime())).toBe(fmtTime(d))
  })

  it('accepts ISO string', () => {
    expect(fmtTime('2026-01-15T14:30:00')).toBeTruthy()
  })

  it('returns empty string for missing value', () => {
    expect(fmtTime('')).toBe('')
    expect(fmtTime(null)).toBe('')
    expect(fmtTime(undefined)).toBe('')
  })

  it('returns empty string for unparsable input', () => {
    expect(fmtTime('not-a-date')).toBe('')
  })

  it('treats epoch 0 as a valid timestamp', () => {
    // 0 是合法时间戳，不得被判为「无值」
    expect(fmtTime(0)).not.toBe('')
  })
})

describe('taskStatusClass', () => {
  it('maps backend statuses to their styles', () => {
    expect(taskStatusClass('running')).toBe('text-neon-blue bg-neon-blue/10')
    expect(taskStatusClass('finished')).toBe('text-neon-green bg-neon-green/10')
    expect(taskStatusClass('failed')).toBe('text-red-400 bg-red-400/10')
  })

  it('falls back for pending or unknown status', () => {
    expect(taskStatusClass('pending')).toBe(FALLBACK)
    expect(taskStatusClass('whatever')).toBe(FALLBACK)
  })
})

describe('issueStatusClass', () => {
  it('maps the full issue lifecycle', () => {
    expect(issueStatusClass('pending')).toBe('text-amber-400 bg-amber-400/10')
    expect(issueStatusClass('analyzed')).toBe('text-neon-green bg-neon-green/10')
    expect(issueStatusClass('fixed')).toBe('text-neon-blue bg-neon-blue/10')
    expect(issueStatusClass('resolved')).toBe('text-neon-green bg-neon-green/15')
    expect(issueStatusClass('failed')).toBe('text-red-400 bg-red-400/10')
  })

  it('falls back for unknown status', () => {
    expect(issueStatusClass('unknown')).toBe(FALLBACK)
  })
})

describe('repoStatusClass', () => {
  it('maps import states', () => {
    expect(repoStatusClass('importing')).toBe('text-neon-blue bg-neon-blue/10')
    expect(repoStatusClass('ready')).toBe('text-neon-green bg-neon-green/10')
    expect(repoStatusClass('failed')).toBe('text-red-400 bg-red-400/10')
  })

  it('falls back for unknown status', () => {
    expect(repoStatusClass('nope')).toBe(FALLBACK)
  })
})
