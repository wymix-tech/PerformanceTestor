<template>
  <div class="grid grid-cols-1 xl:grid-cols-3 gap-4">
    <!-- 实例列表 -->
    <div class="glass-card p-5 xl:col-span-2">
      <div class="text-sm font-medium mb-3 flex items-center justify-between">
        <span>数据集实例</span>
        <div class="flex items-center gap-3">
          <select v-model="statusFilter" data-test="status-filter" class="input-select text-xs py-1" @change="reload">
            <option value="">全部状态</option>
            <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
          </select>
          <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="reload">刷新</button>
        </div>
      </div>
      <div v-if="loadFailed" class="text-xs text-red-400 py-4 text-center">实例列表加载失败，请点击刷新重试</div>
      <div v-if="!instances.length" class="text-xs text-fg-sub py-8 text-center">
        暂无数据集实例，前往
        <router-link to="/stress?tab=datagen" class="text-neon-blue hover:underline">压测中心 → 数据准备</router-link>
        发起灌数
      </div>
      <div v-else class="space-y-2 max-h-[560px] overflow-y-auto pr-1">
        <div
          v-for="it in instances" :key="it.id"
          data-test="dataset-row"
          class="px-4 py-3 rounded-lg bg-white/5 hover:bg-white/10 transition-all cursor-pointer"
          @click="selectInstance(it)"
        >
          <div class="flex items-center justify-between">
            <div class="text-sm text-fg-main truncate font-mono">#{{ it.id }} · {{ it.definitionId }}</div>
            <span class="flex items-center gap-2 shrink-0">
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="datasetStatusClass(it.status)">{{ it.status }}</span>
              <button
                v-if="isCancelable(it)"
                data-test="cancel-btn"
                class="text-[11px] text-red-400 hover:underline cursor-pointer"
                @click.stop="cancelInstance(it)"
              >取消</button>
            </span>
          </div>
          <div class="flex items-center justify-between mt-2 text-[11px] text-fg-sub">
            <span>
              {{ it.environmentId || '—' }}
              <template v-if="it.actualRows != null"> · {{ it.actualRows }} 行</template>
            </span>
            <span>{{ fmtTime(it.createTime) }}</span>
          </div>
          <div v-if="it.errorMsg" class="text-[11px] text-red-400 mt-1 truncate" :title="it.errorMsg">{{ it.errorMsg }}</div>
        </div>
      </div>
      <!-- 游标分页：hasMore=false 时不渲染按钮，避免"点了没反应"（TraceAnalyzerView 口径） -->
      <div v-if="instances.length" class="pt-2">
        <button
          v-if="hasMore"
          data-test="load-more"
          class="w-full py-2 rounded-lg text-xs text-fg-sub border border-white/10 hover:text-neon-green hover:border-neon-green/30 transition-all disabled:opacity-50"
          :disabled="listLoading"
          @click="loadMore"
        >{{ listLoading ? '加载中…' : '加载更多' }}</button>
        <div v-else class="text-[11px] text-fg-sub text-center py-1">已到最后一条</div>
      </div>
    </div>

    <!-- 实例详情 -->
    <div class="glass-card p-5">
      <div class="text-sm font-medium mb-3 flex items-center justify-between">
        <span>实例详情</span>
        <button v-if="selected" class="text-xs text-neon-blue hover:underline cursor-pointer" @click="loadDetail">刷新</button>
      </div>
      <div v-if="!selected" class="text-xs text-fg-sub py-8 text-center">点击左侧实例查看详情</div>
      <template v-else>
        <div data-test="detail-panel" class="space-y-3 text-xs">
          <div class="grid grid-cols-1 gap-y-1.5">
            <div class="flex justify-between"><span class="text-fg-dim">实例 ID</span><span class="font-mono text-fg-main">{{ selected.id }}</span></div>
            <div class="flex justify-between"><span class="text-fg-dim">定义</span><span class="font-mono text-fg-main">{{ selected.definitionId }}</span></div>
            <div class="flex justify-between"><span class="text-fg-dim">目标环境</span><span class="text-fg-main">{{ selected.environmentId || '—' }}</span></div>
            <div class="flex justify-between">
              <span class="text-fg-dim">状态</span>
              <span class="px-2 py-0.5 rounded-full" :class="datasetStatusClass(selected.status)">{{ selected.status }}</span>
            </div>
            <div class="flex justify-between"><span class="text-fg-dim">实际行数</span><span class="text-fg-main">{{ selected.actualRows ?? '—' }}</span></div>
            <div class="flex justify-between"><span class="text-fg-dim">存储占用</span><span class="text-fg-main">{{ selected.actualStorageBytes != null ? `${selected.actualStorageBytes} B` : '—' }}</span></div>
            <div class="flex justify-between"><span class="text-fg-dim">创建时间</span><span class="text-fg-main">{{ fmtTime(selected.createTime) || '—' }}</span></div>
          </div>
          <div v-if="selected.schemaFingerprint">
            <div class="text-fg-dim mb-1">Schema 指纹</div>
            <div class="text-[10px] font-mono break-all bg-white/5 rounded-lg px-3 py-2 text-fg-sub">{{ selected.schemaFingerprint }}</div>
          </div>

          <!-- 运行中进度轮询 -->
          <div v-if="progress" data-test="progress-panel">
            <div class="text-fg-dim mb-1">构建进度</div>
            <div class="bg-white/5 rounded-lg px-3 py-2 space-y-1">
              <div class="flex justify-between">
                <span class="text-fg-sub">状态</span>
                <span class="text-fg-main">{{ progress.instanceStatus }}</span>
              </div>
              <div class="flex justify-between">
                <span class="text-fg-sub">执行线程</span>
                <span>{{ progress.running ? '运行中' : '空闲' }}</span>
              </div>
              <div v-if="progress.errorMsg" class="text-red-400">{{ progress.errorMsg }}</div>
            </div>
          </div>

          <div v-if="selected.errorMsg">
            <div class="text-fg-dim mb-1">失败信息</div>
            <div class="text-[11px] text-red-400 bg-red-400/10 rounded-lg px-3 py-2 break-all">{{ selected.errorMsg }}</div>
          </div>

          <div v-if="validationSummary">
            <div class="text-fg-dim mb-1">验证摘要</div>
            <pre class="text-[10px] font-mono bg-white/5 rounded-lg px-3 py-2 text-fg-sub whitespace-pre-wrap break-all max-h-40 overflow-y-auto">{{ validationSummary }}</pre>
          </div>

          <div v-if="transitionLog.length">
            <div class="text-fg-dim mb-1">状态转换记录</div>
            <div class="space-y-1.5 max-h-40 overflow-y-auto pr-1">
              <div v-for="(r, idx) in transitionLog" :key="idx" class="bg-white/5 rounded-lg px-3 py-1.5 text-[11px]">
                <div class="flex justify-between">
                  <span class="text-fg-main">{{ r.from_state || r.fromState }} → {{ r.to_state || r.toState }}</span>
                  <span class="text-fg-sub">{{ fmtTime(r.occurred_at || r.occurredAt) }}</span>
                </div>
                <div class="text-fg-dim mt-0.5">{{ r.operator }}{{ r.reason ? ` · ${r.reason}` : '' }}</div>
              </div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { datasetApi } from '../api'
