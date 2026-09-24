<template>
  <div class="grid grid-cols-1 xl:grid-cols-3 gap-4">
    <div class="glass-card p-5">
      <div class="text-sm font-medium mb-4">新建定时压测</div>
      <div class="space-y-3">
        <div>
          <label class="text-xs text-fg-sub">调度名称</label>
          <input v-model="sch.name" class="input-text w-full" placeholder="如：核心接口回归门禁" />
        </div>
        <div>
          <label class="text-xs text-fg-sub">压测模板（基于当前表单参数）</label>
          <div class="text-[11px] text-fg-sub bg-white/5 rounded-lg px-3 py-2 font-mono break-all">
            {{ sch.templatePreview || '在「压测」Tab 配置参数后回到此处（或直接填下方模板 JSON）' }}
          </div>
          <textarea v-model="sch.taskJson" rows="4" class="input-text w-full font-mono text-xs mt-2"
            placeholder='{"targetUrl":"http://127.0.0.1:8081/api/user/list?limit=200","concurrency":20,"durationSec":30}'></textarea>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="text-xs text-fg-sub">间隔（分钟）</label>
            <input v-model.number="sch.intervalMinutes" type="number" class="input-text w-full" min="1" max="10080" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">基线任务 ID（0=自动）</label>
            <input v-model.number="sch.baselineTaskId" type="number" class="input-text w-full" min="0" />
          </div>
        </div>
        <div>
          <label class="text-xs text-fg-sub">告警 Webhook（钉钉/飞书/自定义，可选）</label>
          <input v-model="sch.webhookUrl" class="input-text w-full" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" />
          <div class="text-[11px] text-fg-sub mt-1">回归阈值：QPS↓&gt;10% / P95↑&gt;20% / 错误率↑&gt;5pct 任一命中即告警</div>
        </div>
        <button class="btn-primary w-full justify-center" :disabled="creatingSchedule" @click="createSchedule">
          {{ creatingSchedule ? '创建中…' : '创建调度' }}
        </button>
      </div>
    </div>

    <div class="glass-card p-5 xl:col-span-2">
      <div class="text-sm font-medium mb-3 flex items-center justify-between">
        <span>调度列表</span>
        <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="loadSchedules">刷新</button>
      </div>
      <div v-if="!schedules.length" class="text-xs text-fg-sub py-8 text-center">暂无定时压测调度</div>
      <div v-else class="space-y-2 max-h-[430px] overflow-y-auto pr-1">
        <div v-for="s in schedules" :key="s.id" class="px-4 py-3 rounded-lg bg-white/5">
          <div class="flex items-center justify-between">
            <div class="text-sm text-fg-main">{{ s.scheduleName }}</div>
            <div class="flex items-center gap-2">
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="s.enabled ? 'bg-neon-green/10 text-neon-green' : 'bg-white/5 text-fg-sub'">
                {{ s.enabled ? '运行中' : '已暂停' }}
              </span>
              <button class="text-[11px] text-neon-blue hover:underline cursor-pointer" @click="toggleSchedule(s)">
                {{ s.enabled ? '暂停' : '启用' }}
              </button>
              <button class="text-[11px] text-neon-green hover:underline cursor-pointer" @click="loadDiff(s)">基线对比</button>
              <button class="text-[11px] text-red-400 hover:underline cursor-pointer" @click="removeSchedule(s)">删除</button>
            </div>
          </div>
          <div class="text-[11px] text-fg-sub mt-1.5 flex flex-wrap gap-3">
            <span>每 {{ s.intervalMinutes }} 分钟</span>
            <span v-if="s.lastRunTime">上次触发 {{ fmtTime(s.lastRunTime) }}</span>
            <span v-if="s.nextRunTime">下次 {{ fmtTime(s.nextRunTime) }}</span>
            <span v-if="s.lastTaskId">任务 #{{ s.lastTaskId }}</span>
          </div>
          <!-- 基线对比结果 -->
          <div v-if="diffOf[s.id]" class="mt-3">
            <div v-if="diffOf[s.id].noBaseline" class="text-[11px] text-amber-400">暂无可用基线（首次执行或历史任务缺失）</div>
            <template v-else>
              <div class="flex items-center gap-2 mb-1.5">
                <span class="text-[11px] text-fg-sub">基线 #{{ diffOf[s.id].baseline?.id }}</span>
                <span class="text-fg-sub">→</span>
                <span class="text-[11px] text-fg-sub">当前 #{{ diffOf[s.id].current?.id }}</span>
                <span v-if="diffOf[s.id].regressed" class="text-[11px] px-2 py-0.5 rounded-full bg-red-400/10 text-red-400">⚠ 检测到性能回归</span>
                <span v-else class="text-[11px] px-2 py-0.5 rounded-full bg-neon-green/10 text-neon-green">✓ 指标正常</span>
              </div>
              <table class="w-full text-[11px]" aria-label="定时压测调度列表">
                <tbody>
                  <tr v-for="r in diffOf[s.id].rows" :key="r.label" class="border-b border-white/5">
                    <td class="py-1 text-fg-dim">{{ r.label }}</td>
                    <td class="text-right text-fg-sub">{{ r.base }}</td>
                    <td class="text-right text-fg-main">{{ r.cur }}</td>
                    <td class="text-right font-medium" :class="r.regressed ? 'text-red-400' : r.improved ? 'text-neon-green' : 'text-fg-sub'">{{ r.deltaPct || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </template>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { scheduleApi } from '../../api'
import { fmtTime } from '../../utils/format'

/**
 * 定时压测面板：调度管理 + 基线对比。
 *
 * <p>从 StressCenterView 拆出，行为与视觉保持不变；
 * 压测模板默认取「压测」Tab 表单快照（由父组件通过 template 传入）。
 */
const props = defineProps<{
  /** 压测表单快照（切到本 Tab 时用于预填模板） */
  template?: Record<string, any> | null
}>()

const schedules = ref<Array<Record<string, any>>>([])
const diffOf = ref<Record<number, any>>({})
const creatingSchedule = ref(false)
const sch = ref({
  name: '',
  intervalMinutes: 60,
  baselineTaskId: 0,
  webhookUrl: '',
  taskJson: '',
  /** 表单模板预览（当前压测表单快照派生） */
  templatePreview: ''
})

// 首次进入（或模板变化且尚未手填）时用表单快照预填模板
watch(() => props.template, (snap) => {
  if (sch.value.taskJson || !snap?.targetUrl) return
  sch.value.templatePreview = `${snap.method} ${snap.targetUrl} · ${snap.concurrency}并发 × ${snap.durationSec}s`
  sch.value.taskJson = JSON.stringify(snap)
}, { immediate: true })

async function loadSchedules() {
  try {
    const r = await scheduleApi.list()
    schedules.value = r.data || []
  } catch { /* 列表加载失败静默 */ }
}

async function createSchedule() {
  if (!sch.value.name.trim() || !sch.value.taskJson.trim()) return
  let task: Record<string, unknown>
  try {
    task = JSON.parse(sch.value.taskJson)
  } catch {
    alert('压测模板 JSON 解析失败，请检查格式')
    return
  }
  creatingSchedule.value = true
  try {
    const r = await scheduleApi.create({
      scheduleName: sch.value.name.trim(),
      intervalMinutes: sch.value.intervalMinutes,
      baselineTaskId: sch.value.baselineTaskId,
      webhookUrl: sch.value.webhookUrl.trim(),
      task
    })
    // http 拦截器已解包为 { code, message, data }
    const res = r as unknown as { code: number; message: string; data: unknown }
    if (res.code === 0) {
      sch.value.name = ''
      await loadSchedules()
    } else {
      alert(res.message || '创建失败')
    }
  } finally {
    creatingSchedule.value = false
  }
}

async function toggleSchedule(s: Record<string, any>) {
  await scheduleApi.toggle(s.id, !s.enabled)
  await loadSchedules()
}

async function removeSchedule(s: Record<string, any>) {
  if (!confirm(`确认删除调度「${s.scheduleName}」？`)) return
  await scheduleApi.remove(s.id)
  await loadSchedules()
}

async function loadDiff(s: Record<string, any>) {
  try {
    const r = await scheduleApi.diff(s.id)
    diffOf.value = { ...diffOf.value, [s.id]: r.data }
  } catch { /* 对比失败静默 */ }
}

onMounted(loadSchedules)
defineExpose({ loadSchedules })
</script>
