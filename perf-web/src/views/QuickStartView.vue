<template>
  <div>
    <div class="page-title">
      <Rocket class="w-5 h-5 text-neon-green" />
      快速开始
      <span class="text-xs font-normal text-fg-sub ml-2">带着项目上下文，六步完成性能检测闭环</span>
    </div>

    <!-- 步骤条 -->
    <div class="glass-card p-4 mb-5">
      <div class="flex items-center">
        <template v-for="(s, i) in stepDefs" :key="s.title">
          <button
            class="flex items-center gap-2 shrink-0 group"
            @click="goStep(i)"
          >
            <span
              class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-all"
              :class="badgeClass(i)"
            >
              <Check v-if="done(i)" class="w-4 h-4" />
              <template v-else>{{ i + 1 }}</template>
            </span>
            <span
              class="text-xs hidden md:block"
              :class="i === step ? 'text-fg-main font-medium' : done(i) ? 'text-neon-green' : 'text-fg-sub'"
            >{{ s.title }}</span>
          </button>
          <div v-if="i < stepDefs.length - 1" class="flex-1 h-0.5 mx-2 rounded" :class="done(i) ? 'bg-neon-green/50' : 'bg-white/10'" />
        </template>
      </div>
    </div>

    <!-- Step 1 项目 -->
    <div v-if="step === 0" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">选择被测项目</div>
      <div class="text-[11px] text-fg-sub mb-4">项目贯穿后续所有步骤（接口清单 / 源码分析 / 数据归属）。没有项目？<router-link class="text-neon-blue hover:underline" to="/projects">去项目管理导入</router-link></div>
      <div v-if="!repos.length" class="text-xs text-fg-sub py-8 text-center">
        还没有项目，<router-link class="text-neon-blue hover:underline" to="/projects">去导入</router-link>（zip / git / 本地路径）
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        <button
          v-for="r in repos" :key="r.id"
          class="text-left p-4 rounded-xl border transition-all"
          :class="repoId === r.id ? 'border-neon-green bg-neon-green/10' : 'border-white/10 bg-white/[0.03] hover:border-white/30'"
          @click="selectRepo(r)"
        >
          <div class="text-sm text-fg-main">{{ r.repoName }}</div>
          <div class="text-[11px] text-fg-sub mt-1">{{ r.appName || '未绑定应用' }} · {{ r.fileCount }} 文件</div>
          <div v-if="repoId === r.id" class="text-[11px] text-neon-green mt-1">✓ 已选</div>
        </button>
      </div>
    </div>

    <!-- Step 2 数据准备 -->
    <div v-else-if="step === 1" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">灌注测试数据 <span class="text-neon-green text-xs ml-1">项目：{{ repoName }}</span></div>
      <div class="text-[11px] text-fg-sub mb-4">选择数据库连接，试运行通过后正式灌数。需要更复杂的联表/规则配置？<router-link class="text-neon-blue hover:underline" to="/stress?tab=datagen">打开完整数据准备</router-link></div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">数据库连接</label>
            <select v-model="connId" class="input-select w-full">
              <option :value="0">— 选择连接配置 —</option>
              <option v-for="c in conns" :key="c.id" :value="c.id">{{ c.connName }}（{{ c.dbUrl }}）</option>
            </select>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-fg-sub">用户表行数</label><input v-model.number="genUsers" type="number" class="input-text w-full" /></div>
            <div><label class="text-xs text-fg-sub">订单表行数</label><input v-model.number="genOrders" type="number" class="input-text w-full" /></div>
          </div>
          <div class="flex gap-2">
            <button class="btn-secondary flex-1 justify-center" :disabled="!connId || busy" @click="runGen(true)">试运行</button>
            <button class="btn-primary flex-1 justify-center" :disabled="!connId || busy || !genDryPassed" @click="runGen(false)">正式灌数</button>
          </div>
        </div>
        <div class="rounded-xl bg-white/5 p-4 text-xs space-y-1.5">
          <div class="text-fg-sub font-medium mb-1">表组（demo 联表模板）</div>
          <div class="font-mono text-[11px] text-fg-sub leading-relaxed">perf_user（{{ genUsers }} 行）<br />perf_order（{{ genOrders }} 行，user_id → perf_user.id）</div>
          <div v-if="genTask" class="pt-2 border-t border-white/10">
            <span :class="taskStatusClass(genTask.status)">{{ genTask.status }}</span>
            <span class="text-fg-sub ml-2">#{{ genTask.id }} {{ genTask.inserted }}/{{ genTask.totalRows || genTask.rowCount }} 行</span>
          </div>
          <div v-if="genDryPassed && !genTask" class="text-neon-green">✓ 试运行通过，可正式灌数</div>
        </div>
      </div>
    </div>

    <!-- Step 3 压测 -->
    <div v-else-if="step === 2" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">配置并发起压测 <span class="text-neon-green text-xs ml-1">项目：{{ repoName }}</span></div>
      <div class="text-[11px] text-fg-sub mb-4">已导入项目会自动扫描 GET 接口清单。需要完整配置（JMeter/分阶段）？<router-link class="text-neon-blue hover:underline" to="/stress">打开压测中心</router-link></div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">GET 接口</label>
            <select v-model="endpointIdx" class="input-select w-full">
              <option :value="-1">— {{ endpoints.length ? '选择接口' : '无扫描结果（手填路径）' }} —</option>
              <option v-for="(e, i) in endpoints" :key="i" :value="i">{{ e.path }}</option>
            </select>
          </div>
          <div v-if="endpointIdx < 0">
            <label class="text-xs text-fg-sub">完整 URL</label>
            <input v-model="manualUrl" class="input-text w-full" placeholder="http://127.0.0.1:8081/api/user/list?limit=200" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-fg-sub">并发</label><input v-model.number="stressConcurrency" type="number" min="1" class="input-text w-full" /></div>
            <div><label class="text-xs text-fg-sub">时长(秒)</label><input v-model.number="stressDuration" type="number" min="5" class="input-text w-full" /></div>
          </div>
          <button class="btn-primary w-full justify-center" :disabled="busy || targetUrl === ''" @click="startStress">发起压测</button>
        </div>
        <div class="rounded-xl bg-white/5 p-4 text-xs space-y-1.5">
          <div class="text-fg-sub font-medium mb-1">任务状态</div>
          <div v-if="!stressTask" class="text-fg-sub">尚未发起</div>
          <template v-else>
            <div><span :class="taskStatusClass(stressTask.status)">{{ stressTask.status }}</span> <span class="text-fg-sub ml-2">#{{ stressTask.id }} {{ stressTask.concurrency }} 并发 × {{ stressTask.durationSec }}s</span></div>
            <div class="text-fg-sub font-mono text-[11px] truncate">{{ stressTask.targetUrl }}</div>
          </template>
        </div>
      </div>
    </div>

    <!-- Step 4 结果 -->
    <div v-else-if="step === 3" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">获取压测结果</div>
      <div class="text-[11px] text-fg-sub mb-4">任务完成后自动拉取报告摘要。<router-link class="text-neon-blue hover:underline" to="/stress">打开完整压测报告</router-link></div>
      <div v-if="!stressTask" class="text-xs text-fg-sub py-8 text-center">请先在上一步发起压测</div>
      <div v-else-if="!report" class="text-xs text-fg-sub py-8 text-center flex items-center justify-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon-green animate-pulse"></span> 任务 {{ stressTask.status }}，等待完成…
      </div>
      <div v-else class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">总请求</div><div class="text-xl font-bold neon-text mt-1">{{ report.totalRequests ?? '—' }}</div></div>
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">平均 QPS</div><div class="text-xl font-bold neon-text mt-1">{{ report.qps ?? '—' }}</div></div>
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">P95 (ms)</div><div class="text-xl font-bold neon-text mt-1">{{ report.p95Ms ?? '—' }}</div></div>
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">错误率</div><div class="text-xl font-bold neon-text mt-1">{{ report.errorRate != null ? (report.errorRate * 100).toFixed(2) + '%' : '—' }}</div></div>
        <div v-if="capacityText" class="col-span-full rounded-xl bg-neon-green/5 border border-neon-green/20 p-3 text-xs text-fg-sub">{{ capacityText }}</div>
      </div>
    </div>

    <!-- Step 5 调用链 -->
    <div v-else-if="step === 4" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">分析调用链 <span class="text-neon-green text-xs ml-1">{{ appName }}</span></div>
      <div class="text-[11px] text-fg-sub mb-4">压测流量产生的调用链（热点方法耗时树）。<router-link class="text-neon-blue hover:underline" to="/trace">打开调用链分析</router-link></div>
      <div v-if="!traces.length" class="text-xs text-fg-sub py-8 text-center">暂无调用链，回上一步压测产生流量后自动捕获</div>
      <div v-else class="space-y-2">
        <button v-for="t in traces" :key="t.id" class="w-full flex items-center justify-between px-4 py-3 rounded-lg bg-white/5 hover:bg-white/10 transition-all text-left" @click="router.push('/trace')">
          <div class="min-w-0">
            <div class="text-xs text-fg-main truncate font-mono">{{ t.rootMethod || t.entryMethod || t.traceId }}</div>
            <div class="text-[11px] text-fg-sub mt-0.5">{{ t.startTime || '' }}</div>
          </div>
          <div class="text-sm text-amber-400 shrink-0 ml-3">{{ Number(t.totalMs || t.durationMs || 0).toFixed(1) }}ms</div>
        </button>
      </div>
    </div>

    <!-- Step 6 优化 -->
    <div v-else class="glass-card p-5">
      <div class="text-sm font-medium mb-1">问题优化</div>
      <div class="text-[11px] text-fg-sub mb-4">平台自动识别的性能问题（N+1 / 慢 SQL / 瓶颈），可让 AI 关联源码给出修复方案。<router-link class="text-neon-blue hover:underline" to="/optimize">打开性能优化</router-link></div>
      <div v-if="!issues.length" class="text-xs text-fg-sub py-6 text-center">暂无问题记录 — 可让 AI 立即分析</div>
      <div v-else class="space-y-2 mb-4">
        <div v-for="i in issues" :key="i.id" class="flex items-center justify-between px-4 py-2.5 rounded-lg bg-white/5">
          <div class="min-w-0">
            <div class="text-xs text-fg-main truncate">{{ i.title }}</div>
            <div class="text-[11px] text-fg-sub mt-0.5">{{ i.issueType }} · {{ i.status }}</div>
          </div>
          <span class="text-[11px] px-2 py-0.5 rounded-full shrink-0" :class="issueStatusClass(i.status)">{{ i.status }}</span>
        </div>
      </div>
      <button class="btn-primary justify-center" @click="goAiAnalyze">
        <Bot class="w-4 h-4" /> 让 AI 分析根因并给出修复方案
      </button>
    </div>

    <!-- 底部导航 -->
    <div class="flex items-center justify-between mt-5">
      <button class="btn-secondary" :disabled="step === 0" @click="goStep(step - 1)">上一步</button>
      <div class="text-xs text-fg-sub">步骤 {{ step + 1 }} / {{ stepDefs.length }} · 所选项目：<span class="text-neon-green">{{ repoName || '未选择' }}</span></div>
      <button v-if="step < stepDefs.length - 1" class="btn-primary" @click="goStep(step + 1)">下一步<ArrowRight class="w-4 h-4 ml-1" /></button>
      <button v-else class="btn-primary" @click="router.push('/dashboard')">完成，回到总览<Check class="w-4 h-4 ml-1" /></button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Rocket, Check, ArrowRight, Bot } from 'lucide-vue-next'
