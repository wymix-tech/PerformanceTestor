<template>
  <div class="space-y-3">
    <!-- 接口选择（父组件提供 endpoints 时展示） -->
    <div v-if="endpoints.length">
      <label class="text-xs text-fg-sub">接口</label>
      <select v-model.number="endpointIdx" class="input-select w-full" @change="selectEndpoint">
        <option :value="-1">— 选择接口（{{ endpoints.length }} 个）—</option>
        <option v-for="(e, i) in endpoints" :key="i" :value="i">
          [{{ e.httpMethod }}] {{ e.path }}（{{ e.className }}#{{ e.methodName || '?' }}）
        </option>
      </select>
    </div>

    <div>
      <label class="text-xs text-fg-sub">服务地址（选项目后按 agent 上报自动带出，可修改）</label>
      <input v-model="baseUrl" class="input-text w-full" placeholder="http://127.0.0.1:8081" @input="composeUrl()" />
    </div>
    <div>
      <label class="text-xs text-fg-sub">目标 URL（{{ endpoints.length ? '选接口自动拼接，可微调' : 'http/https' }}）</label>
      <input v-model="targetUrl" class="input-text w-full" placeholder="http://host:port/api/xxx" />
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs text-fg-sub">方法</label>
        <select v-model="method" class="input-select w-full">
          <option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option><option>PATCH</option>
        </select>
      </div>
      <div>
        <label class="text-xs text-fg-sub">引擎</label>
        <select v-model="engineType" class="input-select w-full">
          <option value="http">http（内置）</option>
          <option value="jmeter" title="需平台服务器安装 JMeter 并配置 JMETER_HOME">jmeter</option>
        </select>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs text-fg-sub">并发数</label>
        <input v-model.number="concurrency" type="number" class="input-text w-full" min="1" max="1000" />
      </div>
      <div>
        <label class="text-xs text-fg-sub">时长（秒）</label>
        <input v-model.number="durationSec" type="number" class="input-text w-full" min="1" max="3600" />
      </div>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs text-fg-sub">预热（秒，不计入统计）</label>
        <input v-model.number="warmupSec" type="number" class="input-text w-full" min="0" max="300" title="预热期发压但不计入正式统计，消除冷连接/JIT 污染" />
      </div>
      <div>
        <label class="text-xs text-fg-sub">QPS 上限（0 不限）</label>
        <input v-model.number="qpsLimit" type="number" class="input-text w-full" min="0" max="100000" />
      </div>
    </div>
    <div v-if="method !== 'GET'">
      <label class="text-xs text-fg-sub">请求体 Body（JSON，可选）</label>
      <textarea v-model="body" class="input-text w-full font-mono" rows="3" placeholder='{"key": "value"}'></textarea>
    </div>
    <div>
      <label class="text-xs text-fg-sub">分档并发（逗号分隔，容量曲线）</label>
      <input v-model="stageConcurrencies" class="input-text w-full" placeholder="如 10,50,100" />
    </div>
    <button class="btn-primary w-full justify-center" :disabled="disabled || !targetUrl.trim()" @click="emitSubmit">
      {{ disabled ? '压测进行中…' : submitLabel }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

/**
 * 压测表单公共组件：从 StressCenterView（895 行）/ QuickStartView（913 行）抽取的
 * 重复表单逻辑——接口选择、URL 拼接、方法/引擎/并发/时长/预热/QPS 限流/分档并发。
 *
 * <p>父组件职责：项目与分支选择、任务发起后的轮询与报告展示（经 props 注入 endpoints
 * 与 autoBaseUrl，经 submit 事件接收标准请求体）。
 */
const props = withDefaults(
  defineProps<{
    /** 接口扫描结果（空数组则纯手动 URL 模式） */
    endpoints?: Array<Record<string, any>>
    /** agent 上报自动探测的服务地址（变化时仅在用户未手改前预填） */
    autoBaseUrl?: string
    /** 目标 URL 初始值（QuickStart 的演示默认地址） */
    defaultTargetUrl?: string
    /** 提交禁用（压测进行中） */
    disabled?: boolean
    /** 提交按钮文案 */
    submitLabel?: string
  }>(),
  {
    endpoints: () => [],
    autoBaseUrl: '',
    defaultTargetUrl: '',
    disabled: false,
    submitLabel: '开始压测'
  }
)

const emit = defineEmits<{
  (e: 'submit', payload: Record<string, unknown>): void
  (e: 'endpoint-selected', endpoint: Record<string, any>): void
}>()

const endpointIdx = ref(-1)
const baseUrl = ref('')
const targetUrl = ref(props.defaultTargetUrl)
const method = ref('GET')
const engineType = ref('http')
const concurrency = ref(50)
const durationSec = ref(30)
const warmupSec = ref(10)
const qpsLimit = ref(0)
const body = ref('')
const stageConcurrencies = ref('10,50,100')

// 服务地址自动预填：仅空值或上次自动值时刷新（用户手改后不覆盖）
let lastAutoBaseUrl = ''
watch(
  () => props.autoBaseUrl,
  (v) => {
    if (v && (!baseUrl.value || baseUrl.value === lastAutoBaseUrl)) {
      baseUrl.value = v
      lastAutoBaseUrl = v
      composeUrl()
    }
  }
)

// 接口列表变化（切换项目/分支）时重置选择
watch(
  () => props.endpoints,
  () => {
    endpointIdx.value = -1
  }
)

/** 选中接口：自动填 method 与 URL（baseUrl + path） */
function selectEndpoint() {
  const e = props.endpoints[endpointIdx.value]
  if (!e) return
  method.value = e.httpMethod || 'GET'
  composeUrl(e.path)
  emit('endpoint-selected', e)
}

/** 拼接完整目标 URL；显式传入 path（选接口时）优先于当前输入 */
function composeUrl(pathOverride?: string) {
  const base = (baseUrl.value || '').replace(/\/+$/, '')
  const e = props.endpoints[endpointIdx.value]
  const p = pathOverride !== undefined ? pathOverride : e ? e.path : ''
  if (base && p) {
    targetUrl.value = base + p
  }
}

/** 组装标准压测请求体 */
function buildPayload(): Record<string, unknown> {
  const stageList = stageConcurrencies.value
    .split(',')
    .map((s) => Number(s.trim()))
    .filter((n) => n > 0)
  const payload: Record<string, unknown> = {
    targetUrl: targetUrl.value.trim(),
    method: method.value,
    engineType: engineType.value,
    concurrency: concurrency.value,
    durationSec: durationSec.value,
    warmupSec: warmupSec.value,
    qpsLimit: qpsLimit.value
  }
  if (stageList.length > 1) payload.stageConcurrencies = stageList
  if (body.value.trim()) payload.body = body.value.trim()
  return payload
}

function emitSubmit() {
  if (!targetUrl.value.trim()) return
  emit('submit', buildPayload())
}

defineExpose({
  /** "再来一次"回填：把已完成任务的配置回填表单 */
  fillFrom(task: Record<string, any>, stagePoints: Array<Record<string, any>> = []) {
    targetUrl.value = task.targetUrl || targetUrl.value
    method.value = task.method || 'GET'
    engineType.value = task.engineType || 'http'
    concurrency.value = task.concurrency ?? 50
    durationSec.value = task.durationSec ?? 30
    warmupSec.value = task.warmupSec ?? 10
    qpsLimit.value = task.qpsLimit ?? 0
    // 服务地址取 URL origin（http://host:port），便于后续选接口重新拼接
    try {
      baseUrl.value = new URL(task.targetUrl).origin
    } catch {
      /* 非标准 URL 保持原样 */
    }
    if (stagePoints.length) {
      stageConcurrencies.value = stagePoints.map((p) => p.concurrency).join(',')
    }
    endpointIdx.value = -1
  },
  /** 自动选中首个接口（QuickStart 项目选定后调用） */
  selectFirstEndpoint() {
    if (props.endpoints.length) {
      endpointIdx.value = 0
      selectEndpoint()
    }
  },
  /** 当前表单状态（诊断用） */
  snapshot: () => ({
    targetUrl: targetUrl.value,
    method: method.value,
    concurrency: concurrency.value,
    durationSec: durationSec.value
  })
})
</script>
