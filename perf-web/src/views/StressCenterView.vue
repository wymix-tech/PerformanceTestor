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
        class="px-5 py-2 rounded-lg text-sm transition-all"
        :class="activeTab === tab.key ? 'bg-neon-green/15 text-neon-green border border-neon-green/30' : 'text-fg-sub border border-white/10 hover:text-fg-main'"
        @click="activeTab = tab.key"
      >{{ tab.label }}</button>
    </div>

    <!-- 压测 Tab -->
    <div v-if="activeTab === 'stress'" class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">新建压测任务</div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">项目<span class="ml-2 font-normal">项目即应用，归属自动确定</span></label>
            <select v-model="form.repoId" class="input-select w-full" @change="loadEndpoints">
              <option :value="0">— 手动填写 URL —</option>
              <option v-for="r in repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
            </select>
          </div>
          <div v-if="form.repoId > 0">
            <label class="text-xs text-fg-sub flex items-center justify-between">
              <span>分支与接口</span>
              <button class="text-neon-blue hover:underline text-[11px]" :disabled="resyncing" @click="resyncAndScan">
                {{ resyncing ? '同步中…' : '↻ 与源码重新同步' }}
              </button>
            </label>
            <div class="flex gap-2 mb-2">
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
            <select v-model="form.endpointIdx" class="input-select w-full" @change="applyEndpoint">
              <option :value="-1">— 选择接口（{{ endpoints.length }} 个）—</option>
              <option v-for="(e, i) in endpoints" :key="i" :value="i">
                [{{ e.httpMethod }}] {{ e.path }}（{{ e.className }}#{{ e.methodName || '?' }}）
              </option>
            </select>
          </div>
          <div>
            <label class="text-xs text-fg-sub">服务地址（host:port，与接口路径拼接）</label>
            <input v-model="form.baseUrl" class="input-text w-full" placeholder="http://127.0.0.1:8081" @input="composeUrl()" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">目标 URL（{{ form.repoId > 0 ? '自动拼接，可微调' : 'http/https' }}）</label>
            <input v-model="form.targetUrl" class="input-text w-full" placeholder="http://host:port/api/xxx" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-fg-sub">方法</label>
              <select v-model="form.method" class="input-select w-full"><option>GET</option><option>POST</option></select>
            </div>
            <div>
              <label class="text-xs text-fg-sub">引擎</label>
              <select v-model="form.engineType" class="input-select w-full"><option>http</option><option>jmeter</option></select>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-fg-sub">并发数</label>
              <input v-model.number="form.concurrency" type="number" class="input-text w-full" min="1" max="1000" />
            </div>
            <div>
              <label class="text-xs text-fg-sub">时长（秒）</label>
              <input v-model.number="form.durationSec" type="number" class="input-text w-full" min="1" max="3600" />
            </div>
          </div>
          <div>
            <label class="text-xs text-fg-sub">分档并发（逗号分隔，容量曲线）</label>
            <input v-model="form.stageConcurrencies" class="input-text w-full" placeholder="如 10,50,100" />
          </div>
          <button class="btn-primary w-full justify-center" :disabled="running" @click="startStress">
            {{ running ? '压测进行中…' : '开始压测' }}
          </button>
        </div>
      </div>

      <div class="glass-card p-5 xl:col-span-2">
        <div class="text-sm font-medium mb-3 flex items-center justify-between">
          <span>最近任务</span>
          <button class="text-xs text-neon-blue hover:underline" @click="loadTasks">刷新</button>
        </div>
        <div v-if="!tasks.length" class="text-xs text-fg-sub py-8 text-center">暂无压测任务</div>
        <div v-else class="space-y-2 max-h-[430px] overflow-y-auto pr-1">
          <div v-for="t in tasks" :key="t.id" class="px-4 py-3 rounded-lg bg-white/5 hover:bg-white/10 transition-all cursor-pointer"
            @click="showReport(t.id)">
            <div class="flex items-center justify-between">
              <div class="text-sm text-fg-main truncate">{{ t.taskName || t.targetUrl }}</div>
              <span class="flex items-center gap-2 shrink-0">
                <span v-if="t.appName" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue">{{ t.appName }}</span>
                <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(t.status)">{{ t.status }}</span>
              </span>
            </div>
            <div class="flex items-center justify-between mt-2 text-[11px] text-fg-sub">
              <span>{{ t.concurrency }} 并发 · {{ t.durationSec }}s · {{ t.engineType }}</span>
              <span v-if="t.finishTime">{{ fmtTime(t.finishTime) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 压测报告 -->
    <div v-if="activeTab === 'stress' && report" class="glass-card p-5 mt-4">
      <div class="text-sm font-medium mb-4 flex items-center justify-between">
        <span>压测报告 — {{ report.task.taskName || report.task.targetUrl }}</span>
        <button class="text-xs text-neon-blue hover:underline" @click="runCapacity">容量评估 →</button>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">
        <div class="metric-card"><div class="text-xs text-fg-sub">总请求</div><div class="text-lg font-bold">{{ report.result?.totalRequests ?? '—' }}</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">QPS</div><div class="text-lg font-bold neon-text">{{ report.result?.qps ?? '—' }}</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">P50</div><div class="text-lg font-bold">{{ report.result?.p50Ms ?? '—' }}ms</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">P95</div><div class="text-lg font-bold">{{ report.result?.p95Ms ?? '—' }}ms</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">P99</div><div class="text-lg font-bold">{{ report.result?.p99Ms ?? '—' }}ms</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">错误率</div><div class="text-lg font-bold" :class="(report.result?.errorRate ?? 0) > 5 ? 'text-red-400' : 'text-neon-green'">{{ report.result?.errorRate ?? 0 }}%</div></div>
      </div>
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div>
          <div class="text-xs text-fg-sub mb-2">QPS 实时曲线</div>
          <MetricChart :option="sampleOption" height="220px" />
        </div>
        <div>
          <div class="text-xs text-fg-sub mb-2">分档并发容量曲线（QPS / P95）</div>
          <MetricChart :option="stageOption" height="220px" />
        </div>
      </div>
      <div v-if="capacity" class="mt-4 p-4 rounded-lg bg-neon-green/5 border border-neon-green/20">
        <div class="text-sm font-medium text-neon-green mb-2">容量评估：目标 P95 ≤ {{ capacity.targetP95Ms }}ms</div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <div>最大可承受并发：<b class="text-lg text-neon-green">{{ capacity.maxConcurrency }}</b></div>
          <div>对应最大 QPS：<b class="text-lg text-neon-green">{{ capacity.maxQps }}</b></div>
          <div>采样档位：{{ capacity.curve?.length ?? 0 }} 个</div>
          <div v-if="capacity.extrapolated" class="text-amber-400">外推估算（超出实测范围）</div>
        </div>
      </div>
    </div>

    <!-- 容量建议 Tab -->
    <div v-else-if="activeTab === 'capacity'" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">扩容建议（基于监控数据）</div>
        <div v-if="!advice?.capacity" class="text-xs text-fg-sub py-10 text-center">暂无建议，先在压测 Tab 完成分档压测并评估容量</div>
        <div v-else class="space-y-3">
          <div
            v-for="(a, i) in advice.capacity.advices" :key="i"
            class="px-4 py-3 rounded-lg border"
            :class="riskClass(a.riskLevel)"
          >
            <div class="flex items-center justify-between">
              <div class="text-sm text-fg-main">{{ a.title }}</div>
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="riskClass(a.riskLevel)">{{ a.riskLevel }}</span>
            </div>
            <div class="text-xs text-fg-sub mt-1.5">{{ a.description }}</div>
            <div class="text-xs mt-1.5 flex gap-3">
              <span class="text-neon-green">方式：{{ a.scaleType }}</span>
              <span v-if="a.suggestedInstances">实例：{{ a.suggestedInstances }}</span>
            </div>
          </div>
        </div>
      </div>
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">JVM 配置建议</div>
        <div v-if="advice?.capacity?.jvmAdvice" class="space-y-3">
          <div class="px-4 py-3 rounded-lg bg-white/5">
            <div class="text-xs text-fg-sub">堆峰值 {{ advice.capacity.jvmAdvice.peakHeapUsedMb }}MB / 上限 {{ advice.capacity.jvmAdvice.heapMaxMb }}MB（{{ advice.capacity.jvmAdvice.peakHeapRatio }}%）</div>
            <div class="text-sm text-fg-main mt-1">建议：{{ advice.capacity.jvmAdvice.heapSize }}</div>
          </div>
          <div class="px-4 py-3 rounded-lg bg-white/5">
            <div class="text-xs text-fg-sub">GC 收集器</div>
            <div class="text-sm text-fg-main mt-1">{{ advice.capacity.jvmAdvice.gcCollector }}</div>
          </div>
          <div v-if="advice.capacity.jvmAdvice.params?.length" class="px-4 py-3 rounded-lg bg-white/5">
            <div class="text-xs text-fg-sub mb-2">关键参数</div>
            <div class="flex flex-wrap gap-2">
              <code v-for="(p, i) in advice.capacity.jvmAdvice.params" :key="i" class="text-[11px] px-2 py-1 rounded bg-white/5 text-neon-blue font-mono">{{ p }}</code>
            </div>
          </div>
        </div>
        <div v-else class="text-xs text-fg-sub py-10 text-center">暂无 JVM 配置建议</div>
      </div>
    </div>

    <!-- 数据准备 Tab -->
    <div v-else class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">自动灌数据</div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">连接配置（与「问题与优化」页共用，选中自动填充）</label>
            <select v-model="gen.connId" class="input-select w-full" @change="applyConn">
              <option :value="0">— 手动填写连接 —</option>
              <option v-for="c in conns" :key="c.id" :value="c.id">{{ c.connName }}（{{ c.dbUrl }}）</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-fg-sub">JDBC URL（jdbc:mysql://）</label>
            <input v-model="gen.dbUrl" class="input-text w-full" placeholder="jdbc:mysql://host:3306/db" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-fg-sub">用户</label><input v-model="gen.dbUser" class="input-text w-full" /></div>
            <div><label class="text-xs text-fg-sub">密码</label><input v-model="gen.dbPassword" type="password" class="input-text w-full" /></div>
          </div>
          <div>
            <label class="text-xs text-fg-sub">目标表</label>
            <select v-model="gen.tableName" class="input-select w-full" @change="loadColumns">
              <option value="">— 先读取表清单 —</option>
              <option v-for="t in tables" :key="t" :value="t">{{ t }}</option>
            </select>
            <button class="text-xs text-neon-blue hover:underline mt-1" @click="loadTables">读取表清单</button>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-fg-sub">行数</label><input v-model.number="gen.rowCount" type="number" class="input-text w-full" min="1" max="100000" /></div>
            <div><label class="text-xs text-fg-sub">字段规则(JSON)</label><input v-model="gen.ruleJson" class="input-text w-full" placeholder='{"fields":{"age":{"type":"range","min":18,"max":60}}}' /></div>
          </div>
          <div v-if="columns.length" class="text-xs text-fg-sub">列：{{ columns.map((c: any) => c.name).join(', ') }}</div>
          <button class="btn-primary w-full justify-center" @click="startDataGen">开始灌数据</button>
        </div>
      </div>
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">灌数据任务</div>
        <div v-if="!genTasks.length" class="text-xs text-fg-sub py-8 text-center">暂无任务</div>
        <div v-else class="space-y-2">
          <div v-for="t in genTasks" :key="t.id" class="px-4 py-3 rounded-lg bg-white/5">
            <div class="flex items-center justify-between">
              <div class="text-sm text-fg-main">{{ t.taskName }}</div>
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(t.status)">{{ t.status }}</span>
            </div>
            <div class="text-[11px] text-fg-sub mt-1">{{ t.tableName }} · {{ t.inserted }}/{{ t.rowCount }} 行 · {{ t.costMs }}ms</div>
            <div class="h-1.5 rounded bg-white/5 mt-2 overflow-hidden">
              <div class="h-full bg-neon-green rounded transition-all" :style="{ width: (t.inserted / Math.max(t.rowCount, 1)) * 100 + '%' }"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Gauge } from 'lucide-vue-next'
import MetricChart from '../components/charts/MetricChart.vue'
import { stressApi, dataGenApi, capacityApi, slowLogApi, sourceApi, appApi, dbConnApi } from '../api'

const tabs = [
  { key: 'stress', label: '压测' },
  { key: 'capacity', label: '容量建议' },
  { key: 'datagen', label: '数据准备' }
]
const activeTab = ref('stress')
const running = ref(false)
const tasks = ref<Array<Record<string, any>>>([])
const report = ref<Record<string, any> | null>(null)
const samples = ref<Array<Record<string, any>>>([])
const capacity = ref<Record<string, any> | null>(null)
const advice = ref<Record<string, any> | null>(null)
const genTasks = ref<Array<Record<string, any>>>([])
const tables = ref<string[]>([])
const columns = ref<Array<{ name: string; type: string }>>([])

const form = ref({
  taskName: '',
  targetUrl: '',
  repoId: 0,
  endpointIdx: -1,
  baseUrl: '',
  method: 'GET',
  concurrency: 50,
  durationSec: 30,
  engineType: 'http',
  stageConcurrencies: '10,50,100'
})
// 项目/应用/接口扫描
const repos = ref<Array<Record<string, any>>>([])
const apps = ref<string[]>([])
const endpoints = ref<Array<Record<string, any>>>([])

// 项目即应用：当前项目的归属应用 = appName（导入时默认=项目名）兜底项目名
const currentRepoApp = computed(() => {
  const r = repos.value.find((x) => x.id === form.value.repoId)
  if (!r) return ''
  return r.appName || r.repoName || ''
})

async function loadEndpoints() {
  endpoints.value = []
  form.value.endpointIdx = -1
  if (!form.value.repoId) return
  try {
    const r = await sourceApi.endpoints(form.value.repoId)
    endpoints.value = r.data || []
  } catch { /* 扫描失败保持空列表 */ }
  // 选定项目后自动同步分支列表（默认展示，无需点击）
  loadBranchOptions()
}

// 与源码重新同步：重新拉取源码 → 重建索引 → 重扫接口（源码更新后点击）
const resyncing = ref(false)
async function resyncAndScan() {
  if (!form.value.repoId || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.resync(form.value.repoId)
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

// 分支切换：当前仓库分支 + 分支列表缓存 + 切换（重同步 + 重扫接口）
const currentRepoBranch = computed(() => {
  const r = repos.value.find((x) => x.id === form.value.repoId)
  return r ? r.branch || '' : ''
})
const branchOptions = ref<string[]>([])
async function loadBranchOptions() {
  if (!form.value.repoId) return
  try {
    const r = await sourceApi.branches(form.value.repoId)
    branchOptions.value = r.data || []
  } catch { /* 拉取失败保持空 */ }
}
async function changeBranch(branch: string) {
  if (!branch || branch === currentRepoBranch.value || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.switchBranch(form.value.repoId, branch)
    await loadRepos()
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

// 选中接口：自动填 method 与 URL（baseUrl + path），用户只需提供 host:port
function applyEndpoint() {
  const e = endpoints.value[form.value.endpointIdx]
  if (!e) return
  form.value.method = e.httpMethod === 'POST' ? 'POST' : 'GET'
  composeUrl(e.path)
  if (!form.value.taskName) {
    form.value.taskName = `${e.className}#${e.methodName || e.path}`
  }
}

// 拼接完整目标 URL；显式传入 path（选接口时）优先于当前输入
function composeUrl(pathOverride?: string) {
  if (form.value.repoId <= 0) return
  const base = (form.value.baseUrl || '').replace(/\/+$/, '')
  const p = pathOverride !== undefined ? pathOverride : currentPath()
  if (base && p) {
    form.value.targetUrl = base + p
  }
}

function currentPath(): string {
  const e = endpoints.value[form.value.endpointIdx]
  return e ? e.path : ''
}
const gen = ref({
  taskName: '',
  connId: 0,
  dbUrl: '',
  dbUser: 'root',
  dbPassword: '',
  tableName: '',
  rowCount: 1000,
  ruleJson: ''
})

const sampleOption = computed(() => ({
  backgroundColor: 'transparent',
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 12, top: 12, bottom: 20 },
  xAxis: { type: 'category', data: samples.value.map((s) => `#${s.id}`), axisLabel: { color: '#8A94A6', fontSize: 10 }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } } },
  yAxis: { type: 'value', axisLabel: { color: '#8A94A6', fontSize: 10 }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } } },
  series: [
    { name: 'QPS', type: 'line', smooth: true, showSymbol: false, data: samples.value.map((s) => s.qps), lineStyle: { color: '#00E5A0', width: 2 }, areaStyle: { color: 'rgba(0,229,160,0.12)' } },
    { name: 'P95(ms)', type: 'line', smooth: true, showSymbol: false, data: samples.value.map((s) => s.p95Ms), lineStyle: { color: '#FAAD14', width: 1.5 }, yAxisIndex: 0 }
  ]
}))

