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
            <label class="text-xs text-fg-sub">任务名</label>
            <input v-model="form.taskName" class="input-text w-full" placeholder="如 登录接口压测" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">目标 URL（http/https）</label>
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
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(t.status)">{{ t.status }}</span>
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
import { computed, onMounted, ref } from 'vue'
import { Gauge } from 'lucide-vue-next'
import MetricChart from '../components/charts/MetricChart.vue'
import { stressApi, dataGenApi, capacityApi, slowLogApi } from '../api'

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
  method: 'GET',
  concurrency: 50,
  durationSec: 30,
  engineType: 'http',
  stageConcurrencies: '10,50,100'
})
const gen = ref({
  taskName: '',
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

function fmtTime(t: string | number) {
  return new Date(Number(t)).toLocaleString('zh-CN', { hour12: false })
}

async function loadTasks() {
  const r = await stressApi.list({ limit: 20 })
  tasks.value = r.data
}

async function startStress() {
  if (!form.value.targetUrl) return
  const stageList = form.value.stageConcurrencies
    .split(',').map((s) => Number(s.trim())).filter((n) => n > 0)
  const body: Record<string, unknown> = {
    taskName: form.value.taskName || undefined,
    targetUrl: form.value.targetUrl,
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
  loadAdvice()
  dataGenApi.list().then((r) => { genTasks.value = r.data }).catch(() => {})
})
</script>
