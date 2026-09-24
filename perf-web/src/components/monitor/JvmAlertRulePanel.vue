<template>
  <section class="glass-card p-5">
    <header class="mb-3 flex items-center justify-between">
      <div class="flex items-center gap-2">
        <BellRing class="w-4 h-4 text-neon-green" />
        <span class="text-sm font-medium">JVM 阈值告警规则</span>
        <span class="text-[11px] text-fg-sub">超阈值自动通知（堆 / Full GC / 线程）</span>
      </div>
      <button class="btn-primary text-xs" @click="openAdd">+ 新增规则</button>
    </header>

    <!-- 规则列表 -->
    <div v-if="!rules.length" class="text-xs text-fg-sub py-3">
      暂无规则。新增后每 60 秒检测一次，冷却期内不重复发送。
    </div>
    <div v-else class="overflow-x-auto">
      <table class="w-full text-xs" aria-label="告警规则列表">
        <thead class="text-fg-sub">
          <tr class="border-b border-white/10">
            <th class="py-2 text-left font-normal">应用</th>
            <th class="py-2 text-left font-normal">指标</th>
            <th class="py-2 text-right font-normal">阈值</th>
            <th class="py-2 text-right font-normal">窗口</th>
            <th class="py-2 text-right font-normal">冷却</th>
            <th class="py-2 text-center font-normal">启用</th>
            <th class="py-2 text-right font-normal">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rules" :key="r.id" class="border-b border-white/5">
            <td class="py-2">{{ r.appName || '全部应用' }}</td>
            <td class="py-2">{{ metricLabel(r.metricKey) }}</td>
            <td class="py-2 text-right">{{ r.threshold }}{{ metricUnit(r.metricKey) }}</td>
            <td class="py-2 text-right">{{ r.windowSec }}s</td>
            <td class="py-2 text-right">{{ r.quietSec }}s</td>
            <td class="py-2 text-center">
              <button
                class="text-[11px] px-2 py-0.5 rounded"
                :class="r.enabled ? 'bg-neon-green/20 text-neon-green' : 'bg-white/10 text-fg-sub'"
                @click="toggle(r)"
              >
                {{ r.enabled ? '已启用' : '已停用' }}
              </button>
            </td>
            <td class="py-2 text-right whitespace-nowrap">
              <button class="text-neon-blue hover:underline mr-2" @click="check(r)">检测</button>
              <button class="text-neon-blue hover:underline mr-2" @click="openEdit(r)">编辑</button>
              <button class="text-red-400 hover:underline" @click="remove(r)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 新增/编辑表单 -->
    <div v-if="editing" class="mt-4 pt-4 border-t border-white/10">
      <div class="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div>
          <label class="text-xs text-fg-sub">应用（留空=全部）</label>
          <input v-model="form.appName" class="input-text w-full" :disabled="isEdit" placeholder="全部应用" />
        </div>
        <div>
          <label class="text-xs text-fg-sub">指标</label>
          <select v-model="form.metricKey" class="input-select w-full" :disabled="isEdit" aria-label="指标">
            <option v-for="m in metricOptions" :key="m.key" :value="m.key">{{ m.label }}</option>
          </select>
        </div>
        <div>
          <label class="text-xs text-fg-sub">阈值</label>
          <input v-model.number="form.threshold" type="number" min="0" step="0.1" class="input-text w-full" />
        </div>
        <div>
          <label class="text-xs text-fg-sub">统计窗口（秒）</label>
          <input v-model.number="form.windowSec" type="number" min="30" max="86400" class="input-text w-full" />
        </div>
        <div>
          <label class="text-xs text-fg-sub">冷却期（秒）</label>
          <input v-model.number="form.quietSec" type="number" min="0" max="86400" class="input-text w-full" />
        </div>
      </div>
      <div class="mt-3">
        <label class="text-xs text-fg-sub">通知地址（钉钉/飞书机器人，留空不发送）</label>
        <input v-model="form.webhook" class="input-text w-full" placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." />
      </div>
      <p v-if="isEdit" class="mt-2 text-[11px] text-fg-sub">
        应用与指标为规则唯一标识，编辑时不可修改（如需变更请删除后重建）。
      </p>
      <div class="mt-3 flex gap-2">
        <button class="btn-primary text-xs" @click="submit">保存</button>
        <button class="text-xs text-fg-sub hover:underline" @click="cancel">取消</button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { BellRing } from 'lucide-vue-next'
import { jvmAlertApi } from '../../api'

interface AlertRule {
  id: number
  appName: string
  metricKey: string
  threshold: number
  windowSec: number
  quietSec: number
  enabled: number
  webhook: string
}
interface MetricOption {
  key: string
  label: string
  unit: string
}

const props = defineProps<{ app?: string }>()
const emit = defineEmits<{ (e: 'notify', msg: string): void }>()

const rules = ref<AlertRule[]>([])
const metricOptions = ref<MetricOption[]>([])
const editing = ref(false)
const isEdit = ref(false)
const form = ref({ id: 0, appName: '', metricKey: 'heapUsedRatio', threshold: 85, windowSec: 300, quietSec: 900, webhook: '' })

function metricLabel(key: string) {
  return metricOptions.value.find((m) => m.key === key)?.label ?? key
}
function metricUnit(key: string) {
  return metricOptions.value.find((m) => m.key === key)?.unit ?? ''
}

async function load() {
  const [r, m] = await Promise.allSettled([
    jvmAlertApi.list(props.app || undefined),
    jvmAlertApi.metrics()
  ])
  if (r.status === 'fulfilled') rules.value = r.value.data || []
  if (m.status === 'fulfilled') metricOptions.value = m.value.data || []
}

function openAdd() {
  isEdit.value = false
  form.value = { id: 0, appName: props.app || '', metricKey: 'heapUsedRatio', threshold: 85, windowSec: 300, quietSec: 900, webhook: '' }
  editing.value = true
}

function openEdit(r: AlertRule) {
  isEdit.value = true
  form.value = {
    id: r.id,
    appName: r.appName,
    metricKey: r.metricKey,
    threshold: r.threshold,
    windowSec: r.windowSec,
    quietSec: r.quietSec,
    webhook: r.webhook || ''
  }
  editing.value = true
}

function cancel() {
  editing.value = false
}

async function submit() {
  const payload = {
    appName: form.value.appName,
    metricKey: form.value.metricKey,
    threshold: form.value.threshold,
    windowSec: form.value.windowSec,
    quietSec: form.value.quietSec,
    webhook: form.value.webhook,
    enabled: 1
  }
  if (isEdit.value) {
    await jvmAlertApi.update(form.value.id, payload)
    emit('notify', '告警规则已更新')
  } else {
    await jvmAlertApi.add(payload)
    emit('notify', '告警规则已新增')
  }
  editing.value = false
  await load()
}

async function toggle(r: AlertRule) {
  const enabled = !r.enabled
  await jvmAlertApi.setEnabled(r.id, enabled)
  await load()
}

async function check(r: AlertRule) {
  const res = await jvmAlertApi.check(r.id)
  emit('notify', res.data?.triggered ? '检测完成：已超阈值并发送告警' : '检测完成：未超阈值')
  await load()
}

async function remove(r: AlertRule) {
  await jvmAlertApi.remove(r.id)
  emit('notify', '告警规则已删除')
  await load()
}

onMounted(load)
// 应用切换时重新加载对应规则
defineExpose({ load })
</script>
