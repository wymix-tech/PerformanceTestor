<template>
  <div :class="mode === 'compact' ? 'space-y-4' : 'grid grid-cols-1 xl:grid-cols-2 gap-4'">
    <div class="glass-card p-5">
      <div class="flex items-center justify-between mb-4">
        <div class="text-sm font-medium">自动灌数据</div>
        <div v-if="autoFilled" class="text-[11px] text-neon-green">✓ 项目与数据库连接已自动带入</div>
      </div>
      <div class="space-y-2.5">
        <!-- 必填字段（连接 / 认证）始终可见，绝不可藏到折叠区 -->
        <div>
          <label class="text-xs text-fg-sub">连接配置（与「问题与优化」页共用，选中自动填充）</label>
          <select v-model="gen.connId" class="input-select w-full" @change="applyConn">
            <option :value="0">— 手动填写连接 —</option>
            <option v-for="c in conns" :key="c.id" :value="c.id">{{ c.connName }}（{{ c.dbUrl }}）</option>
          </select>
        </div>
        <div>
          <label class="text-xs text-fg-sub">JDBC URL（jdbc:mysql:// 或 jdbc:postgresql://）</label>
          <input v-model="gen.dbUrl" class="input-text w-full" placeholder="jdbc:mysql://host:3306/db 或 jdbc:postgresql://host:5432/db" />
        </div>
        <div class="grid grid-cols-2 gap-2.5">
          <div>
            <label class="text-xs text-fg-sub">用户</label>
            <input v-model="gen.dbUser" class="input-text w-full" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">密码</label>
            <input v-model="gen.dbPassword" type="password" class="input-text w-full"
                   :placeholder="gen.connId ? '已选连接：留空则使用库中加密凭据' : ''" />
          </div>
        </div>
        <p v-if="gen.connId" class="text-[11px] text-fg-sub -mt-1">
          已选中保存的连接，密码由服务端按连接配置解密使用（列表中不返回明文）
        </p>
        <div>
          <label class="text-xs text-fg-sub">目标表</label>
          <div class="flex gap-2">
            <select v-model="gen.tableName" class="input-select flex-1" @change="loadColumns">
              <option value="">— 先读取表清单 —</option>
              <option v-for="t in tables" :key="t" :value="t">{{ t }}</option>
            </select>
            <button class="btn-secondary shrink-0 px-3" @click="loadTables">读取表清单</button>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-2.5">
          <div>
            <label class="text-xs text-fg-sub">源码项目（可选）</label>
            <select v-model="gen.repoId" class="input-select w-full">
              <option :value="0">— 不使用源码分析 —</option>
              <option v-for="r in repoStore.repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-fg-sub">行数</label>
            <input v-model.number="gen.rowCount" type="number" class="input-text w-full" min="1" max="1000000" />
          </div>
        </div>

        <!-- 高级选项：仅孤儿率 + JSON 规则——这两项平时极少用到，向导内折叠避免撑高 -->
        <div class="rounded-lg border border-white/10 overflow-hidden">
          <button
            class="w-full flex items-center justify-between px-3 py-2 text-xs text-fg-sub hover:bg-white/5 cursor-pointer"
            @click="advancedOpen = !advancedOpen"
          >
            <span>高级选项（孤儿率 / 字段规则 / 联表表组）</span>
            <span class="text-[10px]">{{ advancedOpen ? '收起 ▲' : '展开 ▼' }}</span>
          </button>
          <div v-show="advancedOpen" class="px-3 pb-3 pt-1 space-y-2.5 border-t border-white/10">
            <div>
              <label class="text-xs text-fg-sub">孤儿率 orphanRate（0~1，可选）</label>
              <input v-model.number="gen.orphanRate" type="number" step="0.01" min="0" max="1" class="input-text w-full" />
            </div>
            <div>
              <label class="text-xs text-fg-sub">字段规则(JSON) — 支持 enum+weights / range+overThreshold / ref / refSkew</label>
              <textarea v-model="gen.ruleJson" rows="3" class="input-text w-full font-mono text-xs"
                placeholder='{"fields":{"user_id":{"type":"refSkew","table":"user","column":"id","hotPercent":20,"hotShare":80},"status":{"type":"enum","values":["PENDING","PAID"],"weights":[20,80]}}}'></textarea>
              <div v-if="skewWarn" class="text-[11px] text-amber-400 mt-1">⚠ 检测到高热点倾斜（refSkew 热行承载过大）：建议降低批量或为子表分区，否则高并发下行锁冲突会抵消批量写入收益</div>
            </div>
            <div>
              <div class="flex items-center justify-between">
                <label class="text-xs text-fg-sub">联表表组(JSON，可选 — 填写后按表组拓扑排序灌数)</label>
                <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="fillTableGroupExample">填入示例</button>
              </div>
              <textarea v-model="gen.tablesJson" rows="3" class="input-text w-full font-mono text-xs"
                placeholder='[{"tableName":"user","rowCount":10000},{"tableName":"order","rowCount":100000,"fields":{"user_id":{"type":"ref","table":"user","column":"id"}}}]'></textarea>
            </div>
          </div>
        </div>

        <!-- 列信息与语义建议：仅在有数据时展开，不占固定高度 -->
        <div v-if="columns.length" class="text-xs text-fg-sub space-y-0.5 max-h-32 overflow-y-auto">
          <div v-for="c in columns" :key="c.name">
            {{ c.name }}（{{ c.type }}）
            <span v-if="c.suggestedValues && c.suggestedValues.length" class="text-neon-green">建议值: {{ c.suggestedValues.join(' / ') }}</span>
            <span v-else-if="c.suspectEnum" class="text-amber-400">疑似枚举列，建议人工配置规则</span>
          </div>
        </div>
        <div v-if="suggestions.length" class="space-y-1.5 max-h-40 overflow-y-auto">
          <div class="text-xs text-fg-sub font-medium">语义建议（低置信关联需确认采纳）</div>
          <div v-for="s in suggestions" :key="s.id" class="flex items-center justify-between px-3 py-2 rounded-lg bg-white/5">
            <div class="text-[11px] text-fg-sub">
              <template v-if="s.hintType === 'relation'">
                {{ s.tableName }}.{{ s.columnName }} → {{ s.parentTable }}.{{ s.parentColumn }}
                <span class="text-neon-blue ml-1">[{{ s.source }}] {{ (s.confidence * 100).toFixed(0) }}%</span>
              </template>
              <template v-else>
                {{ s.tableName }}.{{ s.columnName }} 候选值 {{ s.valuesJson }}
              </template>
            </div>
            <div class="flex gap-2">
              <button class="text-[11px] text-neon-green hover:underline cursor-pointer" @click="adoptHint(s.id, true)">采纳</button>
              <button class="text-[11px] text-fg-sub hover:underline cursor-pointer" @click="adoptHint(s.id, false)">忽略</button>
            </div>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-2.5 pt-1">
          <button class="btn-secondary w-full justify-center" :disabled="busy" @click="startDataGen(true)">试运行（校验预检）</button>
          <button class="btn-primary w-full justify-center" :disabled="busy || !dryRunPassed" @click="startDataGen(false)">开始灌数据</button>
        </div>
        <div v-if="!dryRunPassed" class="text-[11px] text-fg-sub">建议先执行试运行：校验表存在/规则解析/关联识别/环检测与外键池预检，通过后开启正式执行</div>
      </div>
    </div>

    <div v-if="mode !== 'compact'" class="glass-card p-5">
      <div class="text-sm font-medium mb-3">灌数据任务</div>
      <div v-if="!tasks.length" class="text-xs text-fg-sub py-8 text-center">暂无任务</div>
      <div v-else class="space-y-2">
        <div v-for="t in tasks" :key="t.id" class="px-4 py-3 rounded-lg bg-white/5">
          <div class="flex items-center justify-between">
            <div class="text-sm text-fg-main">{{ t.taskName }}</div>
            <div class="flex items-center gap-2">
              <span v-if="t.tablesJson" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue">联表</span>
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="taskStatusClass(t.status)">{{ t.status }}</span>
            </div>
          </div>
          <div class="text-[11px] text-fg-sub mt-1">
            {{ t.tableName }} · {{ t.inserted }}/{{ (t.totalRows || t.rowCount) }} 行 · {{ t.costMs }}ms
          </div>
          <div class="h-1.5 rounded bg-white/5 mt-2 overflow-hidden">
            <div class="h-full bg-neon-green rounded transition-all" :style="{ width: (t.inserted / Math.max(t.totalRows || t.rowCount, 1)) * 100 + '%' }"></div>
          </div>
          <div v-if="t.detailJson" class="text-[11px] mt-1.5 space-y-0.5">
            <div v-if="parseDetail(t.detailJson).dryRunReport" class="text-neon-green">✓ 试运行通过 · 预计 {{ parseDetail(t.detailJson).dryRunReport.totalRows }} 行 · 单表事务估算 {{ parseDetail(t.detailJson).dryRunReport.estimatedMaxTxnMs }}ms</div>
            <div v-for="(w, wi) in parseDetail(t.detailJson).warnings || []" :key="wi" class="text-amber-400">⚠ {{ w }}</div>
            <div v-if="parseDetail(t.detailJson).selfCheck" class="text-fg-sub">
              自检：非空率 {{ ((parseDetail(t.detailJson).selfCheck.nonEmptyRate || 0) * 100).toFixed(0) }}%
              <span v-if="parseDetail(t.detailJson).selfCheck.warning" class="text-amber-400">（{{ parseDetail(t.detailJson).selfCheck.warning }}）</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { dataGenApi, dbConnApi } from '../../api'