const stageOption = computed(() => {
  const pts = report.value?.result?.stagePoints || []
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#8A94A6', fontSize: 11 }, top: 0 },
    grid: { left: 40, right: 40, top: 26, bottom: 20 },
    xAxis: { type: 'category', data: pts.map((p: any) => `${p.concurrency} 并发`), axisLabel: { color: '#8A94A6', fontSize: 10 }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } } },
    yAxis: [
      { type: 'value', name: 'QPS', axisLabel: { color: '#8A94A6', fontSize: 10 }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } } },
      { type: 'value', name: 'P95ms', axisLabel: { color: '#8A94A6', fontSize: 10 }, splitLine: { show: false } }
    ],
    series: [
      { name: 'QPS', type: 'bar', data: pts.map((p: any) => p.qps), itemStyle: { color: '#0EA5E9', borderRadius: [4, 4, 0, 0] } },
      { name: 'P95(ms)', type: 'line', yAxisIndex: 1, data: pts.map((p: any) => p.p95Ms), lineStyle: { color: '#00E5A0', width: 2 }, itemStyle: { color: '#00E5A0' } }
    ]
  }
})

function statusClass(s: string) {
  const map: Record<string, string> = {
    running: 'text-neon-green bg-neon-green/10',
    success: 'text-neon-green bg-neon-green/10',
    failed: 'text-red-400 bg-red-400/10',
    done: 'text-neon-green bg-neon-green/10',
    executing: 'text-neon-blue bg-neon-blue/10'
  }
  return map[s] || 'text-fg-sub bg-white/5'
}

