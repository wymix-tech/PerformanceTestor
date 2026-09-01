<template>
  <div v-if="report" class="glass-card p-5">
    <!-- 报告头：任务名 + 版本 + 操作 -->
    <div class="text-sm font-medium mb-4 flex items-center justify-between flex-wrap gap-2">
      <span class="flex items-center gap-2">
        压测报告 — {{ report.task?.taskName || report.task?.targetUrl }}
        <span v-if="report.task?.appVersion" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-purple/10 text-neon-purple">
          v{{ report.task.appVersion }}
        </span>
        <span v-if="report.result?.warmupSec" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue" title="预热期请求不计入统计">
          预热 {{ report.result.warmupSec }}s
        </span>
      </span>
      <span class="flex items-center gap-3">
        <slot name="actions" />
        <!-- 导出：打印页（新窗口，浏览器打印/PDF）/ 服务端 PDF / Word -->
        <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="openPrintView">🖨 打印预览</button>
        <button class="text-xs text-neon-blue hover:underline cursor-pointer" :disabled="!!exporting" @click="exportReport('pdf')">
          {{ exporting === 'pdf' ? '导出中…' : '导出 PDF' }}
        </button>
        <button class="text-xs text-neon-blue hover:underline cursor-pointer" :disabled="!!exporting" @click="exportReport('doc')">
          {{ exporting === 'doc' ? '导出中…' : '导出 Word' }}
        </button>
        <button v-if="taskFinished" class="text-xs text-neon-green hover:underline cursor-pointer" @click="emit('rerun')">↻ 再来一次</button>
        <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="emit('run-capacity')">容量评估 →</button>
      </span>
    </div>

    <!-- 核心指标卡 -->
    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">
      <div class="metric-card"><div class="text-xs text-fg-sub">总请求</div><div class="text-lg font-bold">{{ report.result?.totalRequests ?? '—' }}</div></div>
      <div class="metric-card"><div class="text-xs text-fg-sub">QPS</div><div class="text-lg font-bold neon-text">{{ report.result?.qps ?? '—' }}</div></div>
      <div class="metric-card"><div class="text-xs text-fg-sub">P50</div><div class="text-lg font-bold">{{ report.result?.p50Ms ?? '—' }}ms</div></div>
      <div class="metric-card"><div class="text-xs text-fg-sub">P95</div><div class="text-lg font-bold">{{ report.result?.p95Ms ?? '—' }}ms</div></div>
      <div class="metric-card"><div class="text-xs text-fg-sub">P99</div><div class="text-lg font-bold">{{ report.result?.p99Ms ?? '—' }}ms</div></div>
      <div class="metric-card">
        <div class="text-xs text-fg-sub">错误率</div>
        <div class="text-lg font-bold" :class="(report.result?.errorRate ?? 0) > 5 ? 'text-red-400' : 'text-neon-green'">
          {{ report.result?.errorRate ?? 0 }}%
        </div>
      </div>
    </div>

    <!-- 父组件扩展区（版本对比表格等） -->
    <slot name="extra" />

    <!-- 曲线 -->
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

    <!-- 容量评估结果 -->
    <div v-if="capacity" class="mt-4 p-4 rounded-lg bg-neon-green/5 border border-neon-green/20">
      <div class="text-sm font-medium text-neon-green mb-2">容量评估：目标 P95 ≤ {{ capacity.targetP95Ms }}ms</div>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
        <div>最大可承受并发：<b class="text-lg text-neon-green">{{ capacity.maxConcurrency }}</b></div>
        <div>对应最大 QPS：<b class="text-lg text-neon-green">{{ capacity.maxQps }}</b></div>
        <div>采样档位：{{ capacity.curve?.length ?? 0 }} 个</div>
        <div v-if="capacity.extrapolated" class="text-amber-400">外推估算（超出实测范围）</div>
      </div>

      <!-- 科学化模型：拟合公式 / R² / 理论拐点 / RT 拐点 -->
      <div v-if="capacity.model" class="mt-3 pt-3 border-t border-neon-green/20 space-y-2 text-xs">
        <div class="font-mono text-[11px] text-fg-sub break-all">
          拟合模型：{{ capacity.model.formula }}（R² = {{ capacity.r2 }}，{{ capacity.model.points }} 档参与拟合）
        </div>
        <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
          <div v-if="capacity.model.vertexConcurrency">
            理论拐点并发：<b class="text-neon-green">{{ capacity.model.vertexConcurrency }}</b>
            <span class="text-fg-sub">（峰值 QPS {{ capacity.model.vertexQps }}）</span>
          </div>
          <div v-if="capacity.rtKneeConcurrency">
            RT 上翘拐点并发：<b class="text-amber-400">{{ capacity.rtKneeConcurrency }}</b>
          </div>
          <div v-else class="text-fg-sub">RT 曲线未见明显上翘拐点</div>
        </div>
      </div>

      <!-- 各档 95% 置信区间 -->
      <div v-if="capacity.confidenceInterval?.length" class="mt-3 pt-3 border-t border-neon-green/20">
        <div class="text-xs text-fg-sub mb-2">分档 95% 置信区间（QPS 采样 {{ capacity.confidenceInterval[0].qpsSamples }} 点 / 档）</div>
        <div class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead>
              <tr class="text-fg-sub text-[11px]">
                <th class="text-left py-1 pr-3 font-normal">并发</th>
                <th class="text-left py-1 pr-3 font-normal">QPS</th>
                <th class="text-left py-1 pr-3 font-normal">QPS 95% CI</th>
                <th class="text-left py-1 pr-3 font-normal">P95 (ms)</th>
                <th class="text-left py-1 font-normal">P95 95% CI</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in capacity.confidenceInterval" :key="i" class="border-t border-white/5">
                <td class="py-1 pr-3">{{ c.concurrency }}</td>
                <td class="py-1 pr-3">{{ c.qps }}</td>
                <td class="py-1 pr-3 font-mono text-neon-blue">{{ c.qpsLower }} ~ {{ c.qpsUpper }}</td>
                <td class="py-1 pr-3">{{ c.p95Ms }}</td>
                <td class="py-1 font-mono text-neon-green">{{ c.p95Lower }} ~ {{ c.p95Upper }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 拟合曲线：实测点 + 二项式拟合 + 95% 置信带 + 拐点标记 -->
    <div v-if="capacity?.fittedCurve?.length" class="mt-4">
      <div class="text-xs text-fg-sub mb-2">容量拟合曲线（实测点 / 拟合趋势 / 95% 置信带 / 拐点）</div>
      <MetricChart :option="fitOption" height="240px" />
    </div>

    <!-- 历史趋势：同应用 + 同目标的多版本指标折线（≥2 次记录才展示曲线） -->
    <StressTrendChart v-if="trend.length" :points="trend" :current-task-id="report?.task?.id" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import axios from 'axios'
import { onMounted, watch } from 'vue'
import MetricChart from '../charts/MetricChart.vue'
import StressTrendChart from './StressTrendChart.vue'
import { reportApi, stressApi } from '../../api'

/**
 * 压测报告公共组件：指标卡 + QPS/分档曲线 + 容量评估卡片 + 历史趋势。
 * 版本对比等视图特有内容经 #extra / #actions 插槽注入。
 */
const props = defineProps<{
  /** 报告（task + result） */
  report: Record<string, any> | null
  /** 采样序列（QPS 曲线数据源） */
  samples: Array<Record<string, any>>
  /** 容量评估结果 */
  capacity?: Record<string, any> | null
}>()

const emit = defineEmits<{
  (e: 'rerun'): void
  (e: 'run-capacity'): void
}>()

/** 报告任务是否已执行完（完成或失败均可"再来一次"） */
const taskFinished = computed(() => {
  const s = props.report?.task?.status
  return s === 'finished' || s === 'failed'
})

/**
 * 历史趋势数据（同应用 + 同目标 URL 的近期指标序列）。
 *
 * <p>仅在归属了应用名的任务上有意义（未归属应用的历史无法可靠归集），
 * 且趋势加载失败时静默降级——报告主体不受影响。
 */
const trend = ref<Array<Record<string, any>>>([])

async function loadTrend() {
  const task = props.report?.task
  if (!task?.appName || !task?.targetUrl) {
    trend.value = []
    return
  }
  try {
    const r = await stressApi.trend({
      appName: task.appName,
      url: task.targetUrl,
      limit: 20
    })
    trend.value = r.data || []
  } catch {
    trend.value = [] // 趋势是增强信息，失败不影响报告主体
  }
}

/** 正在导出的格式（'' 表示空闲，用于按钮 loading 态与防重复点击） */
const exporting = ref('')

/**
 * 打印预览：拉取服务端渲染的打印页（同套 CSS，白底 A4），写入新窗口后调起打印对话框。
 *
 * 注意：不用 window.open(url) 直开 —— 新窗口请求不带 JWT 会被 401 拦截跳登录页，
 * 故改为 axios 携带 token 取回 HTML 再写入（同为裸 axios，避免拦截器解包）。
 */
async function openPrintView() {
  const id = props.report?.task?.id
  if (!id) return
  const token = localStorage.getItem('perf_token')
  try {
    const res = await axios.get(reportApi.exportUrl(id, 'html', true), {
      responseType: 'text',
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    })
    const w = window.open('', '_blank')
    if (!w) {
      alert('浏览器拦截了新窗口，请允许弹出窗口后重试')
      return
    }
    w.document.write(res.data)
    w.document.close()
    // 等待样式与内容就绪后再唤起打印对话框
    w.setTimeout(() => w.print(), 300)
  } catch {
    alert('打印预览加载失败，请重试')
  }
}

/**
 * 导出 PDF / Word：服务端生成文件，经 Blob 触发下载。
 *
 * 注意：此处用裸 axios 而非封装实例 —— 后者响应拦截器会解包为 `response.data`，
 * 导致拿不到 Content-Disposition（文件名），故需自行注入 token。
 */
async function exportReport(format: 'pdf' | 'doc') {
  const id = props.report?.task?.id
  if (!id || exporting.value) return
  exporting.value = format
  try {
    const token = localStorage.getItem('perf_token')
    const res = await axios.get(reportApi.exportUrl(id, format, false), {
      responseType: 'blob',
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    })
    // 从 Content-Disposition 提取文件名，失败时用本地兜底名
    const disposition: string = res.headers?.['content-disposition'] || ''
    const matched = disposition.match(/filename\*=UTF-8''(.+)/)
    const filename = matched ? decodeURIComponent(matched[1])
      : `压测报告_${id}.${format}`
    const url = URL.createObjectURL(new Blob([res.data]))
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    alert('导出失败，请重试')
  } finally {
    exporting.value = ''
  }
}

const sampleOption = computed(() => ({
  backgroundColor: 'transparent',
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 12, top: 12, bottom: 20 },
  xAxis: {
    type: 'category',
    data: props.samples.map((s) => `#${s.id}`),
    axisLabel: { color: '#8A94A6', fontSize: 10 },
    axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } }
  },
  yAxis: {
    type: 'value',
    axisLabel: { color: '#8A94A6', fontSize: 10 },
    splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } }
  },
  series: [
    {
      name: 'QPS', type: 'line', smooth: true, showSymbol: false,
      data: props.samples.map((s) => s.qps),
      lineStyle: { color: '#00E5A0', width: 2 },
      areaStyle: { color: 'rgba(0,229,160,0.12)' }
    },
    {
      name: 'P95(ms)', type: 'line', smooth: true, showSymbol: false,
      data: props.samples.map((s) => s.p95Ms),
      lineStyle: { color: '#FAAD14', width: 1.5 }
    }
  ]
}))

