import { ref } from 'vue'

/**
 * 快速开始向导的跨页面导航状态。
 *
 * 用途：用户在向导内会频繁跳转到压测中心/调用链/性能优化/AI 助手等页面查看细节，
 * 而侧边栏菜单没有「快速开始」入口，一旦点出去就回不来。本模块把向导进度持久化到
 * sessionStorage，并由 MainLayout 顶栏渲染一个全局「返回快速开始」按钮，
 * 保证任何页面都能一步回到原步骤。
 *
 * 说明：用 sessionStorage 而非 localStorage——向导是「一次会话内」的引导流程，
 * 关掉标签页即视为结束，不应在新会话里复活旧状态。
 */

/** 向导快照（字段可缺省，恢复时按存在性处理） */
export interface QuickStartSnapshot {
  /** 当前步骤下标（0 起） */
  step: number
  /** 步骤标题，用于返回按钮文案 */
  stepTitle: string
  repoId: number
  connId: number
  /** 各步骤完成标记（只认本轮操作） */
  sessionDone: boolean[]
  stressTask: any
  report: any
  capacityText: string
  traces: any[]
  issues: any[]
  savedAt: number
}

const STORAGE_KEY = 'perf_quickstart_session'

/**
 * 是否存在进行中的向导会话（响应式）。
 * MainLayout 据此决定是否显示返回按钮；当前已在向导页时也由它判断是否隐藏。
 */
export const hasQuickStart = ref(false)

/** 当前快照的步骤标题（响应式，供按钮文案使用） */
export const quickStartStepTitle = ref('')

export function saveQuickStart(snapshot: QuickStartSnapshot): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot))
    hasQuickStart.value = true
    quickStartStepTitle.value = snapshot.stepTitle || ''
  } catch {
    // 隐私模式 / 配额超限：降级为不持久化，但不影响当前页内使用
  }
}

export function loadQuickStart(): QuickStartSnapshot | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as QuickStartSnapshot) : null
  } catch {
    return null
  }
}

export function clearQuickStart(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // 忽略
  }
  hasQuickStart.value = false
  quickStartStepTitle.value = ''
}

/** 从既有缓存同步响应式标记（MainLayout 挂载时调用，避免刷新后按钮丢失） */
export function syncQuickStartFlag(): void {
  const snap = loadQuickStart()
  hasQuickStart.value = snap !== null
  quickStartStepTitle.value = snap?.stepTitle || ''
}