import { useRepoStore } from '../../stores/repoStore'
import { taskStatusClass } from '../../utils/format'

/**
 * 自动灌数据表单（数据准备）。
 *
 * <p>压测中心「数据准备」Tab 与快速开始向导「灌注数据」步骤共用同一份实现，
 * 保证两处能力完全一致（连接配置/读取表清单/列建议值/字段规则/联表表组/孤儿率/
 * 语义建议采纳/试运行预检/正式灌数/任务进度与自检）。
 *
 * <p>调用方可通过 initial* 属性预填（如向导把上游步骤选择的项目与部署后
 * agent 上报探测到的数据库连接自动带入）；这些值后续变化会同步到表单，
 * 用户仍可随时手动修改。
 */
const props = withDefaults(defineProps<{
  /** 预填的源码项目 ID（0 = 不使用源码分析） */
  initialRepoId?: number
  /** 预填的数据库连接配置 ID（0 = 手动填写） */
  initialConnId?: number
  /** 预填的 JDBC URL（通常由 agent 上报探测得到） */
  initialDbUrl?: string
  initialDbUser?: string
  initialDbPassword?: string
  /**
   * 布局模式：
   * - full（默认）：两栏，左表单右任务列表（压测中心「数据准备」Tab 用）
   * - compact：单栏，仅表单，隐藏任务列表（快速开始向导内不需要，灌数结果回到压测中心查看）
   */
  mode?: 'full' | 'compact'
  /** 是否轮询刷新任务列表（压测中心需要，向导按需关闭） */
  poll?: boolean
}>(), {
  initialRepoId: 0,
  initialConnId: 0,
  initialDbUrl: '',
  initialDbUser: '',
  initialDbPassword: '',
  mode: 'full',
  poll: false
})