const stageOption = computed(() => {
  const pts = props.report?.result?.stagePoints || []
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#8A94A6', fontSize: 11 }, top: 0 },
    grid: { left: 40, right: 40, top: 26, bottom: 20 },
    xAxis: {
      type: 'category',
      data: pts.map((p: any) => `${p.concurrency} 并发`),
      axisLabel: { color: '#8A94A6', fontSize: 10 },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } }
    },
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

/** 拟合曲线数据源：实测点（散点）+ 拟合趋势线 + 95% 置信带 + 拐点标记 */
const fitSeries = computed(() => {
  const cap = props.capacity
  if (!cap?.fittedCurve?.length) return []
  const fitted = cap.fittedCurve.map((p: any) => [p.concurrency, p.qps])
  const measured = (cap.curve || []).map((p: any) => [p.concurrency, p.qps])
  const ci = cap.confidenceInterval || []
  const series: any[] = []

  // 95% 置信带：下界透明 + 上界堆叠填充（ECharts 标准面积带写法）
  if (ci.length) {
    series.push({
      name: 'CI 下界', type: 'line', data: ci.map((c: any) => [c.concurrency, c.qpsLower]),
      lineStyle: { opacity: 0 }, stack: 'ci', symbol: 'none', silent: true, tooltip: { show: false }
    })
    series.push({
      name: '95% 置信带', type: 'line', data: ci.map((c: any) => [c.concurrency, c.qpsUpper - c.qpsLower]),
      lineStyle: { opacity: 0 }, stack: 'ci', symbol: 'none',
      areaStyle: { color: 'rgba(14,165,233,0.18)' }
    })
  }
  series.push({
    name: '拟合趋势', type: 'line', smooth: true, showSymbol: false, data: fitted,
    lineStyle: { color: '#A78BFA', width: 2, type: 'dashed' }, itemStyle: { color: '#A78BFA' }
  })
  series.push({
    name: '实测 QPS', type: 'scatter', data: measured, symbolSize: 8,
    itemStyle: { color: '#0EA5E9' }
  })
  if (cap.model?.vertexConcurrency) {
    series.push({
      name: '理论拐点', type: 'scatter', symbol: 'diamond', symbolSize: 14,
      data: [[cap.model.vertexConcurrency, cap.model.vertexQps]],
      itemStyle: { color: '#FAAD14' }
    })
  }
  return series
})

