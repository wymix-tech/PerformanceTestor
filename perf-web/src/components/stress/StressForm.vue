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

    <!-- 多接口场景编排（可选） -->
    <div v-if="scenarioEnabled" class="rounded-xl border border-white/10 bg-white/5 p-3">
      <label class="flex items-center gap-1.5 text-xs text-fg-sub">
        <input v-model="scenarioMode" type="checkbox" @change="onScenarioToggle" />
        多接口混合场景（按权重配比发压，支持登录态提取与传递）
      </label>

      <template v-if="scenarioMode">
        <div v-if="!scenario.length" class="text-[11px] text-fg-sub py-3 text-center">暂无步骤，点击「+ 添加步骤」开始编排</div>
        <div v-else class="space-y-3 mt-3">
          <div
            v-for="(s, i) in scenario" :key="i"
            class="rounded-lg border border-white/10 bg-black/20 p-3 space-y-2"
          >
            <div class="flex items-center justify-between">
              <span class="text-[11px] text-fg-sub">步骤 {{ i + 1 }}{{ s.name ? ` · ${s.name}` : '' }}</span>
              <button class="text-[11px] text-red-400 hover:underline" @click="removeStep(i)">移除</button>
            </div>
            <div class="grid grid-cols-12 gap-2">
              <div class="col-span-3">
                <label class="text-[10px] text-fg-sub">名称</label>
                <input v-model="s.name" class="input-text w-full text-xs" placeholder="如 login" />
              </div>
              <div class="col-span-2">
                <label class="text-[10px] text-fg-sub">方法</label>
                <select v-model="s.method" class="input-select w-full text-xs">
                  <option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option><option>PATCH</option>
                </select>
              </div>
              <div class="col-span-2">
                <label class="text-[10px] text-fg-sub">权重</label>
                <input v-model.number="s.weight" type="number" min="0" class="input-text w-full text-xs" />
              </div>
              <div class="col-span-5">
                <label class="text-[10px] text-fg-sub">URL（支持 {{ VAR_PH }} 占位）</label>
                <input v-model="s.url" class="input-text w-full text-xs" placeholder="http://host/api/xxx" />
              </div>
            </div>
            <div>
              <label class="text-[10px] text-fg-sub">请求头（每行一个 k: v，值支持 {{ VAR_PH }}）</label>
              <textarea v-model="s.headersText" rows="2" class="input-text w-full text-xs font-mono"
                :placeholder="AUTH_PH"></textarea>
            </div>
            <div v-if="s.method !== 'GET'">
              <label class="text-[10px] text-fg-sub">请求体（支持 {{ VAR_PH }} 占位）</label>
              <textarea v-model="s.body" rows="2" class="input-text w-full text-xs font-mono" placeholder='{"id": 1}'></textarea>
            </div>
            <div class="grid grid-cols-2 gap-2">
              <div>
                <label class="text-[10px] text-fg-sub">提取变量名（如 token）</label>
                <input v-model="s.extractVar" class="input-text w-full text-xs" placeholder="token" />
              </div>
              <div>
                <label class="text-[10px] text-fg-sub">响应 JSON 路径（如 data.token）</label>
                <input v-model="s.extractPath" class="input-text w-full text-xs" placeholder="data.token" />
              </div>
            </div>
          </div>
        </div>

        <div class="flex items-center gap-2 mt-3">
          <button class="text-[11px] text-neon-blue hover:underline" @click="addStep">+ 添加步骤</button>
          <span class="text-[10px] text-fg-sub flex-1 truncate">流量配比：{{ weightSummary }}</span>
        </div>
        <div class="text-[10px] text-fg-sub mt-1.5 leading-relaxed">
          场景模式下 JMeter 引擎不支持多步骤，将自动回退 http 引擎执行。
        </div>
      </template>
    </div>

    <button
      class="btn-primary w-full justify-center"
      :disabled="disabled || !canSubmit"
      @click="emitSubmit"
    >
      {{ disabled ? '压测进行中…' : submitLabel }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

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
    /** 是否展示多接口场景编排区（定时压测等轻量场景可关闭） */
    scenarioEnabled?: boolean
  }>(),
  {
    endpoints: () => [],
    autoBaseUrl: '',
    defaultTargetUrl: '',
    disabled: false,
    submitLabel: '开始压测',
    /** 是否展示多接口场景编排（定时压测等轻量场景可关闭） */
    scenarioEnabled: true
  }
)

