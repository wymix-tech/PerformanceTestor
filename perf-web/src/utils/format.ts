// 全局共用的小工具函数收敛处（状态样式 / 时间格式化）

/** 状态徽章通用兜底样式 */
const FALLBACK = 'text-fg-sub bg-white/5'

/** 压测/灌数据任务状态（后端真实值：pending/running/finished/failed） */
export function taskStatusClass(s: string): string {
  const map: Record<string, string> = {
    running: 'text-neon-blue bg-neon-blue/10',
    finished: 'text-neon-green bg-neon-green/10',
    failed: 'text-red-400 bg-red-400/10'
  }
  return map[s] || FALLBACK
}

/** 性能问题状态（后端真实值：pending/analyzed/failed） */
export function issueStatusClass(s: string): string {
  const map: Record<string, string> = {
    pending: 'text-amber-400 bg-amber-400/10',
    analyzed: 'text-neon-green bg-neon-green/10',
    failed: 'text-red-400 bg-red-400/10'
  }
  return map[s] || FALLBACK
}

/** 源码仓库导入状态（后端真实值：importing/ready/failed） */
export function repoStatusClass(s: string): string {
  const map: Record<string, string> = {
    importing: 'text-neon-blue bg-neon-blue/10',
    ready: 'text-neon-green bg-neon-green/10',
    failed: 'text-red-400 bg-red-400/10'
  }
  return map[s] || FALLBACK
}

/** 本地化时间格式化（无效值返回空串） */
export function fmtTime(t: string | number | Date | undefined | null): string {
  if (!t && t !== 0) return ''
  const d = typeof t === 'object' ? t : new Date(typeof t === 'number' ? t : String(t))
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString('zh-CN', { hour12: false })
}