import { datasetStatusClass, fmtTime } from '../utils/format'
import { useAuthStore } from '@/stores/auth'

/**
 * 数据集管理页（B7 续批次 5/6）：实例列表（状态过滤 + 游标分页）+ 详情面板
 * （进度/验证摘要/转换记录）+ 运行中实例取消按钮（POST /api/datasets/{id}/cancel，
 * B7 续批次 3）。
 *
 * <p>游标分页（B7 续批次 6）：cursor 传上页 nextCursor（不透明串），
 * hasMore=false 末页——「加载更多」追加式翻页，对齐 TraceAnalyzerView 口径。
 * 取消为协作式处置：operator/reason 必填（后端门禁），已 CANCELLED 时幂等
 * 返回 accepted=false——按成功处理不报错（口径对齐 dataset_api.py）。
 * VERIFYING/PARTIAL/终态无取消出边，按钮仅对 PLANNED/GENERATING/LOADING 显示。
 */
const CANCELABLE = ['PLANNED', 'GENERATING', 'LOADING']
/** 状态过滤选项（对齐 ptde 状态机十态，总方案 §12.2） */
const STATUSES = ['PLANNED', 'GENERATING', 'LOADING', 'VERIFYING', 'READY', 'PARTIAL', 'FAILED', 'CLEANING', 'CLEANED', 'CANCELLED']
/** 游标分页单页条数（后端上限 200） */
const PAGE_SIZE = 50

const authStore = useAuthStore()
const instances = ref<Array<Record<string, any>>>([])
const statusFilter = ref('')
const loadFailed = ref(false)
const selected = ref<Record<string, any> | null>(null)
const progress = ref<Record<string, any> | null>(null)
/** 游标分页状态：cursor 为 null 表示已到末页 */
const nextCursor = ref<string | null>(null)
const hasMore = ref(false)
const listLoading = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

