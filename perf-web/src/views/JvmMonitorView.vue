<template>
  <div>
    <div class="page-title">
      <Activity class="w-5 h-5 text-neon-green" />
      项目监控
      <span class="text-xs font-normal text-fg-sub ml-2">JVM：堆 / 非堆 · GC · 线程 · CPU（5s 轮询）</span>
    </div>

    <!-- 过滤条 -->
    <div class="glass-card p-4 mb-4 flex flex-wrap items-center gap-4">
      <label class="text-xs text-fg-sub">项目</label>
      <select v-model="app" class="input-select" @change="loadData">
        <option v-for="a in appList" :key="a.appName" :value="a.appName">{{ a.appName }}</option>
      </select>
      <label class="text-xs text-fg-sub">时间范围</label>
      <select v-model="range" class="input-select" @change="loadData">
        <option value="3600000">近 1 小时</option>
        <option value="3600000 * 6">近 6 小时</option>
        <option value="86400000">近 24 小时</option>
      </select>
      <button class="btn-primary" @click="toggleAuto">{{ auto ? '暂停轮询' : '开始轮询' }}</button>
      <button class="text-xs text-neon-blue hover:underline" @click="loadData">立即刷新</button>
    </div>

    <!-- 指标卡 -->
    <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mb-4">
      <div v-for="m in metricCards" :key="m.label" class="metric-card">
        <div class="text-xs text-fg-sub mb-1">{{ m.label }}</div>
        <div class="text-xl font-bold" :class="m.color || 'neon-text'">{{ m.value }}</div>
      </div>
    </div>

    <!-- 图表区 -->
    <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">内存趋势（MB）</div>
        <MetricChart :option="memOption" height="260px" />
      </div>
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">GC 次数 / 耗时</div>
        <MetricChart :option="gcOption" height="260px" />
      </div>
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">线程数</div>
        <MetricChart :option="threadOption" height="240px" />
      </div>
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">CPU 使用率</div>
        <MetricChart :option="cpuOption" height="240px" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Activity } from 'lucide-vue-next'
import MetricChart from '../components/charts/MetricChart.vue'
import { jvmApi, appApi } from '../api'

const app = ref('')
const range = ref(3600000)
const auto = ref(true)
const appList = ref<Array<{ appName: string }>>([])
const rows = ref<Array<Record<string, any>>>([])
let timer: number | undefined

const metricCards = computed(() => {
  const last = rows.value[rows.value.length - 1] || {}
  return [
    { label: '堆已用', value: last.heapUsedMb != null ? `${Number(last.heapUsedMb).toFixed(0)} MB` : '—', color: 'text-neon-green' },
    { label: '堆上限', value: last.heapMaxMb != null ? `${Number(last.heapMaxMb).toFixed(0)} MB` : '—' },
    { label: '非堆', value: last.nonHeapUsedMb != null ? `${Number(last.nonHeapUsedMb).toFixed(0)} MB` : '—' },
    { label: 'Young GC', value: last.youngGcCount != null ? `${last.youngGcCount} 次` : '—' },
    { label: 'Full GC', value: last.fullGcCount != null ? `${last.fullGcCount} 次` : '—', color: last.fullGcCount > 2 ? 'text-amber-400' : '' },
    { label: '线程数', value: last.threadCount != null ? `${last.threadCount}` : '—' }
  ]
})

function seriesData(key: string) {
  return [...rows.value]
    .sort((a, b) => new Date(a.collectTime).getTime() - new Date(b.collectTime).getTime())
    .map((r) => ({
      t: new Date(r.collectTime).toLocaleTimeString('zh-CN', { hour12: false }),
      v: Number(r[key])
    }))
}

function buildOption(datas: Array<{ name: string; key: string; color: string; area?: boolean }>, yName: string) {
  const pts = seriesData(datas[0].key)
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#8A94A6', fontSize: 11 }, top: 0 },
    grid: { left: 44, right: 16, top: 28, bottom: 24 },
    xAxis: {
      type: 'category', data: pts.map((p) => p.t),
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } },
      axisLabel: { color: '#8A94A6', fontSize: 11 }
    },
    yAxis: {
      type: 'value', name: yName,
      axisLabel: { color: '#8A94A6', fontSize: 11 },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } }
    },
    series: datas.map((d) => ({
      name: d.name, type: 'line' as const, smooth: true, showSymbol: false,
      data: seriesData(d.key).map((p) => p.v),
      lineStyle: { color: d.color, width: d.area ? 2 : 1.5 },
      itemStyle: { color: d.color },
      ...(d.area ? { areaStyle: { color: d.color.replace(')', ',0.12)').replace('rgb', 'rgba') } } : {})
    }))
  }
}

const memOption = computed(() =>
  buildOption([
    { name: '堆已用', key: 'heapUsedMb', color: '#00E5A0', area: true },
    { name: '堆上限', key: 'heapMaxMb', color: '#0EA5E9' },
    { name: '非堆', key: 'nonHeapUsedMb', color: '#FAAD14' }
  ], 'MB'))
const gcOption = computed(() =>
  buildOption([
    { name: 'Young GC 耗时', key: 'youngGcMs', color: '#0EA5E9' },
    { name: 'Full GC 耗时', key: 'fullGcMs', color: '#F5222D' }
  ], 'ms'))
const threadOption = computed(() =>
  buildOption([{ name: '线程数', key: 'threadCount', color: '#00E5A0', area: true }], '个'))
const cpuOption = computed(() =>
  buildOption([
    { name: 'JVM CPU', key: 'cpuUsage', color: '#00E5A0', area: true },
    { name: '系统 CPU', key: 'systemCpu', color: '#0EA5E9' }
  ], '%'))

async function loadData() {
  const end = Date.now()
  const start = end - Number(range)
  const [j, a] = await Promise.allSettled([
    jvmApi.metrics({ app: app.value || undefined, start, end, limit: 200 }),
    appApi.list()
  ])
  if (j.status === 'fulfilled') rows.value = j.value.data
  if (a.status === 'fulfilled') {
    appList.value = a.value.data
    // 默认选中第一个项目（项目即应用；不显示"全部"）
    if (!app.value && appList.value.length) {
      app.value = appList.value[0].appName
      await loadData()
      return
    }
  }
}

function toggleAuto() {
  auto.value = !auto.value
  if (auto.value) {
    timer = window.setInterval(loadData, 5000)
    loadData()
  } else if (timer) {
    window.clearInterval(timer)
  }
}

onMounted(() => {
  loadData()
  timer = window.setInterval(loadData, 5000)
})
onUnmounted(() => timer && window.clearInterval(timer))
</script>