function riskClass(r: string) {
  return r === 'high'
    ? 'border-red-400/30 bg-red-400/5 text-red-300'
    : r === 'medium'
      ? 'border-amber-400/30 bg-amber-400/5 text-amber-300'
      : 'border-neon-green/30 bg-neon-green/5 text-neon-green'
}

function fmtTime(t: string | number | Date | undefined | null) {
  if (!t && t !== 0) return ''
  const d = typeof t === 'object' ? t : new Date(typeof t === 'number' ? t : String(t))
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString('zh-CN', { hour12: false })
}

async function loadTasks() {
  const r = await stressApi.list({ limit: 20 })
  tasks.value = r.data
}

let pollTimer: ReturnType<typeof setInterval> | null = null
function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    loadTasks()
  }, 3000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function startStress() {
  if (!form.value.targetUrl) return
  const stageList = form.value.stageConcurrencies
    .split(',').map((s) => Number(s.trim())).filter((n) => n > 0)
  const body: Record<string, unknown> = {
    taskName: form.value.taskName || undefined,
    targetUrl: form.value.targetUrl,
    // 强绑定：压测任务归属应用 = 项目绑定的应用
    appName: currentRepoApp.value || undefined,
    method: form.value.method,
    concurrency: form.value.concurrency,
    durationSec: form.value.durationSec,
    engineType: form.value.engineType
  }
  if (stageList.length > 1) body.stageConcurrencies = stageList
  running.value = true
  try {
    await stressApi.create(body)
    await loadTasks()
    setTimeout(() => { running.value = false }, 3000)
  } catch {
    running.value = false
  }
}