const fitOption = computed(() => {
  const cap = props.capacity
  const markLine = cap?.rtKneeConcurrency
    ? {
        symbol: 'none',
        label: { formatter: 'RT 拐点 ' + cap.rtKneeConcurrency, color: '#FAAD14', fontSize: 10 },
        lineStyle: { color: '#FAAD14', type: 'dashed' as const },
        data: [{ xAxis: cap.rtKneeConcurrency }]
      }
    : undefined
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#8A94A6', fontSize: 11 }, top: 0 },
    grid: { left: 50, right: 16, top: 26, bottom: 24 },
    xAxis: {
      type: 'value', name: '并发', nameTextStyle: { color: '#8A94A6', fontSize: 10 },
      min: cap?.fittedCurve?.[0]?.concurrency ?? 0,
      axisLabel: { color: '#8A94A6', fontSize: 10 },
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } },
      splitLine: { show: false }
    },
    yAxis: {
      type: 'value', name: 'QPS', nameTextStyle: { color: '#8A94A6', fontSize: 10 },
      axisLabel: { color: '#8A94A6', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } }
    },
    series: fitSeries.value.length
      ? [...fitSeries.value.slice(0, -1), { ...fitSeries.value[fitSeries.value.length - 1], markLine }]
      : []
  }
})

// 切换报告时重新拉取对应场景的历史趋势
onMounted(loadTrend)
watch(() => props.report?.task?.id, loadTrend)
</script>