import { taskStatusClass, issueStatusClass } from '../utils/format'
import { appApi, dataGenApi, sourceApi, stressApi, traceApi, dbConnApi } from '../api'

const route = useRoute()
const router = useRouter()

const stepDefs = [
  { title: '选择项目' }, { title: '灌注数据' }, { title: '发起压测' },
  { title: '获取结果' }, { title: '分析调用链' }, { title: '问题优化' }
]

const step = ref(0)
const busy = ref(false)
const repos = ref<Array<Record<string, any>>>([])
const repoId = ref<number>(0)
const conns = ref<Array<Record<string, any>>>([])
const connId = ref<number>(0)
const genUsers = ref(1000)
const genOrders = ref(2000)
const genTask = ref<any>(null)
const genDryPassed = ref(false)
const endpoints = ref<Array<Record<string, any>>>([])
const endpointIdx = ref(-1)
const manualUrl = ref('http://127.0.0.1:8081/api/user/list?limit=200')
const stressConcurrency = ref(20)
const stressDuration = ref(15)
const stressTask = ref<any>(null)
const report = ref<any>(null)
const capacityText = ref('')
const traces = ref<Array<Record<string, any>>>([])
const issues = ref<Array<Record<string, any>>>([])
const appInstances = ref<Array<Record<string, any>>>([])
let pollTimer: any = null

