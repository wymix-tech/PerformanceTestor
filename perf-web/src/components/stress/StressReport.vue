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
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MetricChart from '../charts/MetricChart.vue'

/**
 * 压测报告公共组件：指标卡 + QPS/分档曲线 + 容量评估卡片。
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
</script>