const isCancelable = (it: Record<string, any>) => CANCELABLE.includes(String(it.status))

const validationSummary = computed(() => {
  const v = selected.value?.validationSummary
  if (!v) return ''
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2)
})

const transitionLog = computed(() => {
  const log = selected.value?.transitionLog
  return Array.isArray(log) ? log : []
})

/**
 * 拉取一页实例列表。
 *
 * <p>reset=true 首屏/切过滤（覆盖列表）；false 翻页（追加，不重置已加载内容）。
 * 首屏失败标记失败态保留旧数据；翻页失败静默保旧数据（不闪空）。
 */
async function loadInstances(reset = true) {
  if (!reset && (listLoading.value || !hasMore.value)) return
  listLoading.value = true
  try {
    const page = await datasetApi.list({
      status: statusFilter.value || undefined,
      size: PAGE_SIZE,
      cursor: reset ? undefined : (nextCursor.value ?? undefined)
    })
    instances.value = reset ? page.items : [...instances.value, ...page.items]
    nextCursor.value = page.nextCursor
    hasMore.value = page.hasMore
    loadFailed.value = false
    // 首屏后当前选中项已不在列表（被清理）时复位详情，避免展示陈旧快照
    if (reset && selected.value && !instances.value.some((it) => String(it.id) === String(selected.value!.id))) {
      selected.value = null
      progress.value = null
    }
  } catch {
    if (reset) loadFailed.value = true
  } finally {
    listLoading.value = false
  }
}

/** 首屏/切过滤/手动刷新：重置游标后拉第一页 */
function reload() {
  return loadInstances(true)
}

/** 翻页：带上上一页的 nextCursor 追加，不重置已加载内容 */
function loadMore() {
  return loadInstances(false)
}

function selectInstance(it: Record<string, any>) {
  selected.value = it
  progress.value = null
  loadDetail()
}

async function loadDetail() {
  const id = selected.value?.id
  if (id == null) return
  try {
    const r: any = await datasetApi.detail(id)
    if (r?.code === 0 && r.data) {
      selected.value = r.data
      progress.value = null
      await loadProgress()
    }
  } catch {
    /* 详情加载失败静默，保留当前快照 */
  }
}

/** 运行中实例（PLANNED/GENERATING/LOADING/VERIFYING）轮询构建进度 */
async function loadProgress() {
  const st = String(selected.value?.status || '')
  if (!['PLANNED', 'GENERATING', 'LOADING', 'VERIFYING'].includes(st)) {
    progress.value = null
    return
  }
  try {
    const r: any = await datasetApi.progress(selected.value!.id)
    if (r?.code === 0) progress.value = r.data
  } catch {
    /* 进度加载失败静默（下轮详情刷新重试） */
  }
}

/**
 * 运行中实例取消：operator/reason 必填（后端 code=1 显式拒绝缺失）。
 * reason 取消理由为空即不发起（不弹空审计）；accepted=false 为幂等返回，
 * 按成功处理。
 */
async function cancelInstance(it: Record<string, any>) {
  const reason = window.prompt(`取消数据集实例 #${it.id}（${it.definitionId}）？请输入取消理由（审计必填）：`)
  if (reason == null) return
  const trimmed = reason.trim()
  if (!trimmed) {
    alert('取消理由必填（operator/reason 审计），已取消操作')
    return
  }
  if (!window.confirm(`确认取消实例 #${it.id}？取消为终态（不可恢复），已产出分片保留供排查。`)) return
  try {
    const r: any = await datasetApi.cancel(it.id, {
      operator: authStore.username || 'web',
      reason: trimmed
    })
    if (r?.code !== 0) {
      alert(r?.message || '取消失败')
      return
    }
    const accepted = r.data?.accepted !== false
    alert(accepted
      ? `已发起取消：实例 #${it.id} → CANCELLED（工作线程将在安全点协作退出）`
      : `实例 #${it.id} 已处于 CANCELLED（幂等返回，未重复流转）`)
    await reload()
  } catch {
    alert('取消请求失败（网络异常），请稍后重试')
  }
}

/** 定时刷新：选中实例的详情/进度（列表经 刷新/加载更多 手动维护，避免翻页态被轮询覆盖） */
function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    if (selected.value) loadDetail()
  }, 5000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  reload()
  startPolling()
})
onUnmounted(stopPolling)

defineExpose({ reload, loadMore, loadDetail, instances })
</script>