async function showReport(id: number) {
  const r = await stressApi.report(id)
  report.value = r.data
  capacity.value = r.data.result?.capacity || null
  const s = await stressApi.samples(id)
  samples.value = s.data
}

async function runCapacity() {
  if (!report.value?.task?.id) return
  const r = await capacityApi.analyze({ taskId: report.value.task.id })
  capacity.value = r.data
  // 重新读取报告使 capacity 落入报告
  await showReport(report.value.task.id)
}

async function loadAdvice() {
  const r = await capacityApi.advice()
  advice.value = r.data
}

async function loadTables() {
  const r = await dataGenApi.tables({ dbUrl: gen.value.dbUrl, dbUser: gen.value.dbUser, dbPassword: gen.value.dbPassword })
  tables.value = r.data
}

async function loadColumns() {
  if (!gen.value.tableName) return
  const r = await dataGenApi.columns({ dbUrl: gen.value.dbUrl, dbUser: gen.value.dbUser, dbPassword: gen.value.dbPassword, tableName: gen.value.tableName })
  columns.value = r.data
}

async function startDataGen() {
  await dataGenApi.create({
    taskName: gen.value.taskName || `灌数据-${gen.value.tableName}`,
    connId: gen.value.connId || undefined,
    dbUrl: gen.value.dbUrl,
    dbUser: gen.value.dbUser,
    dbPassword: gen.value.dbPassword,
    tableName: gen.value.tableName,
    rowCount: gen.value.rowCount,
    ruleJson: gen.value.ruleJson || undefined
  })
  const r = await dataGenApi.list()
  genTasks.value = r.data
}

onMounted(() => {
  loadTasks()
  startPolling()
  loadAdvice()
  loadRepos()
  loadApps()
  loadConns()
  dataGenApi.list().then((r) => { genTasks.value = r.data }).catch(() => {})
})

onUnmounted(stopPolling)

// 项目/应用/连接配置（数据准备 Tab 复用连接配置）
const conns = ref<Array<Record<string, any>>>([])

async function loadRepos() {
  const r = await sourceApi.repos()
  repos.value = r.data
}

async function loadApps() {
  try {
    const r = await appApi.list()
    apps.value = (r.data || []).map((a: Record<string, any>) => a.appName).filter(Boolean)
  } catch { /* 应用列表失败不阻断 */ }
}

async function loadConns() {
  try {
    const r = await dbConnApi.list()
    conns.value = r.data || []
  } catch { /* 连接配置失败不阻断 */ }
}

// 数据准备：从连接配置选择后自动填连接参数（仍可手改）
function applyConn() {
  const c = conns.value.find((x) => x.id === gen.value.connId)
  if (c) {
    gen.value.dbUrl = c.dbUrl
    gen.value.dbUser = c.username
    gen.value.dbPassword = c.password || ''
  }
}
</script>