const emit = defineEmits(['created'])

const repoStore = useRepoStore()
const conns = ref<Array<Record<string, any>>>([])
const tables = ref<string[]>([])
const columns = ref<Array<Record<string, any>>>([])
const suggestions = ref<Array<Record<string, any>>>([])
const tasks = ref<Array<Record<string, any>>>([])
const busy = ref(false)
const dryRunPassed = ref(false)

const gen = ref({
  connId: props.initialConnId || 0,
  dbUrl: props.initialDbUrl || '',
  dbUser: props.initialDbUser || 'root',
  dbPassword: props.initialDbPassword || '',
  tableName: '',
  rowCount: props.initialDbUrl ? 10000 : 1000,
  ruleJson: '',
  tablesJson: '',
  orphanRate: 0,
  repoId: props.initialRepoId || 0
})

/** 高级选项是否展开（仅孤儿率/JSON 规则：默认收起，避免向导窄空间内表单过高；
 *  此处的孤儿率/规则/表组是少见的高级字段，常用场景填好上面 4 项就能跑） */
const advancedOpen = ref(false)

/** 是否由调用方自动带入（用于提示用户"已自动填充"） */
const autoFilled = computed(() => !!(props.initialRepoId || props.initialConnId || props.initialDbUrl))

// refSkew 高倾斜警告：hotShare/hotPercent 比值 > 2.5 视为热行承载过大
const skewWarn = computed(() => {
  try {
    const root = JSON.parse(gen.value.ruleJson || '{}')
    const fields = root.fields || root || {}
    return Object.values(fields).some((r: any) =>
      r?.type === 'refSkew' && (r.hotShare || 80) / Math.max(r.hotPercent || 20, 1) > 2.5)
  } catch {
    return false
  }
})

function parseDetail(json: string): any {
  try {
    return typeof json === 'string' ? JSON.parse(json) : json
  } catch {
    return {}
  }
}

function fillTableGroupExample() {
  gen.value.tablesJson = JSON.stringify([
    { tableName: gen.value.tableName || 'user', rowCount: 10000 },
    { tableName: 'order', rowCount: 50000, fields: { user_id: { type: 'ref', table: 'user', column: 'id' } } }
  ], null, 0)
}

