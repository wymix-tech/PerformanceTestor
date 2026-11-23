<template>
  <div>
    <div class="page-title">
      <GitBranch class="w-5 h-5 text-neon-green" />
      调用链分析
      <span class="text-xs font-normal text-fg-sub ml-2">Controller → Service → DAO 耗时分布 · 火焰图 · 慢 SQL · CPU 热点</span>
    </div>

    <div class="glass-card p-4 mb-4 flex flex-wrap items-center gap-4">
      <label class="text-xs text-fg-sub">项目</label>
      <select v-model="query.app" class="input-select w-44" @change="loadTraces">
        <option v-for="name in projectNames" :key="name" :value="name">{{ name }}</option>
      </select>
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
        >{{ tab.label }} <span v-if="tab.key === 'slow'" class="opacity-70">{{ slowItems.length }}</span></button>
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
          <!-- 游标分页：hasMore=false 时不渲染按钮，避免"点了没反应" -->
          <button
            v-if="tracesHasMore"
            class="w-full py-2 rounded-lg text-xs text-fg-sub border border-white/10 hover:text-neon-green hover:border-neon-green/30 transition-all disabled:opacity-50"
            :disabled="tracesLoading"
            @click="loadMoreTraces"
          >{{ tracesLoading ? '加载中…' : '加载更多' }}</button>
          <div v-else class="text-[11px] text-fg-sub text-center py-1">已到最后一条</div>
        </div>
      </div>

      <div class="glass-card p-4 xl:col-span-2">
        <div v-if="!detail" class="text-xs text-fg-sub py-16 text-center">选择左侧调用链查看详情</div>
        <template v-else>
          <div class="text-sm font-medium mb-3 flex items-center justify-between">
            <span class="truncate font-mono">{{ detail.rootMethod }}</span>
            <span class="text-xs text-amber-400">总耗时 {{ rootCostText }}ms</span>
          </div>
          <div class="text-xs text-fg-sub mb-3">调用树（时间轴 = 根节点总耗时 100% · 条左端 = 调用开始时刻 · 长度 = 耗时占比）</div>
          <div class="max-h-[280px] overflow-y-auto tree-bg rounded-lg p-3">
            <TraceTree v-if="treeNode" :node="treeNode" :depth="0" :root-cost="rootCost" />
          </div>
          <div class="text-sm font-medium mt-4 mb-2">火焰图<span class="text-xs font-normal text-fg-sub ml-2">X 轴 = 根节点总耗时 100% · 左端 = 调用开始时刻 · 宽度 = 耗时占比 · 自上而下为调用栈深度</span></div>
          <div v-if="treeNode">
            <MetricChart :option="flameOption" :height="flameHeight + 'px'" />
          </div>
        </template>
      </div>
    </div>

    <!-- 慢命令合并视图：慢 SQL（数据库）+ 中间件慢命令（Redis/ES/MQ），统一按耗时倒序 -->
    <div v-else-if="activeTab === 'slow'">
      <div class="glass-card p-4">
        <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
          <div class="text-sm font-medium">慢命令列表（合并）</div>
          <div class="flex flex-wrap items-center gap-2">
            <label class="text-xs text-fg-sub">类型</label>
            <select v-model="slowType" class="input-select w-32" @change="loadSlowItems">
              <option value="">全部</option>
              <option value="sql">SQL</option>
              <option value="redis">Redis</option>
              <option value="es">ES</option>
              <option value="mq">MQ</option>
            </select>
            <label class="text-xs text-fg-sub ml-2">最小耗时</label>
            <select v-model="minCostMs" class="input-select w-28" @change="loadSlowItems">
              <option :value="0">全部</option>
              <option :value="100">100ms</option>
              <option :value="500">500ms</option>
              <option :value="1000">1s</option>
            </select>
          </div>
        </div>
        <div class="text-[11px] text-fg-sub mb-3">
          中间件慢命令（Redis / ES / MQ）需被测应用以 <span class="font-mono">middlewareEnabled=true</span> 启动 agent 才有数据
        </div>
        <div v-if="!slowItems.length" class="text-xs text-fg-sub py-10 text-center">暂无慢命令</div>
        <div v-else class="space-y-2">
          <div v-for="s in slowItems" :key="s.kind + '-' + s.id" class="flex items-start gap-3 px-4 py-3 rounded-lg bg-white/5">
            <div class="text-2xl font-bold shrink-0"
              :class="Number(s.costMs) > 1000 ? 'text-red-400' : 'text-amber-400'">{{ Number(s.costMs).toFixed(0) }}ms</div>
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 mb-1">
                <span class="text-[10px] px-1.5 py-0.5 rounded font-mono" :class="typeBadgeClass(s.type)">{{ typeLabel(s.type) }}</span>
              </div>
              <div class="text-xs text-fg-main font-mono break-all">{{ s.text }}</div>
              <div v-if="formatChain(s.methodChain)" class="text-[11px] mt-1.5 truncate" :title="typeof s.methodChain === 'string' ? s.methodChain : JSON.stringify(s.methodChain)">
                <span class="text-fg-sub">调用链：</span>
                <span class="text-neon-blue font-mono">{{ formatChain(s.methodChain) }}</span>
              </div>
              <div class="text-[11px] text-fg-sub mt-1.5 flex flex-wrap gap-3">
                <span v-if="s.source" class="px-2 py-0.5 rounded bg-white/5">{{ s.source }}</span>
                <span v-if="s.appName">应用：{{ s.appName }}</span>
                <span v-if="s.dbHost">DB：{{ s.dbHost }}</span>
                <span v-if="s.traceId" class="font-mono" :title="s.traceId">链路：{{ s.traceId.slice(-8) }}</span>
                <span v-if="s.executeTime">{{ fmtTime(s.executeTime) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- CPU 热点 Tab：agent 弱采样的方法级 CPU 帧命中排行 + JFR 火焰图（P2-1 双视图） -->
    <div v-else-if="activeTab === 'hotspot'">
      <div class="glass-card p-4">
        <div class="flex flex-wrap items-center justify-between gap-3 mb-3">
          <div class="text-sm font-medium">CPU 热点（最近 1 小时）</div>
          <div class="flex flex-wrap items-center gap-2">
            <!-- 剖析维度切换：cpu / alloc / lock -->
            <div class="flex gap-1">
              <button
                v-for="pt in profileTypes" :key="pt.key"
                class="px-3 py-1 rounded-lg text-xs transition-all"
                :class="profileType === pt.key ? 'bg-neon-green/15 text-neon-green border border-neon-green/30' : 'text-fg-sub border border-white/10 hover:text-fg-main'"
                @click="switchProfileType(pt.key)"
              >{{ pt.label }}</button>
            </div>
            <div class="flex gap-1 ml-2">
              <button
                v-for="v in viewModes" :key="v.key"
                class="px-3 py-1 rounded-lg text-xs transition-all"
                :class="hotspotView === v.key ? 'bg-neon-blue/15 text-neon-blue border border-neon-blue/30' : 'text-fg-sub border border-white/10 hover:text-fg-main'"
                @click="hotspotView = v.key"
              >{{ v.label }}</button>
            </div>
          </div>
        </div>
        <div class="text-[11px] text-fg-sub mb-3">
          火焰图需被测应用以 <span class="font-mono">hotspotEnabled=true</span> 且
          <span class="font-mono">profilerMode=jfr</span> 启动 agent（JFR 连续剖析）；
          列表为叶帧排行（兼容旧 HotspotSampler）
        </div>

        <!-- 火焰图视图：JFR 折叠栈（cpu / alloc / lock 三维度切换） -->
        <div v-if="hotspotView === 'flame'">
          <div v-if="!profileLoaded" class="text-xs text-fg-sub py-10 text-center">加载中…</div>
          <template v-else>
            <FlameGraph
              :folded="profileFolded"
              :total-label="profileType === 'alloc' ? '字节' : profileType === 'lock' ? '毫秒' : '样本'"
            />
            <div v-if="profileSummary" class="text-[11px] text-fg-sub mt-2">
              快照 {{ profileSummary.snapshotCount }} 批 · 唯一栈 {{ profileSummary.uniqueStacks }} 条 ·
              总量 {{ profileSummary.totalSamples }}
            </div>
          </template>
        </div>

        <!-- 列表视图：叶帧热点排行（原有展示零破坏） -->
        <template v-else>
          <div v-if="!hotspots.length" class="text-xs text-fg-sub py-10 text-center">暂无 CPU 热点数据</div>
          <template v-else>
            <MetricChart :option="hotspotOption" :height="Math.max(240, hotspots.length * 26) + 'px'" />
            <div class="mt-3 space-y-1.5 max-h-[320px] overflow-y-auto pr-1">
              <div v-for="(h, i) in hotspots" :key="h.methodName"
                class="flex items-center gap-3 px-3 py-2 rounded-lg bg-white/5">
                <span class="text-[11px] w-6 text-right text-fg-sub shrink-0">{{ i + 1 }}</span>
                <div class="text-xs font-mono text-fg-main truncate flex-1" :title="h.methodName">{{ h.methodName }}</div>
                <div class="text-xs text-neon-green shrink-0">{{ h.samples }} 次</div>
                <div class="w-32 h-1.5 rounded bg-white/5 overflow-hidden shrink-0">
                  <div class="h-full bg-gradient-to-r from-neon-green to-neon-teal rounded"
                    :style="{ width: (h.samples / hotspots[0].samples) * 100 + '%' }"></div>
                </div>
              </div>
            </div>
          </template>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { GitBranch } from 'lucide-vue-next'
import MetricChart from '../components/charts/MetricChart.vue'
import FlameGraph from '../components/charts/FlameGraph.vue'
import TraceTree from '../components/TraceTree.vue'
import { fmtTime } from '../utils/format'
import { traceApi, slowSqlApi, slowCommandApi, hotspotApi, profileApi, sourceApi } from '../api'
import { useRepoStore } from '../stores/repoStore'

const tabs = [
  { key: 'trace', label: '调用链' },
  { key: 'slow', label: '慢命令' },
  { key: 'hotspot', label: 'CPU 热点' }
]
const activeTab = ref('trace')
const query = ref<{ app: string; method: string }>({ app: '', method: '' })
// 项目下拉数据源 = 源码仓库名
const projectNames = ref<string[]>([])
const repoStore = useRepoStore()
const traces = ref<Array<Record<string, any>>>([])
/** 调用链游标分页状态：cursor 为 null 表示已到末页 */
const tracesCursor = ref<string | null>(null)
const tracesHasMore = ref(false)
const tracesLoading = ref(false)
const detail = ref<Record<string, any> | null>(null)
const treeNode = ref<Record<string, any> | null>(null)
const slowSqls = ref<Array<Record<string, any>>>([])
/** 中间件慢命令（Redis / ES / MQ） */
const slowCommands = ref<Array<Record<string, any>>>([])
/** 类型过滤：'' 全部 / sql / redis / es / mq */
const slowType = ref('')
const minCostMs = ref(0)
const selectedId = ref<number>()
/** CPU 热点方法排行（agent HotspotSampler 弱采样） */
const hotspots = ref<Array<Record<string, any>>>([])

// ---- JFR 火焰图状态（P2-1）：维度切换 + 双视图 ----
const profileTypes = [
  { key: 'cpu', label: 'CPU' },
  { key: 'alloc', label: '分配' },
  { key: 'lock', label: '锁竞争' }
] as const
const viewModes = [
  { key: 'flame', label: '火焰图' },
  { key: 'list', label: '列表' }
] as const
const hotspotView = ref<'flame' | 'list'>('flame')
const profileType = ref<'cpu' | 'alloc' | 'lock'>('cpu')
const profileFolded = ref('')
const profileSummary = ref<Record<string, any> | null>(null)
const profileLoaded = ref(false)

/** 切换剖析维度：重拉对应 profileType 的折叠栈聚合 */
function switchProfileType(key: 'cpu' | 'alloc' | 'lock') {
  if (profileType.value === key) return
  profileType.value = key
  loadProfile()
}

/** JFR 折叠栈聚合加载（服务端已跨批合并，前端直渲） */
async function loadProfile() {
  profileLoaded.value = false
  const end = Date.now()
  try {
    const r = await profileApi.query({
      app: query.value.app || undefined,
      profileType: profileType.value,
      start: end - 3600000,
      end,
      format: 'folded'
    })
    profileSummary.value = r.data
    profileFolded.value = r.data?.folded ?? ''
  } catch {
    profileSummary.value = null
    profileFolded.value = ''
  } finally {
    profileLoaded.value = true
  }
}

// 总耗时 = 根节点（Controller 层）自身耗时；旧数据兜底用后端 totalMs
const rootCost = computed(() => {
  const rc = Number(treeNode.value?.costMs)
  if (Number.isFinite(rc) && rc > 0) return rc
  return Number(detail.value?.totalMs) || 0
})
const rootCostText = computed(() => rootCost.value.toFixed(3).replace(/0+$/, '').replace(/\.$/, ''))

// 火焰图条形色板（与调用树层级色一致）
const flameColors = ['#00E5A0', '#0EA5E9', '#34D399', '#FAAD14', '#1677FF', '#52C41A', '#F5222D']

// 火焰图数据项：start/width 为相对根总耗时的百分比
interface FlameItem {
  name: string
  depth: number
  start: number
  width: number
  costMs: number
  offsetMs: number
  color: string
}

const flameItems = computed(() => {
  if (!treeNode.value) return [] as FlameItem[]
  const total = rootCost.value || Number(treeNode.value.costMs) || 0
  if (total <= 0) return [] as FlameItem[]
  const items: FlameItem[] = []
  const walk = (n: Record<string, any>, depth: number) => {
    // 与调用树一致的时间轴：起点 = offsetMs 占比，宽度 = costMs 占比（基准为根节点总耗时）
    const offset = Number(n.offsetMs) || 0
    const cost = Number(n.costMs) || 0
    const start = Math.min(Math.max((offset / total) * 100, 0), 99.7)
    const width = Math.max(Math.min((cost / total) * 100, 100 - start), 0.3)
    items.push({
      name: String(n.method || 'root'),
      depth,
      start,
      width,
      costMs: cost,
      offsetMs: offset,
      color: flameColors[depth % flameColors.length]
    })
    for (const c of n.children || []) walk(c, depth + 1)
  }
  walk(treeNode.value, 0)
  return items
})

// 层高 22px，容器高度随调用栈深度自适应
const flameHeight = computed(() => {
  const items = flameItems.value
  if (!items.length) return 80
  return Math.max(...items.map(i => i.depth)) * 22 + 42
})

const flameOption = computed(() => {
  const items = flameItems.value
  if (!items.length) return {}
  const depthCount = Math.max(...items.map(i => i.depth)) + 1
  return {
    backgroundColor: 'transparent',
    grid: { left: 8, right: 8, top: 6, bottom: 6 },
    xAxis: { type: 'value', min: 0, max: 100, show: false },
    yAxis: { type: 'value', min: 0, max: depthCount, inverse: true, show: false },
    tooltip: {
      confine: true,
      backgroundColor: '#182238',
      borderColor: 'rgba(0,229,160,0.3)',
      textStyle: { color: '#E6EDF7', fontSize: 12 },
      formatter: (p: any) => {
        const it = items[p.dataIndex]
        if (!it) return ''
        return (
          `<div style="max-width:460px;white-space:normal;font-family:monospace;margin-bottom:4px">${it.name}</div>` +
          `耗时 ${it.costMs.toFixed(2)}ms · 开始 +${it.offsetMs.toFixed(2)}ms · 占根节点 ${Math.round(it.width)}%`
        )
      }
    },
    series: [
      {
        type: 'custom',
        renderItem: (params: any, api: any) => {
          const it: FlameItem = items[params.dataIndex]
          if (!it) return null
          // 坐标系：x=时间轴百分比 0~100，y=栈深度（inverse 根在顶）
          const p1 = api.coord([it.start, it.depth])
          const s = api.size([it.width, 1])
          const x = p1[0]
          const y = p1[1] + 1
          const w = Math.max(s[0] - 1, 2)
          const h = Math.max(s[1] - 4, 14)
          // 按矩形宽度截断方法名（等宽字体约 6px/字符，留边距）
          const maxChars = Math.max(Math.floor((w - 8) / 6), 0)
          const label = maxChars < 4 ? '' : it.name.length > maxChars ? it.name.slice(0, maxChars - 1) + '…' : it.name
          const children: any[] = [
            {
              type: 'rect',
              shape: { x, y, width: w, height: h, r: 2 },
              style: { fill: it.color, stroke: 'rgba(11,18,32,0.9)', lineWidth: 1 },
              cursor: 'pointer'
            }
          ]
          if (label) {
            children.push({
              type: 'text',
              style: {
                text: label,
                x: x + 4,
                y: y + h / 2,
                fill: '#0B1220',
                fontSize: 10,
                fontFamily: 'Menlo, Consolas, monospace',
                verticalAlign: 'middle'
              },
              silent: true
            })
          }
          return { type: 'group', children }
        },
        data: items,
        clip: true
      }
    ]
  }
})

/** 首屏加载：重置游标后拉第一页 */
async function loadTraces() {
  tracesCursor.value = null
  await fetchTraces(false)
}

/** 加载更多：带上上一页的 nextCursor 追加，不重置已加载内容 */
async function loadMoreTraces() {
  if (!tracesHasMore.value || tracesLoading.value) return
  await fetchTraces(true)
}

/**
 * 拉取调用链一页。
 * @param append true = 翻下一页追加；false = 首屏覆盖
 */
async function fetchTraces(append: boolean) {
  tracesLoading.value = true
  const end = Date.now()
  try {
    const page = await traceApi.list({
      app: query.value.app || undefined,
      method: query.value.method || undefined,
      start: end - 86400000,
      end,
      size: 50,
      cursor: append ? tracesCursor.value ?? undefined : undefined
    })
    traces.value = append ? [...traces.value, ...page.items] : page.items
    tracesCursor.value = page.nextCursor
    tracesHasMore.value = page.hasMore
  } catch {
    if (!append) traces.value = []
    tracesHasMore.value = false
  } finally {
    tracesLoading.value = false
  }
}

/** 项目下拉初始化：全局缓存加载（消除重复拉取），首次加载后默认选中第一个项目 */
async function initProjects() {
  await repoStore.load().catch(() => {})
  if (projectNames.value.length) return
  const names = repoStore.repos.map((r: Record<string, any>) => r.repoName).filter(Boolean)
  projectNames.value = names
  if (!query.value.app && names.length) {
    query.value.app = names[0]
    await loadTraces()
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

/**
 * 慢命令合并加载：慢 SQL + 中间件慢命令并行拉取，类型过滤在前端合并后统一执行。
 *
 * <p>按新的游标分页接口取首屏（size=500 上限）：慢命令页签已有类型/耗时双过滤，
 * 再加一层游标翻页会让界面同时维护三套过滤状态；这里取上限一屏，
 * 需要深翻历史时由调用方按过滤条件缩小窗口。
 */
async function loadSlowItems() {
  const end = Date.now()
  const params = { start: end - 86400000, end, minCostMs: minCostMs.value || undefined, size: 500 }
  const app = query.value.app || undefined
  const [sql, cmd] = await Promise.all([
    slowSqlApi.list({ ...params, app }),
    // 选定 SQL 时无需拉中间件命令；选定具体中间件类型时交由后端过滤（减少传输）
    slowType.value === 'sql'
      ? Promise.resolve({ items: [] as Array<Record<string, any>> })
      : slowCommandApi.list({ ...params, app, type: slowType.value || undefined })
  ])
  slowSqls.value = sql.items
  slowCommands.value = cmd.items
}

/** 合并后的慢命令列表（慢 SQL 与中间件命令统一结构，按耗时倒序） */
const slowItems = computed(() => {
  const merged: Array<Record<string, any>> = []
  for (const s of slowSqls.value) {
    merged.push({ ...s, kind: 'sql', type: 'sql', text: s.sqlText })
  }
  for (const c of slowCommands.value) {
    merged.push({ ...c, kind: 'cmd', type: c.type, text: c.command })
  }
  merged.sort((a, b) => Number(b.costMs ?? 0) - Number(a.costMs ?? 0))
  return slowType.value ? merged.filter(i => i.type === slowType.value) : merged
})

/** 类型标签文案 */
function typeLabel(type: string): string {
  return { sql: 'SQL', redis: 'Redis', es: 'ES', mq: 'MQ' }[type] ?? type
}

/** 类型标签配色（不同中间件一眼可辨） */
function typeBadgeClass(type: string): string {
  return {
    sql: 'bg-neon-blue/15 text-neon-blue',
    redis: 'bg-red-400/15 text-red-400',
    es: 'bg-amber-400/15 text-amber-400',
    mq: 'bg-purple-400/15 text-purple-400'
  }[type] ?? 'bg-white/5 text-fg-sub'
}

// 方法链格式化：methodChain 为 JSON 数组字符串（完整类名#方法），展示为 短类名#方法 → 短类名#方法
function formatChain(chainJson: unknown): string {
  if (!chainJson) return ''
  let arr: any[] = []
  try {
    const parsed = typeof chainJson === 'string' ? JSON.parse(chainJson) : chainJson
    if (Array.isArray(parsed)) arr = parsed
  } catch {
    return ''
  }
  return arr
    .map((m) => {
      const s = String(m)
      const hash = s.lastIndexOf('#')
      if (hash < 0) return s
      const cls = s.slice(0, hash)
      return cls.slice(cls.lastIndexOf('.') + 1) + '#' + s.slice(hash + 1)
    })
    .join(' → ')
}

/** CPU 热点加载（最近 1 小时 top 30） */
async function loadHotspots() {
  try {
    const r = await hotspotApi.list({
      app: query.value.app || undefined,
      limit: 30
    })
    hotspots.value = r.data || []
  } catch {
    hotspots.value = []
  }
}

/** CPU 热点横向条形图（命中次数降序） */
const hotspotOption = computed(() => ({
  backgroundColor: 'transparent',
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
  grid: { left: 8, right: 48, top: 8, bottom: 8, containLabel: true },
  xAxis: {
    type: 'value',
    axisLabel: { color: '#8A94A6', fontSize: 10 },
    splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } }
  },
  yAxis: {
    type: 'category',
    inverse: true,
    data: hotspots.value.map((h) => shortMethod(h.methodName)),
    axisLabel: { color: '#C8D2E0', fontSize: 10 },
    axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } }
  },
  series: [{
    type: 'bar',
    data: hotspots.value.map((h) => h.samples),
    itemStyle: {
      color: {
        type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
        colorStops: [
          { offset: 0, color: 'rgba(0,229,160,0.85)' },
          { offset: 1, color: 'rgba(14,165,233,0.9)' }
        ]
      },
      borderRadius: [0, 4, 4, 0]
    },
    label: { show: true, position: 'right', color: '#8A94A6', fontSize: 10, formatter: '{c}' },
    barMaxWidth: 18
  }]
}))

/** 方法全限定名 → 简短显示（com.a.b.Foo#bar → Foo#bar） */
function shortMethod(name: string): string {
  if (!name) return ''
  const hash = name.indexOf('#')
  const cls = hash > 0 ? name.slice(0, hash) : name
  const short = cls.slice(cls.lastIndexOf('.') + 1)
  return hash > 0 ? short + name.slice(hash) : short
}

// Tab 切换联动加载（切换项目时同步刷新）
watch(activeTab, (tab) => {
  if (tab === 'hotspot' && !hotspots.value.length) loadHotspots()
  if (tab === 'hotspot') loadProfile()
})
watch(() => query.value.app, () => {
  if (activeTab.value === 'hotspot') {
    loadHotspots()
    loadProfile()
  }
  if (activeTab.value === 'slow') loadSlowItems()
})

onMounted(async () => {
  // 项目下拉可能带上默认项目，届时 initProjects 内已拉过一次首屏，避免重复请求
  await initProjects()
  if (!traces.value.length) loadTraces()
  loadSlowItems()
})
</script>
