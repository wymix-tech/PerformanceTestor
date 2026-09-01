<template>
  <div>
    <div class="page-title">
      <Gauge class="w-5 h-5 text-neon-green" />
      压测中心
      <span class="text-xs font-normal text-fg-sub ml-2">压测 · 容量评估 · 扩容建议 · 数据准备</span>
    </div>

    <div class="flex gap-1 mb-4">
      <button
        v-for="tab in tabs" :key="tab.key"
        class="px-5 py-2 rounded-lg text-sm transition-all cursor-pointer"
        :class="activeTab === tab.key ? 'bg-neon-green/15 text-neon-green border border-neon-green/30' : 'text-fg-sub border border-white/10 hover:text-fg-main'"
        @click="activeTab = tab.key"
      >{{ tab.label }}</button>
    </div>

    <!-- 压测 Tab -->
    <div v-if="activeTab === 'stress'" class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">新建压测任务</div>
        <div v-if="rerunFilled" class="text-xs text-neon-green bg-neon-green/10 border border-neon-green/20 rounded px-3 py-2 mb-3">
          已填入上次压测配置，可调整后点击「开始压测」重新发起
        </div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">项目</label>
            <select v-model="repoId" class="input-select w-full" @change="loadEndpoints">
              <option :value="0">— 手动填写 URL —</option>
              <option v-for="r in repoStore.repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
            </select>
          </div>
          <div v-if="repoId > 0">
            <label class="text-xs text-fg-sub flex items-center justify-between">
              <span>{{ currentRepoType === 'git' ? '分支与接口' : '接口' }}</span>
              <button class="text-neon-blue hover:underline text-[11px] cursor-pointer" :disabled="resyncing" @click="resyncAndScan">
                {{ resyncing ? '同步中…' : '↻ 与源码重新同步' }}
              </button>
            </label>
            <div v-if="currentRepoType === 'git'" class="flex gap-2 mb-2">
              <select
                :key="branchOptions.length + '-' + currentRepoBranch"
                class="input-select flex-1"
                :value="currentRepoBranch"
                :disabled="resyncing"
                @focus="loadBranchOptions"
                @change="changeBranch(($event.target as HTMLSelectElement).value)"
              >
                <!-- 已加载列表：渲染全部远端分支；当前分支不在列表中时补当前项 -->
                <template v-if="branchOptions.length">
                  <option v-for="b in branchOptions" :key="b" :value="b">{{ b }}</option>
                  <option v-if="currentRepoBranch && !branchOptions.includes(currentRepoBranch)" :value="currentRepoBranch">
                    {{ currentRepoBranch }}（当前）
                  </option>
                </template>
                <option v-else value="">{{ currentRepoBranch || '分支加载中…' }}</option>
              </select>
            </div>
          </div>
          <!-- 压测参数公共组件（接口选择/URL 拼接/并发/时长/预热/QPS/分档） -->
          <StressForm
            ref="stressFormRef"
            :endpoints="endpoints"
            :auto-base-url="autoBaseUrl"
            :disabled="running"
            submit-label="开始压测"
            @submit="startStress"
            @endpoint-selected="onEndpointSelected"
          />
        </div>
      </div>

      <!-- 最近任务（含 3s 轮询刷新） -->
      <StressTaskList ref="taskListRef" @select="showReport" />
    </div>

    <!-- 压测报告（公共组件；版本对比经 #extra 插槽注入） -->
    <StressReport
      v-if="activeTab === 'stress' && report"
      :report="report"
      :samples="samples"
      :capacity="capacity"
      class="mt-4"
      @rerun="rerunTask"
      @run-capacity="runCapacity"
    >
      <template #extra>
        <!-- 版本对比：选择同应用历史任务，比对核心指标（版本间性能回归/提升一目了然） -->
        <div v-if="compareCandidates.length" class="mb-4 p-4 rounded-lg bg-white/5 border border-white/10">
          <div class="flex items-center gap-3 mb-3">
            <span class="text-xs text-fg-sub shrink-0">版本对比</span>
            <select v-model.number="compareTaskId" class="input-select flex-1" @change="loadCompare(compareTaskId)">
              <option :value="0">选择要对比的历史任务…</option>
              <option v-for="t in compareCandidates" :key="t.id" :value="t.id">
                {{ compareLabel(t) }}
              </option>
            </select>
            <span v-if="compareLoading" class="text-[11px] text-fg-sub">加载中…</span>
          </div>
          <div v-if="compareReport" class="overflow-x-auto">
            <table class="w-full text-xs">
              <thead>
                <tr class="text-fg-sub border-b border-white/10">
                  <th class="text-left py-1.5 font-normal">指标</th>
                  <th class="text-right py-1.5 font-normal">基线 {{ compareReport.task.appVersion ? 'v' + compareReport.task.appVersion : '未知版本' }}</th>
                  <th class="text-right py-1.5 font-normal">当前 {{ report.task.appVersion ? 'v' + report.task.appVersion : '未知版本' }}</th>
                  <th class="text-right py-1.5 font-normal">变化</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in compareRows" :key="row.label" class="border-b border-white/5">
                  <td class="py-1.5 text-fg-dim">{{ row.label }}</td>
                  <td class="text-right text-fg-sub">{{ row.base }}</td>
                  <td class="text-right text-fg-main font-medium">{{ row.cur }}</td>
                  <td class="text-right font-medium" :class="row.cls || 'text-fg-sub'">{{ row.delta || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </StressReport>

    <!-- 容量建议 Tab -->
    <CapacityAdvicePanel v-else-if="activeTab === 'capacity'" />

    <!-- 数据准备 Tab：与快速开始向导「灌注数据」共用 DataGenForm，能力完全一致 -->
    <div v-else-if="activeTab === 'datagen'">
      <DataGenForm :poll="true" />
    </div>
    <!-- 定时压测 Tab：调度管理 + 版本基线对比（回归门禁） -->
    <SchedulePanel v-else-if="activeTab === 'schedule'" :template="stressFormRef?.snapshot?.() || null" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Gauge } from 'lucide-vue-next'
import StressForm from '../components/stress/StressForm.vue'
import StressReport from '../components/stress/StressReport.vue'
import StressTaskList from '../components/stress/StressTaskList.vue'
import CapacityAdvicePanel from '../components/stress/CapacityAdvicePanel.vue'
import SchedulePanel from '../components/stress/SchedulePanel.vue'
import DataGenForm from '../components/datagen/DataGenForm.vue'
import { fmtTime } from '../utils/format'
import { stressApi, sourceApi, appApi, capacityApi } from '../api'
import { useRepoStore } from '../stores/repoStore'
import { useRoute } from 'vue-router'

const route = useRoute()
const repoStore = useRepoStore()

const tabs = [
  { key: 'datagen', label: '数据准备' },
  { key: 'stress', label: '压测' },
  { key: 'capacity', label: '容量建议' },
  { key: 'schedule', label: '定时压测' }
]
const activeTab = ref(['stress', 'capacity', 'datagen'].includes(String(route.query.tab))
  ? String(route.query.tab) : 'stress')
// 支持外部（总览快速开始引导等）通过 /stress?tab=datagen 直达指定 Tab
watch(() => route.query.tab, (t) => {
  if (t && ['stress', 'capacity', 'datagen', 'schedule'].includes(String(t))) activeTab.value = String(t)
})
const running = ref(false)
const report = ref<Record<string, any> | null>(null)
const samples = ref<Array<Record<string, any>>>([])
const capacity = ref<Record<string, any> | null>(null)
/** 任务列表子组件引用：发起压测后立即刷新 */
const taskListRef = ref<InstanceType<typeof StressTaskList> | null>(null)

// ---------- 项目选择与接口扫描（视图特有逻辑，表单细节在 StressForm 公共组件） ----------
const repoId = ref(0)
const endpoints = ref<Array<Record<string, any>>>([])
const appInstances = ref<Array<Record<string, any>>>([])
const stressFormRef = ref<InstanceType<typeof StressForm> | null>(null)
// 任务名（选中接口时自动派生）
const taskName = ref('')

/** 当前项目归属应用（压测任务 appName 取值） */
const currentRepoApp = computed(() => {
  const r = repoStore.repos.find((x) => x.id === repoId.value)
  if (!r) return ''
  return r.appName || r.repoName || ''
})

/** 当前项目类型：分支切换框仅对 git 项目展示（zip/local 无分支概念） */
const currentRepoType = computed(() => {
  const r = repoStore.repos.find((x) => x.id === repoId.value)
  return r ? (r.repoType || '') : ''
})

/** agent 上报的服务地址（StressForm 自动预填） */
const autoBaseUrl = computed(() => {
  const inst = appInstances.value.find((x) => x.appName === currentRepoApp.value)
  if (!inst || !inst.appHost) return ''
  return 'http://' + inst.appHost + (inst.appPort ? ':' + inst.appPort : '')
})

async function loadEndpoints() {
  endpoints.value = []
  branchOptions.value = []
  if (!repoId.value) return
  try {
    const r = await sourceApi.endpoints(repoId.value)
    endpoints.value = r.data || []
  } catch { /* 扫描失败保持空列表 */ }
  // 选定 git 项目后自动同步分支列表（zip/local 项目无分支，不请求）
  if (currentRepoType.value === 'git') {
    loadBranchOptions()
  }
}

// 与源码重新同步：重新拉取源码 → 重建索引 → 重扫接口（源码更新后点击）
const resyncing = ref(false)
async function resyncAndScan() {
  if (!repoId.value || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.resync(repoId.value)
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

// 分支切换：当前仓库分支 + 分支列表缓存 + 切换（重同步 + 重扫接口）
const currentRepoBranch = computed(() => {
  const r = repoStore.repos.find((x) => x.id === repoId.value)
  return r ? r.branch || '' : ''
})
const branchOptions = ref<string[]>([])
async function loadBranchOptions() {
  if (!repoId.value) return
  try {
    const r = await sourceApi.branches(repoId.value)
    branchOptions.value = r.data || []
  } catch { /* 拉取失败保持空 */ }
}
async function changeBranch(branch: string) {
  if (!branch || branch === currentRepoBranch.value || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.switchBranch(repoId.value, branch)
    await repoStore.reload()
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

/** 选中接口回调：自动派生任务名 */
function onEndpointSelected(e: Record<string, any>) {
  if (!taskName.value) {
    taskName.value = `${e.className}#${e.methodName || e.path}`
  }
}

/** 发起压测：StressForm 提交标准请求体，本视图补充任务名与应用归属 */
async function startStress(payload: Record<string, unknown>) {
  const body = {
    ...payload,
    taskName: taskName.value || undefined,
    // 压测任务归属应用 = 项目的应用
    appName: currentRepoApp.value || undefined
  }
  running.value = true
  try {
    await stressApi.create(body)
    await taskListRef.value?.refresh()
    setTimeout(() => { running.value = false }, 3000)
  } catch {
    running.value = false
  }
}

async function showReport(id: number) {
  const r = await stressApi.report(id)
  report.value = r.data
  capacity.value = r.data.result?.capacity || null
  // 切换报告时重置版本对比状态
  compareTaskId.value = 0
  compareReport.value = null
  const s = await stressApi.samples(id)
  samples.value = s.data
}

// ---------- 版本对比 ----------
/** 任务列表数据由 StressTaskList 子组件持有并轮询刷新 */
const tasks = computed(() => taskListRef.value?.tasks ?? [])

// 可对比候选：同应用（或双方均无应用）的其他已完成任务
const compareCandidates = computed(() => {
  const cur = report.value?.task
  if (!cur) return []
  return tasks.value.filter((t: any) =>
    t.id !== cur.id && t.status === 'finished' &&
    (!cur.appName || !t.appName || t.appName === cur.appName))
})

const compareTaskId = ref(0)
const compareReport = ref<any>(null)
const compareLoading = ref(false)

// 候选项文案：版本 + 时间 + 并发
function compareLabel(t: any) {
  const v = t.appVersion ? 'v' + t.appVersion : '未带版本'
  const time = t.finishTime ? fmtTime(t.finishTime) : fmtTime(t.createTime)
  return `${v} · ${time} · ${t.concurrency}并发（${t.taskName || t.targetUrl}）`
}

async function loadCompare(id: number) {
  compareReport.value = null
  if (!id) return
  compareLoading.value = true
  try {
    const r = await stressApi.report(id)
    compareReport.value = r.data
  } catch { /* 拉取失败保持空 */ } finally {
    compareLoading.value = false
  }
}

// 相对变化百分比（基线 → 当前）
function pct(cur: number | null | undefined, base: number | null | undefined): string {
  if (cur == null || base == null || base === 0) return ''
  const v = ((cur - base) / base) * 100
  return (v > 0 ? '+' : '') + v.toFixed(1) + '%'
}

// 对比行：QPS 越高越好，延迟/错误率越低越好
const compareRows = computed(() => {
  const cur = report.value?.result
  const base = compareReport.value?.result
  if (!cur || !base) return []
  const defs = [
    { label: 'QPS', cur: cur.qps, base: base.qps, higherBetter: true },
    { label: 'P50 (ms)', cur: cur.p50Ms, base: base.p50Ms, higherBetter: false },
    { label: 'P95 (ms)', cur: cur.p95Ms, base: base.p95Ms, higherBetter: false },
    { label: 'P99 (ms)', cur: cur.p99Ms, base: base.p99Ms, higherBetter: false },
    { label: '错误率 (%)', cur: cur.errorRate, base: base.errorRate, higherBetter: false },
  ]
  return defs.map((d) => {
    const delta = pct(d.cur, d.base)
    const improved = delta !== '' && (d.higherBetter === delta.startsWith('+'))
    const cls = delta === '' ? '' : (improved ? 'text-neon-green' : 'text-red-400')
    return { label: d.label, cur: d.cur ?? '—', base: d.base ?? '—', delta: improved ? delta + ' ↑' : (delta ? delta + ' ↓' : ''), cls }
  })
})

// "再来一次"回填提示（短暂展示）
const rerunFilled = ref(false)

// 再来一次：把已完成任务的配置回填到表单，由用户点击「开始压测」重新发起
function rerunTask() {
  const t = report.value?.task
  if (!t || !stressFormRef.value) return
  stressFormRef.value.fillFrom(t, report.value?.result?.stagePoints || [])
  // 项目联动：按任务应用匹配项目（恢复接口列表；无匹配则手动 URL 模式）
  const repo = repoStore.repos.find((r) => t.appName && r.appName === t.appName)
  if (repo) {
    repoId.value = repo.id
    loadEndpoints()
  } else {
    // 无匹配项目：退回手动 URL 模式（targetUrl 已完整填入，可直接压测）
    repoId.value = 0
    endpoints.value = []
    branchOptions.value = []
  }
  activeTab.value = 'stress'
  rerunFilled.value = true
  setTimeout(() => { rerunFilled.value = false }, 6000)
}

async function runCapacity() {
  if (!report.value?.task?.id) return
  const r = await capacityApi.analyze({ taskId: report.value.task.id })
  capacity.value = r.data
  // 重新读取报告使 capacity 落入报告
  await showReport(report.value.task.id)
}

onMounted(() => {
  repoStore.load()
  loadApps()
})

/** 应用实例列表（StressForm 自动预填服务地址用） */
async function loadApps() {
  try {
    const r = await appApi.list()
    appInstances.value = r.data || []
  } catch { /* 应用列表失败不阻断 */ }
}
</script>