const repoName = computed(() => repos.value.find((r) => r.id === repoId.value)?.repoName || '')
const appName = computed(() => repos.value.find((r) => r.id === repoId.value)?.appName || repoName.value)
const targetUrl = computed(() => endpointIdx.value >= 0
  ? String(endpoints.value[endpointIdx.value].url || endpoints.value[endpointIdx.value].path || '')
  : manualUrl.value.trim())

function done(i: number) {
  if (i === 0) return repoId.value > 0
  if (i === 1) return !!genTask.value && ['finished', 'running'].includes(genTask.value.status)
  if (i === 2) return !!stressTask.value
  if (i === 3) return !!report.value
  if (i === 4) return traces.value.length > 0
  return issues.value.length > 0
}

function badgeClass(i: number) {
  if (done(i)) return 'bg-neon-green text-black'
  if (i === step.value) return 'bg-neon-blue text-black'
  return 'bg-white/10 text-fg-sub'
}

function goStep(i: number) {
  step.value = Math.max(0, Math.min(stepDefs.length - 1, i))
  router.replace({ query: { step: String(step.value) } })
  onEnterStep()
}

function selectRepo(r: any) {
  repoId.value = r.id
  loadEndpoints()
  loadTracesAndIssues()
}

async function loadEndpoints() {
  endpoints.value = []
  if (!repoId.value) return
  try {
    const app = appName.value
    const inst = appInstances.value.find((a) => a.appName === app)
    const base = inst ? `http://${inst.appHost}:${inst.appPort}` : ''
    const r = await sourceApi.endpoints(repoId.value)
    endpoints.value = (r.data || []).filter((e: any) => e.httpMethod === 'GET')
      .map((e: any) => ({ ...e, url: base + e.path }))
    if (endpoints.value.length) endpointIdx.value = 0
  } catch { /* 扫描失败不阻断 */ }
}

