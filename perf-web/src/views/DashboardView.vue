<template>
  <div>
    <div class="page-title">
      <LayoutDashboard class="w-5 h-5 text-neon-green" />
      总览
      <span class="text-xs font-normal text-fg-sub ml-2">实时性能检测 · 压测 · 容量 · AI 优化闭环</span>
    </div>

    <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mb-6">
      <div v-for="m in metrics" :key="m.label" class="metric-card">
        <div class="flex items-center justify-between mb-3">
          <span class="text-xs text-fg-sub">{{ m.label }}</span>
          <component :is="m.icon" class="w-4 h-4 text-neon-green/70" />
        </div>
        <div class="text-2xl font-bold neon-text">{{ m.value }}</div>
        <div class="text-[11px] text-fg-sub mt-1">{{ m.hint }}</div>
      </div>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-5 xl:col-span-2">
        <div class="flex items-center justify-between mb-3">
          <div class="text-sm font-medium">JVM 堆内存趋势（近 1 小时）</div>
          <button class="text-xs text-neon-blue hover:underline" @click="refresh">刷新</button>
        </div>
        <MetricChart :option="heapOption" height="260px" />
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4 flex items-center gap-2">
          <Bot class="w-4 h-4 text-neon-green" /> AI 快捷指令
        </div>
        <div class="space-y-3">
          <button
            v-for="q in quickActions" :key="q.label"
            class="w-full text-left px-4 py-3 rounded-lg bg-white/5 border border-white/10 hover:border-neon-green/40 hover:bg-neon-green/5 transition-all"
            @click="goAi(q.label)"
          >
            <div class="text-sm text-fg-main">{{ q.label }}</div>
            <div class="text-[11px] text-fg-sub mt-0.5">{{ q.desc }}</div>
          </button>
        </div>
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3 flex items-center gap-2">
          <Gauge class="w-4 h-4 text-neon-blue" /> 最近压测任务
        </div>
        <div v-if="!stressTasks.length" class="text-xs text-fg-sub py-6 text-center">暂无压测任务</div>
        <div v-else class="space-y-2">
          <div v-for="t in stressTasks" :key="t.id" class="flex items-center justify-between px-3 py-2 rounded-lg bg-white/5">
            <div class="min-w-0">
              <div class="text-xs text-fg-main truncate">{{ t.taskName || t.targetUrl }}</div>
              <div class="text-[11px] text-fg-sub">{{ t.concurrency }} 并发 · {{ t.durationSec }}s</div>
            </div>
            <span class="text-[11px] px-2 py-0.5 rounded-full shrink-0" :class="statusClass(t.status)">{{ t.status }}</span>
          </div>
        </div>
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3 flex items-center gap-2">
          <Database class="w-4 h-4 text-neon-blue" /> 最近慢 SQL
        </div>
        <div v-if="!slowSqls.length" class="text-xs text-fg-sub py-6 text-center">暂无慢 SQL</div>
        <div v-else class="space-y-2">
          <div v-for="s in slowSqls" :key="s.id" class="px-3 py-2 rounded-lg bg-white/5">
            <div class="text-xs text-fg-main truncate font-mono">{{ s.sqlText }}</div>
            <div class="text-[11px] mt-1 flex items-center justify-between">
              <span class="text-fg-sub">{{ s.source }}</span>
              <span class="text-amber-400">{{ Number(s.costMs).toFixed(1) }}ms</span>
            </div>
          </div>
        </div>
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3 flex items-center gap-2">
          <Bug class="w-4 h-4 text-amber-400" /> 最近性能问题
        </div>
        <div v-if="!issues.length" class="text-xs text-fg-sub py-6 text-center">暂无性能问题</div>
        <div v-else class="space-y-2">
          <div v-for="i in issues" :key="i.id" class="flex items-start justify-between gap-2 px-3 py-2 rounded-lg bg-white/5">
            <div class="min-w-0">
              <div class="text-xs text-fg-main truncate">{{ i.title }}</div>
              <div class="text-[11px] text-fg-sub mt-0.5">{{ i.issueType }} · {{ i.severity }}</div>
            </div>
            <span class="text-[11px] px-2 py-0.5 rounded-full shrink-0" :class="statusClass(i.status)">{{ i.status }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { LayoutDashboard, Activity, Gauge, Database, Bug, Bot, Cpu, Users, Zap } from 'lucide-vue-next'
import MetricChart from '../components/charts/MetricChart.vue'
import { appApi, stressApi, slowSqlApi, sourceApi, jvmApi } from '../api'

const router = useRouter()
const apps = ref<Array<{ appName: string; status: string }>>([])
const stressTasks = ref<Array<Record<string, any>>>([])
const slowSqls = ref<Array<Record<string, any>>>([])
const issues = ref<Array<Record<string, any>>>([])
const jvmData = ref<Array<Record<string, any>>>([])

const metrics = computed(() => [
  { label: '接入项目', value: apps.value.length, hint: '已注册实例', icon: Users },
  { label: '压测任务', value: stressTasks.value.length, hint: '最近任务', icon: Gauge },
  { label: '慢 SQL 数', value: slowSqls.value.length, hint: '近 1 小时', icon: Database },
  { label: '待处理问题', value: issues.value.filter((i) => i.status === 'open').length, hint: '未分析', icon: Bug },
  { label: 'JVM 采样点', value: jvmData.value.length, hint: '近 1 小时', icon: Cpu },
  { label: '在线状态', value: '运行中', hint: 'perf-server', icon: Zap }
])

const quickActions = [
  { label: '分析最近慢 SQL 根因并给出修复方案', desc: '调用链 + 源码关联 + 大模型多方案' },
  { label: '当前性能能承受多少并发、何时扩容', desc: '容量评估 + 扩容时机建议' },
  { label: '生成一份性能检测报告', desc: '汇总压测 / JVM / 慢 SQL / 问题' }
]

const heapOption = computed(() => {
  const pts = [...jvmData.value].reverse()
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 16, bottom: 24 },
    xAxis: {
      type: 'category',
      data: pts.map((p) => new Date(Number(p.collectTime)).toLocaleTimeString('zh-CN', { hour12: false })),
      axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } },
      axisLabel: { color: '#8A94A6', fontSize: 11 }
    },
    yAxis: {
      type: 'value', name: 'MB',
      axisLabel: { color: '#8A94A6', fontSize: 11 },
      splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } }
    },
    series: [
      {
        name: '堆已用', type: 'line', smooth: true, showSymbol: false,
        data: pts.map((p) => Number(p.heapUsedMb)),
        lineStyle: { color: '#00E5A0', width: 2 },
        areaStyle: { color: 'rgba(0,229,160,0.15)' },
        itemStyle: { color: '#00E5A0' }
      },
      {
        name: '堆上限', type: 'line', smooth: true, showSymbol: false,
        data: pts.map((p) => Number(p.heapMaxMb)),
        lineStyle: { color: '#0EA5E9', width: 1, type: 'dashed' },
        itemStyle: { color: '#0EA5E9' }
      }
    ]
  }
})

function statusClass(s: string) {
  const map: Record<string, string> = {
    running: 'text-neon-green bg-neon-green/10',
    success: 'text-neon-green bg-neon-green/10',
    failed: 'text-red-400 bg-red-400/10',
    analyzing: 'text-neon-blue bg-neon-blue/10',
    open: 'text-amber-400 bg-amber-400/10'
  }
  return map[s] || 'text-fg-sub bg-white/5'
}

async function refresh() {
  const [a, t, s, i, j] = await Promise.allSettled([
    appApi.list(), stressApi.list({ limit: 5 }), slowSqlApi.list({ limit: 5 }),
    sourceApi.issues({ limit: 5 }), jvmApi.metrics({ limit: 120 })
  ])
  if (a.status === 'fulfilled') apps.value = a.value.data
  if (t.status === 'fulfilled') stressTasks.value = t.value.data
  if (s.status === 'fulfilled') slowSqls.value = s.value.data
  if (i.status === 'fulfilled') issues.value = i.value.data
  if (j.status === 'fulfilled') jvmData.value = j.value.data
}

function goAi(message: string) {
  router.push({ path: '/ai', query: { msg: message } })
}

onMounted(refresh)
</script>
