<template>
  <div>
    <div class="page-title">
      <GitBranch class="w-5 h-5 text-neon-green" />
      调用链分析
      <span class="text-xs font-normal text-fg-sub ml-2">Controller → Service → DAO 耗时分布 · 火焰图 · 慢 SQL</span>
    </div>

    <div class="glass-card p-4 mb-4 flex flex-wrap items-center gap-4">
      <label class="text-xs text-fg-sub">应用</label>
      <input v-model="query.app" class="input-text w-36" placeholder="如 perf-demo" />
      <label class="text-xs text-fg-sub">方法</label>
      <input v-model="query.method" class="input-text w-52" placeholder="如 listUsers" />
      <button class="btn-primary" @click="loadTraces">查询</button>
      <div class="flex-1"></div>
      <div class="flex gap-1">
        <button
          v-for="tab in tabs" :key="tab.key"
          class="px-4 py-1.5 rounded-lg text-xs transition-all"
          :class="activeTab === tab.key ? 'bg-neon-green/15 text-neon-green border border-neon-green/30' : 'text-fg-sub border border-white/10 hover:text-fg-main'"
          @click="activeTab = tab.key"
        >{{ tab.label }} <span v-if="tab.key === 'slow'" class="opacity-70">{{ slowSqls.length }}</span></button>
      </div>
    </div>

    <div v-if="activeTab === 'trace'" class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-4">
        <div class="text-sm font-medium mb-3">调用链列表（{{ traces.length }}）</div>
        <div v-if="!traces.length" class="text-xs text-fg-sub py-8 text-center">暂无调用链数据</div>
        <div v-else class="space-y-2 max-h-[520px] overflow-y-auto pr-1">
          <div
            v-for="t in traces" :key="t.id"
            class="px-3 py-2.5 rounded-lg cursor-pointer transition-all border"
            :class="selectedId === t.id ? 'bg-neon-green/10 border-neon-green/40' : 'bg-white/5 border-white/10 hover:border-neon-green/30'"
            @click="selectTrace(t)"
          >
            <div class="text-xs text-fg-main truncate font-mono">{{ t.rootMethod }}</div>
            <div class="flex justify-between mt-1.5 text-[11px]">
              <span class="text-fg-sub truncate max-w-[55%]">{{ t.traceId }}</span>
              <span class="text-amber-400 shrink-0">{{ t.totalMs }}ms</span>
            </div>
          </div>
        </div>
      </div>

      <div class="glass-card p-4 xl:col-span-2">
        <div v-if="!detail" class="text-xs text-fg-sub py-16 text-center">选择左侧调用链查看详情</div>
        <template v-else>
          <div class="text-sm font-medium mb-3 flex items-center justify-between">
            <span class="truncate font-mono">{{ detail.rootMethod }}</span>
            <span class="text-xs text-amber-400">总耗时 {{ detail.totalMs }}ms</span>
          </div>
          <div class="text-xs text-fg-sub mb-3">调用树（占比 = 子节点耗时 / 父节点耗时）</div>
          <div class="max-h-[280px] overflow-y-auto tree-bg rounded-lg p-3">
            <TraceTree v-if="treeNode" :node="treeNode" :depth="0" />
          </div>
          <div class="text-sm font-medium mt-4 mb-2">火焰图</div>
          <div v-if="treeNode" class="h-[260px]">
            <MetricChart :option="flameOption" height="260px" />
          </div>
        </template>
      </div>
    </div>

    <div v-else>
      <div class="glass-card p-4">
        <div class="flex items-center justify-between mb-3">
          <div class="text-sm font-medium">慢 SQL 列表</div>
          <div class="flex items-center gap-2">
            <label class="text-xs text-fg-sub">最小耗时</label>
            <select v-model="minCostMs" class="input-select w-28" @change="loadSlowSqls">
              <option :value="0">全部</option>
              <option :value="100">100ms</option>
              <option :value="500">500ms</option>
              <option :value="1000">1s</option>
            </select>
          </div>
        </div>
        <div v-if="!slowSqls.length" class="text-xs text-fg-sub py-10 text-center">暂无慢 SQL</div>
        <div v-else class="space-y-2">
          <div v-for="s in slowSqls" :key="s.id" class="flex items-start gap-3 px-4 py-3 rounded-lg bg-white/5">
            <div class="text-2xl font-bold shrink-0"
              :class="Number(s.costMs) > 1000 ? 'text-red-400' : 'text-amber-400'">{{ Number(s.costMs).toFixed(0) }}ms</div>
            <div class="min-w-0 flex-1">
              <div class="text-xs text-fg-main font-mono break-all">{{ s.sqlText }}</div>
              <div class="text-[11px] text-fg-sub mt-1.5 flex flex-wrap gap-3">
                <span class="px-2 py-0.5 rounded bg-white/5">{{ s.source }}</span>
                <span v-if="s.appName">应用：{{ s.appName }}</span>
                <span v-if="s.dbHost">DB：{{ s.dbHost }}</span>
                <span v-if="s.executeTime">{{ fmtTime(s.executeTime) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { GitBranch } from 'lucide-vue-next'
import MetricChart from '../components/charts/MetricChart.vue'
import TraceTree from '../components/TraceTree.vue'
import { traceApi, slowSqlApi, appApi } from '../api'

const tabs = [
  { key: 'trace', label: '调用链' },
  { key: 'slow', label: '慢 SQL' }
]
const activeTab = ref('trace')
const query = ref<{ app: string; method: string }>({ app: '', method: '' })
const traces = ref<Array<Record<string, any>>>([])
const detail = ref<Record<string, any> | null>(null)
const treeNode = ref<Record<string, any> | null>(null)
const slowSqls = ref<Array<Record<string, any>>>([])
const minCostMs = ref(0)
const selectedId = ref<number>()

const flameOption = computed(() => {
  if (!treeNode.value) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { formatter: (p: any) => `${p.name}<br/>${p.value}ms` },
    series: [{
      type: 'sunburst',
      radius: ['12%', '95%'],
      sort: null,
      emphasis: { focus: 'ancestor' },
      data: [flameNode(treeNode.value as Record<string, any>)],
      label: { color: '#E6EDF7', fontSize: 11 },
      itemStyle: { borderColor: 'rgba(11,18,32,0.8)', borderWidth: 1 },
      levels: [
        {},
        { color: ['#00E5A0', '#0EA5E9', '#34D399', '#FAAD14'] },
        { color: ['#0EA5E9', '#1677FF', '#00E5A0', '#52C41A'] }
      ]
    }]
  }
})

function flameNode(n: Record<string, any>): Record<string, any> {
  return {
    name: n.method || 'root',
    value: Math.max(Number(n.costMs) || 0, 1),
    children: (n.children || []).map(flameNode)
  }
}

async function loadTraces() {
  const end = Date.now()
  const [t, a] = await Promise.allSettled([
    traceApi.list({ app: query.value.app || undefined, method: query.value.method || undefined, start: end - 86400000, end, limit: 50 }),
    appApi.list()
  ])
  if (t.status === 'fulfilled') traces.value = t.value.data
  if (a.status === 'fulfilled' && !query.value.app && a.value.data.length) {
    query.value.app = a.value.data[0].appName
  }
}

async function selectTrace(t: Record<string, any>) {
  selectedId.value = t.id
  const r = await traceApi.detail(t.traceId)
  detail.value = r.data
  if (r.data?.traceJson) {
    try {
      treeNode.value = typeof r.data.traceJson === 'string' ? JSON.parse(r.data.traceJson) : r.data.traceJson
    } catch {
      treeNode.value = null
    }
  }
}

async function loadSlowSqls() {
  const end = Date.now()
  const r = await slowSqlApi.list({
    app: query.value.app || undefined,
    start: end - 86400000,
    end,
    minCostMs: minCostMs.value || undefined,
    limit: 50
  })
  slowSqls.value = r.data
}

function fmtTime(t: number | string) {
  return new Date(Number(t)).toLocaleString('zh-CN', { hour12: false })
}

onMounted(() => {
  loadTraces()
  loadSlowSqls()
})
</script>