async function runGen(dryRun: boolean) {
  busy.value = true
  try {
    const r = await dataGenApi.create({
      taskName: `向导-${dryRun ? '试运行' : '灌数'}-${repoName.value}`,
      connId: connId.value || undefined,
      dryRun,
      tables: [
        { tableName: 'perf_user', rowCount: genUsers.value },
        { tableName: 'perf_order', rowCount: genOrders.value,
          fields: { user_id: { type: 'ref', table: 'perf_user', column: 'id' } } }
      ]
    })
    genTask.value = r.data
    if (dryRun) {
      // 轮询一次结果后放行正式灌数
      setTimeout(checkGen, 2500)
    }
  } finally {
    busy.value = false
  }
}

async function checkGen() {
  if (!genTask.value) return
  const r = await dataGenApi.list(5)
  const t = (r.data || []).find((x: any) => x.id === genTask.value.id)
  if (t) genTask.value = t
  if (genTask.value.status === 'finished' && genTask.value.detailJson?.includes('"passed":true')) {
    genDryPassed.value = true
  }
}

async function startStress() {
  busy.value = true
  try {
    const r = await stressApi.create({
      taskName: `向导压测-${repoName.value}`,
      targetUrl: targetUrl.value,
      method: 'GET',
      concurrency: stressConcurrency.value,
      durationSec: stressDuration.value,
      engineType: 'http',
      appName: appName.value || undefined
    })
    stressTask.value = r.data
    startPolling()
  } finally {
    busy.value = false
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!stressTask.value) return
    const r = await stressApi.list({ limit: 10 })
    const t = (r.data || []).find((x: any) => x.id === stressTask.value.id)
    if (t) stressTask.value = t
    if (t?.status === 'finished') {
      stopPolling()
      const rep = await stressApi.report(stressTask.value.id)
      const stat = rep.data?.statistics || rep.data?.result?.statistics || {}
      report.value = stat
      const cap = rep.data?.result?.capacity || rep.data?.capacity
      if (cap) capacityText.value = `容量评估：最大可承受并发 ${cap.maxConcurrency ?? '—'}，最大 QPS ${cap.maxQps ?? '—'}`
      loadTracesAndIssues()
    }
  }, 2000)
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

async function loadTracesAndIssues() {
  try {
    const t = await traceApi.list({ app: appName.value || undefined, limit: 5 })
    traces.value = t.data || []
  } catch { /* 忽略 */ }
  try {
    const i = await sourceApi.issues({ limit: 5 })
    issues.value = i.data || []
  } catch { /* 忽略 */ }
}

function goAiAnalyze() {
  router.push({ path: '/ai', query: { msg: `分析项目「${repoName.value}」最近的慢 SQL 与热点调用链根因，并给出修复方案` } })
}

function onEnterStep() {
  if (step.value === 3 && stressTask.value?.status === 'finished' && !report.value) startPolling()
}

onMounted(async () => {
  step.value = Math.max(0, Math.min(5, parseInt(String(route.query.step ?? '0')) || 0))
  const [r, c, g, a] = await Promise.allSettled([
    sourceApi.repos(50), dbConnApi.list(), dataGenApi.list(5), appApi.list()
  ])
  if (r.status === 'fulfilled') repos.value = r.value.data || []
  if (c.status === 'fulfilled') conns.value = c.value.data || []
  if (g.status === 'fulfilled') {
    const finished = (g.value.data || []).find((x: any) => x.status === 'finished')
    if (finished) { genTask.value = finished; genDryPassed.value = true }
  }
  if (a.status === 'fulfilled') appInstances.value = a.value.data || []
  // 已有项目时默认选中第一个，定位到第一个未完成步骤
  if (repos.value.length && !repoId.value) {
    selectRepo(repos.value[0])
  }
  const t = await stressApi.list({ limit: 1 }).catch(() => ({ data: [] }))
  if (t.data?.length && ['running', 'finished'].includes(t.data[0].status)) stressTask.value = t.data[0]
  if (stressTask.value) startPolling()
  loadTracesAndIssues()
})

onUnmounted(stopPolling)
</script>