const emit = defineEmits<{
  (e: 'submit', payload: Record<string, unknown>): void
  (e: 'endpoint-selected', endpoint: Record<string, any>): void
}>()

// ---------------- 多接口场景编排 ----------------

interface ScenarioStep {
  name: string
  url: string
  method: string
  headersText: string
  body: string
  weight: number
  /** 变量提取：响应 JSON 点分路径 → 变量名，如 data.token → token */
  extractVar: string
  extractPath: string
}

/**
 * 占位符示例文案。
 *
 * <p>模板中直接写 {@code {{ }}} 会被 Vue 编译器当作插值表达式解析而报错，
 * 故以字符串常量的形式注入。
 */
const VAR_PH = '{{变量名}}'
const AUTH_PH = 'Authorization: Bearer {{token}}'

/** 是否启用场景模式（关闭即单接口，与历史行为一致） */
const scenarioMode = ref(false)
const scenario = ref<ScenarioStep[]>([])

function newStep(): ScenarioStep {
  return {
    name: '', url: '', method: 'GET', headersText: '', body: '', weight: 1,
    extractVar: '', extractPath: ''
  }
}

function addStep() {
  scenario.value.push(newStep())
  if (scenario.value.length === 1) {
    // 首个步骤默认沿用已填的单接口 URL，避免用户重复输入
    scenario.value[0].url = targetUrl.value
    scenario.value[0].method = method.value
    scenario.value[0].body = body.value
  }
}

function removeStep(i: number) {
  scenario.value.splice(i, 1)
}

/** 场景步骤预览：各步权重占比（用于展示流量配比） */
const weightSummary = computed(() => {
  const total = scenario.value.reduce((s, x) => s + (Number(x.weight) || 0), 0)
  if (total <= 0) return '未配置权重'
  return scenario.value
    .map((x) => `${x.name || x.url || '未命名'} ${Math.round(((Number(x.weight) || 0) * 100) / total)}%`)
    .join(' · ')
})

/** 解析请求头文本（每行 k: v）为对象 */
function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const idx = line.indexOf(':')
    if (idx <= 0) continue
    const k = line.slice(0, idx).trim()
    const v = line.slice(idx + 1).trim()
    if (k) out[k] = v
  }
  return out
}

/** 组装场景步骤（提交用） */
function buildScenario() {
  return scenario.value
    .map((s, i) => {
      const step: Record<string, unknown> = {
        name: s.name?.trim() || `step-${i + 1}`,
        url: s.url.trim(),
        method: s.method || 'GET',
        weight: Number(s.weight) || 1
      }
      const headers = parseHeaders(s.headersText)
      if (Object.keys(headers).length) step.headers = headers
      if (s.body.trim()) step.body = s.body.trim()
      // 变量提取：var 与 path 必须同时提供才生效
      if (s.extractVar.trim() && s.extractPath.trim()) {
        step.extract = [{ var: s.extractVar.trim(), path: s.extractPath.trim() }]
      }
      return step
    })
    .filter((s) => String(s.url).startsWith('http'))
}

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
  // 场景模式：带 scenario 时后端按权重编排执行，忽略单接口的 targetUrl/method/body
  if (scenarioMode.value) {
    payload.scenario = buildScenario()
  }
  return payload
}

/**
 * 提交可用性：
 * 单接口模式要求目标 URL 非空；场景模式要求至少一个合法步骤
 * （步骤 URL 必须以 http 开头，由场景编辑区的过滤规则保证）。
 */
const canSubmit = computed(() => {
  if (scenarioMode.value) {
    return scenario.value.some((s) => String(s.url).trim().startsWith('http'))
      && scenario.value.some((s) => (Number(s.weight) || 0) > 0)
  }
  return !!targetUrl.value.trim()
})

/** 勾选场景模式且尚无步骤时，自动补一个（用当前单接口参数初始化） */
function onScenarioToggle() {
  if (scenarioMode.value && scenario.value.length === 0) {
    addStep()
  }
}

function emitSubmit() {
  if (!canSubmit.value) return
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
