// 全局共用的小工具函数收敛处（状态样式 / 时间格式化）

/** 状态徽章通用兜底样式 */
const FALLBACK = 'text-fg-sub bg-white/5'

/** 压测/灌数据任务状态（后端真实值：pending/running/finished/failed） */
export function taskStatusClass(s: string): string {
  const map: Record<string, string> = {
    running: 'text-neon-blue bg-neon-blue/10',
    finished: 'text-neon-green bg-neon-green/10',
    // 数据已灌入但灌后校验不通过（如行数核对/孤儿抽检失败）：不伪装成功，也不并入失败
    finished_verify_failed: 'text-amber-400 bg-amber-400/10',
    failed: 'text-red-400 bg-red-400/10'
  }
  return map[s] || FALLBACK
}

/** 性能问题状态（后端真实值：pending/analyzed/fixed/resolved） */
export function issueStatusClass(s: string): string {
  const map: Record<string, string> = {
    pending: 'text-amber-400 bg-amber-400/10',
    analyzed: 'text-neon-green bg-neon-green/10',
    // 已修复待验证
    fixed: 'text-neon-blue bg-neon-blue/10',
    // 重放验证达标、自动关闭
    resolved: 'text-neon-green bg-neon-green/15',
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

/** 数据集实例状态（ptde 状态机：总方案 §12.2 十态，取消为主动处置非失败） */
export function datasetStatusClass(s: string): string {
  const map: Record<string, string> = {
    PLANNED: 'text-fg-sub bg-white/5',
    GENERATING: 'text-neon-blue bg-neon-blue/10',
    LOADING: 'text-neon-blue bg-neon-blue/10',
    VERIFYING: 'text-neon-purple bg-neon-purple/10',
    READY: 'text-neon-green bg-neon-green/10',
    PARTIAL: 'text-amber-400 bg-amber-400/10',
    FAILED: 'text-red-400 bg-red-400/10',
    CLEANING: 'text-neon-blue bg-neon-blue/10',
    CLEANED: 'text-fg-sub bg-white/5',
    CANCELLED: 'text-fg-sub bg-white/5'
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