/** 连接参数：选中已保存连接时带 connId，由服务端解析加密凭据 */
function connParams() {
  return {
    connId: gen.value.connId || undefined,
    dbUrl: gen.value.dbUrl,
    dbUser: gen.value.dbUser,
    dbPassword: gen.value.dbPassword
  }
}

async function loadTables() {
  try {
    const r = await dataGenApi.tables(connParams())
    tables.value = r.data || []
  } catch { /* 读取失败不阻断 */ }
}

async function loadColumns() {
  if (!gen.value.tableName) return
  try {
    const r = await dataGenApi.columns({ ...connParams(), tableName: gen.value.tableName })
    columns.value = r.data || []
    loadSuggestions()
  } catch { /* 读取失败不阻断 */ }
}

async function loadSuggestions() {
  try {
    const r = await dataGenApi.suggestions({
      repoId: gen.value.repoId || undefined,
      tableName: gen.value.tableName || undefined,
      status: 'pending'
    })
    suggestions.value = r.data || []
  } catch { /* 建议查询失败不阻断 */ }
}

async function adoptHint(id: number, adopt: boolean) {
  try {
    await dataGenApi.adopt(id, adopt)
    await loadSuggestions()
  } catch { /* 采纳失败不阻断 */ }
}

async function loadTasks() {
  try {
    const r = await dataGenApi.list()
    tasks.value = r.data || []
  } catch { /* 加载失败不阻断 */ }
}

async function loadConns() {
  try {
    const r = await dbConnApi.list()
    conns.value = r.data || []
  } catch { /* 连接配置失败不阻断 */ }
}

/**
 * 选择连接配置后自动填连接参数（仍可手改）。
 * 密码字段刻意留空：凭据已加密存储、列表仅返回脱敏值，
 * 真实密码由服务端按 connId 解析（见 DataGenController.applyConn）。
 */
function applyConn() {
  const c = conns.value.find((x) => x.id === gen.value.connId)
  if (c) {
    gen.value.dbUrl = c.dbUrl
    gen.value.dbUser = c.username
    gen.value.dbPassword = ''
  }
}

async function startDataGen(dryRun: boolean) {
  let tables
  if (gen.value.tablesJson && gen.value.tablesJson.trim()) {
    try {
      tables = JSON.parse(gen.value.tablesJson)
    } catch {
      alert('表组 JSON 解析失败，请检查格式')
      return
    }
  }
  if (!gen.value.dbUrl) {
    alert('请先选择连接配置或填写 JDBC URL')
    return
  }
  busy.value = true
  try {
    const r = await dataGenApi.create({
      taskName: `${dryRun ? '试运行' : '灌数据'}-${tables ? tables[0].tableName : gen.value.tableName}`,
      connId: gen.value.connId || undefined,
      dbUrl: gen.value.dbUrl,
      dbUser: gen.value.dbUser,
      dbPassword: gen.value.dbPassword,
      tableName: gen.value.tableName,
      rowCount: gen.value.rowCount,
      ruleJson: gen.value.ruleJson || undefined,
      tables,
      orphanRate: gen.value.orphanRate || undefined,
      repoId: gen.value.repoId || undefined,
      dryRun
    })
    if (dryRun) dryRunPassed.value = true
    await loadTasks()
    emit('created', { task: r.data, dryRun })
  } finally {
    busy.value = false
  }
}

// 上游预填值变化（如向导中用户回退改选项目、或部署后探测到数据库）→ 同步到表单
watch(() => [props.initialRepoId, props.initialConnId, props.initialDbUrl], () => {
  if (props.initialRepoId) gen.value.repoId = props.initialRepoId
  if (props.initialConnId) {
    gen.value.connId = props.initialConnId
    applyConn() // 连接列表可能刚加载完，此时补填 URL/用户/密码
  } else if (props.initialDbUrl) {
    gen.value.dbUrl = props.initialDbUrl
  }
}, { immediate: true })

// 连接列表加载完成后，若已预选连接则自动填充连接参数
watch(conns, () => {
  if (gen.value.connId && !gen.value.dbUrl) applyConn()
})

let pollTimer: ReturnType<typeof setInterval> | null = null
function startPolling() {
  stopPolling()
  if (!props.poll) return
  pollTimer = setInterval(loadTasks, 3000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(async () => {
  await Promise.all([loadConns(), repoStore.load().catch(() => {}), loadTasks()])
  if (gen.value.connId && !gen.value.dbUrl) applyConn()
  startPolling()
})

onUnmounted(stopPolling)

defineExpose({ loadTasks, gen })
</script>
