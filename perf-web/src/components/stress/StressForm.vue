<template>
  <div class="space-y-3">
    <div>
      <label class="text-xs text-fg-sub">服务地址（选项目后按 agent 上报自动带出，可修改）</label>
      <input v-model="baseUrl" class="input-text w-full" placeholder="http://127.0.0.1:8081" @input="composeUrl()" />
    </div>

    <!-- 压测模式：单接口 / 多接口混合（两种模式均支持从源码接口清单直接选用地址） -->
    <div v-if="scenarioEnabled" class="flex gap-1">
      <button
        v-for="m in modes" :key="m.key"
        type="button"
        class="px-3 py-1.5 rounded-lg text-xs transition-all cursor-pointer"
        :class="mode === m.key
          ? 'bg-neon-green/15 text-neon-green border border-neon-green/30'
          : 'text-fg-sub border border-white/10 hover:text-fg-main'"
        @click="switchMode(m.key)"
      >{{ m.label }}</button>
    </div>

    <!-- ===== 单接口模式 ===== -->
    <template v-if="!isScenario">
      <div v-if="endpoints.length">
        <label class="text-xs text-fg-sub">从源码接口选择</label>
        <select v-model.number="endpointIdx" class="input-select w-full" @change="selectEndpoint">
          <option :value="-1">— 选择接口（共 {{ endpoints.length }} 个）—</option>
          <option v-for="(e, i) in endpoints" :key="i" :value="i">
            [{{ e.httpMethod }}] {{ e.path }}（{{ e.className }}#{{ e.methodName || '?' }}）
          </option>
        </select>
      </div>
      <div v-else class="text-[11px] text-fg-sub">
        未选择项目：请手动填写目标 URL，或在上方选择项目后从源码接口直接选用。
      </div>
      <div>
        <label class="text-xs text-fg-sub">目标 URL（{{ endpoints.length ? '选接口自动拼接，可微调' : 'http/https' }}）</label>
        <input v-model="targetUrl" class="input-text w-full" placeholder="http://host:port/api/xxx" />
      </div>
      <div>
        <label class="text-xs text-fg-sub">方法</label>
        <select v-model="method" class="input-select w-full">
          <option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option><option>PATCH</option>
        </select>
      </div>
      <div v-if="method !== 'GET'">
        <label class="text-xs text-fg-sub">请求体 Body（JSON，可选）</label>
        <textarea v-model="body" class="input-text w-full font-mono" rows="3" placeholder='{"key": "value"}'></textarea>
      </div>
      <div>
        <label class="text-xs text-fg-sub">请求头（每行一个 k: v，可选）</label>
        <textarea v-model="headersText" rows="2" class="input-text w-full font-mono text-xs"
          placeholder="Authorization: Bearer xxx&#10;X-Tenant-Id: 1"></textarea>
      </div>
      <div>
        <label class="text-xs text-fg-sub">结果判断（可选：不满足即计为错误）</label>
        <textarea v-model="assertText" rows="3" class="input-text w-full font-mono text-xs"
          placeholder="status eq 200&#10;json data.code eq 0&#10;body contains success"></textarea>
        <div class="text-[10px] text-fg-sub mt-1 leading-relaxed">
          每行一条，全部满足才算成功。格式：<span class="font-mono">来源 键 比较 期望值</span><br />
          来源 <span class="font-mono">status / body / header / json</span>（status、body 省略键）；
          比较 <span class="font-mono">eq / ne / contains / notContains / gt / lt / regex</span><br />
          例：<span class="font-mono">json data.code eq 0</span> 可识别「HTTP 200 但业务失败」；
          <span class="font-mono">header Content-Type contains json</span> 校验响应头
        </div>
        <div v-if="assertError" class="text-[11px] text-red-400 mt-1">{{ assertError }}</div>
      </div>
    </template>
    <!-- ===== 多接口混合模式：每个步骤都可独立从源码接口清单选用地址 ===== -->
    <template v-else>
      <div class="rounded-xl border border-white/10 bg-white/5 p-3 space-y-3">
        <div v-if="!scenario.length" class="text-[11px] text-fg-sub py-3 text-center">暂无步骤，点击「+ 添加步骤」开始编排</div>
        <div v-else class="space-y-3">
          <div
            v-for="(s, i) in scenario" :key="i"
            class="rounded-lg border border-white/10 bg-black/20 p-3 space-y-2"
          >
            <div class="flex items-center justify-between gap-2">
              <span class="text-[11px] text-fg-sub shrink-0">步骤 {{ i + 1 }}{{ s.name ? ` · ${s.name}` : '' }}</span>
              <!-- 每步独立下拉：选中即替换该步的 URL 与方法，无需手填 -->
              <select
                class="input-select text-[10px] flex-1 min-w-0"
                :value="stepPick[i] ?? -1"
                @change="applyEndpointToStep(i, $event)"
              >
                <option :value="-1">从源码接口选择…</option>
                <option v-for="(e, ei) in endpoints" :key="ei" :value="ei">
                  [{{ e.httpMethod }}] {{ e.path }}（{{ e.className }}#{{ e.methodName || '?' }}）
                </option>
              </select>
              <button class="text-[11px] text-red-400 hover:underline shrink-0" @click="removeStep(i)">移除</button>
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
            <!-- 响应后处理：从响应体或响应头提取变量，供后续步骤引用 -->
            <div class="grid grid-cols-12 gap-2">
              <div class="col-span-3">
                <label class="text-[10px] text-fg-sub">提取变量名</label>
                <input v-model="s.extractVar" class="input-text w-full text-xs" placeholder="token" />
              </div>
              <div class="col-span-3">
                <label class="text-[10px] text-fg-sub">提取来源</label>
                <select v-model="s.extractSource" class="input-select w-full text-xs">
                  <option value="body">响应体 JSON</option>
                  <option value="header">响应头</option>
                </select>
              </div>
              <div class="col-span-6">
                <label class="text-[10px] text-fg-sub">{{ s.extractSource === 'header' ? '响应头名（如 Set-Cookie）' : 'JSON 路径（如 data.token）' }}</label>
                <input v-model="s.extractPath" class="input-text w-full text-xs"
                  :placeholder="s.extractSource === 'header' ? 'Set-Cookie' : 'data.token'" />
              </div>
            </div>
            <div>
              <label class="text-[10px] text-fg-sub">结果判断（可选：不满足即计为错误，每行一条）</label>
              <textarea v-model="s.assertText" rows="2" class="input-text w-full text-xs font-mono"
                placeholder="status eq 200&#10;json data.code eq 0"></textarea>
              <div class="text-[10px] text-fg-sub mt-1">
                来源 <span class="font-mono">status / body / header / json</span>
                比较 <span class="font-mono">eq / ne / contains / notContains / gt / lt / regex</span>
              </div>
              <div v-if="stepAssertErrors[i]" class="text-[10px] text-red-400 mt-1">{{ stepAssertErrors[i] }}</div>
            </div>
          </div>
        </div>

        <div class="flex items-center gap-2">
          <button class="text-[11px] text-neon-blue hover:underline" @click="addStep">+ 添加步骤</button>
          <span class="text-[10px] text-fg-sub flex-1 truncate">流量配比：{{ weightSummary }}</span>
        </div>
        <div class="text-[10px] text-fg-sub leading-relaxed">
          {{ endpoints.length
            ? '每个步骤都可从步骤行的下拉直接选用源码接口地址，无需手填 URL。'
            : '选择项目后，每个步骤都可从源码接口直接选用地址。' }}场景模式下 JMeter 引擎不支持多步骤，将自动回退 http 引擎执行。
        </div>
      </div>
    </template>

    <!-- ===== 共用压测参数（两种模式一致） ===== -->
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs text-fg-sub">引擎</label>
        <select v-model="engineType" class="input-select w-full">
          <option value="http">http（内置）</option>
          <option value="jmeter" title="需平台服务器安装 JMeter 并配置 JMETER_HOME">jmeter</option>
        </select>
      </div>
      <div>
        <label class="text-xs text-fg-sub">并发数</label>
        <input v-model.number="concurrency" type="number" class="input-text w-full" min="1" max="1000" />
      </div>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs text-fg-sub">时长（秒）</label>
        <input v-model.number="durationSec" type="number" class="input-text w-full" min="1" max="3600" />
      </div>
      <div>
        <label class="text-xs text-fg-sub">预热（秒，不计入统计）</label>
        <input v-model.number="warmupSec" type="number" class="input-text w-full" min="0" max="300" title="预热期发压但不计入正式统计，消除冷连接/JIT 污染" />
      </div>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs text-fg-sub">QPS 上限（0 不限）</label>
        <input v-model.number="qpsLimit" type="number" class="input-text w-full" min="0" max="100000" />
      </div>
      <div>
        <label class="text-xs text-fg-sub">分档并发（逗号分隔，容量曲线）</label>
        <input v-model="stageConcurrencies" class="input-text w-full" placeholder="如 10,50,100" />
      </div>
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
  /** 变量提取：响应 JSON 点分路径 / 响应头名 → 变量名 */
  extractVar: string
  /** 提取来源：body（JSON 路径） / header（响应头名） */
  extractSource: 'body' | 'header'
  extractPath: string
  /** 结果判断：每行一条「来源 [键] 比较 期望值」 */
  assertText: string
}

/**
 * 断言来源与比较方式的白名单。
 *
 * <p>与后端 ResponseAssertor 支持的取值保持一致；前端先做一层校验，
 * 避免把明显非法的规则发给后端后在压测中静默全判失败。
 */
const ASSERT_SOURCES = ['status', 'body', 'header', 'json'] as const
const ASSERT_OPS = ['eq', 'ne', 'contains', 'notContains', 'gt', 'lt', 'regex'] as const

/** 需要键的来源（status/body 省略键） */
const KEYED_SOURCES = ['header', 'json']

/**
 * 解析结果判断文本 → 断言规则数组。
 *
 * <p>语法：
 * <ul>
 *   <li>需键来源：{@code json data.code eq 0}（4 段）</li>
 *   <li>无键来源：{@code status eq 200}（3 段）</li>
 * </ul>
 * 空行与 {@code #} 开头的注释行忽略。
 *
 * @returns 规则数组；解析失败时返回 { error } 提示，由调用方展示
 */
/**
 * 取跳过前 {@code n} 个空白分隔 token 后的剩余原文（保留原始空白）。
 *
 * <p>为什么不用 {@code split(/\s+/).slice(n).join(' ')}：该写法会把期望值中的
 * 连续空格折叠成一个，导致 {@code body contains "a  b"}（两空格）这类断言
 * 被静默改成单空格而永不命中。改为按位置定位，原样保留剩余子串。
 *
 * @param line 已去首尾空白的单行文本
 * @param n    需跳过的 token 个数
 * @return 剩余原文（已去前导空白）；token 不足时返回空串
 */
function restAfterTokens(line: string, n: number): string {
  let idx = 0
  for (let t = 0; t < n; t++) {
    while (idx < line.length && /\s/.test(line[idx])) idx++
    while (idx < line.length && !/\s/.test(line[idx])) idx++
  }
  while (idx < line.length && /\s/.test(line[idx])) idx++
  return line.slice(idx)
}

function parseAssertions(text: string): { rules?: Array<Record<string, string>>; error?: string } {
  const rules: Array<Record<string, string>> = []
  const lines = (text || '').split('\n')
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim()
    if (!line || line.startsWith('#')) continue
    const parts = line.split(/\s+/)
    const source = parts[0].toLowerCase()
    if (!ASSERT_SOURCES.includes(source as typeof ASSERT_SOURCES[number])) {
      return { error: `第 ${i + 1} 行：来源「${parts[0]}」不合法，可选 ${ASSERT_SOURCES.join(' / ')}` }
    }
    let key: string | undefined
    let op: string
    let expected: string
    if (KEYED_SOURCES.includes(source)) {
      if (parts.length < 4) {
        return { error: `第 ${i + 1} 行：${source} 断言格式为「${source} 键 比较 期望值」，如 ${source} ${source === 'json' ? 'data.code eq 0' : 'Content-Type contains json'}` }
      }
      key = parts[1]
      op = parts[2]
      expected = restAfterTokens(line, 3)
    } else {
      if (parts.length < 3) {
        return { error: `第 ${i + 1} 行：${source} 断言格式为「${source} 比较 期望值」，如 ${source} eq 200` }
      }
      op = parts[1]
      expected = restAfterTokens(line, 2)
    }
    if (!ASSERT_OPS.includes(op as typeof ASSERT_OPS[number])) {
      return { error: `第 ${i + 1} 行：比较方式「${op}」不合法，可选 ${ASSERT_OPS.join(' / ')}` }
    }
    const rule: Record<string, string> = { source, op, expected }
    if (key !== undefined) rule.key = key
    rules.push(rule)
  }
  return { rules }
}

/**
 * 占位符示例文案。
 *
 * <p>模板中直接写 {@code {{ }}} 会被 Vue 编译器当作插值表达式解析而报错，
 * 故以字符串常量的形式注入。
 */
const VAR_PH = '{{变量名}}'
const AUTH_PH = 'Authorization: Bearer {{token}}'

/**
 * 压测模式：单接口 / 多接口混合。
 *
 * <p>以顶部 Tab 切换，替代原先底部「多接口混合场景」复选框——复选框把两种互斥的
 * 配置方式混在同一表单里，且多接口面板藏在末尾不易发现。
 */
type StressMode = 'single' | 'scenario'
const modes: Array<{ key: StressMode; label: string }> = [
  { key: 'single', label: '单接口' },
  { key: 'scenario', label: '多接口混合' }
]
const mode = ref<StressMode>('single')

/** 是否多接口模式（scenarioEnabled=false 时始终强制单接口） */
const isScenario = computed(() => props.scenarioEnabled && mode.value === 'scenario')
/** 供 payload 组装复用（等价于旧 scenarioMode） */
const scenarioMode = isScenario

const scenario = ref<ScenarioStep[]>([])
/** 每个步骤的源码接口选择下标（-1 = 未选；选完即回填 URL 并复位，便于再次选择） */
const stepPick = ref<number[]>([])

function newStep(): ScenarioStep {
  return {
    name: '', url: '', method: 'GET', headersText: '', body: '', weight: 1,
    extractVar: '', extractSource: 'body', extractPath: '', assertText: ''
  }
}

function addStep() {
  scenario.value.push(newStep())
  stepPick.value.push(-1)
  if (scenario.value.length === 1) {
    // 首个步骤默认沿用已填的单接口 URL，避免用户重复输入
    scenario.value[0].url = targetUrl.value
    scenario.value[0].method = method.value
    scenario.value[0].body = body.value
  }
}

function removeStep(i: number) {
  scenario.value.splice(i, 1)
  stepPick.value.splice(i, 1)
}

/**
 * 把源码接口应用到指定步骤：替换该步的 URL 与方法，并自动派生步骤名。
 *
 * <p>URL 由「服务地址 + 接口 path」拼接；未填服务地址时退化仅填 path，
 * 由用户补全 host（与单接口 selectEndpoint 的拼接规则一致）。
 */
function applyEndpointToStep(i: number, evt: Event) {
  const idx = Number((evt.target as HTMLSelectElement).value)
  const e = props.endpoints[idx]
  // 复位选择器，保证同一接口可重复选择触发
  stepPick.value[i] = -1
  if (!e) return
  const s = scenario.value[i]
  if (!s) return
  s.method = e.httpMethod || 'GET'
  const base = (baseUrl.value || '').replace(/\/+$/, '')
  s.url = base ? base + (e.path || '') : (e.path || '')
  // 步骤名留空时才自动派生，避免覆盖用户自定义名称
  if (!s.name.trim()) s.name = e.methodName || e.path || ''
}

/** 切换模式：切到多接口且尚无步骤时，沿用当前单接口参数补一个步骤 */
function switchMode(next: StressMode) {
  if (mode.value === next) return
  mode.value = next
  if (next === 'scenario' && scenario.value.length === 0) {
    addStep()
  }
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
      // 变量提取：var 与 path 必须同时提供才生效（来源可为 body / header）
      if (s.extractVar.trim() && s.extractPath.trim()) {
        step.extract = [{
          var: s.extractVar.trim(),
          source: s.extractSource || 'body',
          path: s.extractPath.trim()
        }]
      }
      // 结果判断：解析失败由 canSubmit 拦截，此处规则已保证合法
      const { rules } = parseAssertions(s.assertText)
      if (rules && rules.length) step.assertions = rules
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
/** 单接口模式的请求头（每行 k: v）与结果判断（每行一条断言） */
const headersText = ref('')
const assertText = ref('')

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
  // 请求头：单接口与多接口一致地按行解析（此前仅多接口支持，单接口缺失）
  const headers = parseHeaders(headersText.value)
  if (Object.keys(headers).length) payload.headers = headers
  if (!isScenario.value) {
    // 结果判断：单接口模式在此挂载；多接口模式按步骤各自挂载
    const { rules } = parseAssertions(assertText.value)
    if (rules && rules.length) payload.assertions = rules
  }
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
/** 单接口结果判断的语法错误（null = 无错误） */
const assertError = computed(() => {
  if (isScenario.value) return null
  const { error } = parseAssertions(assertText.value)
  return error || null
})

/** 多接口各步骤结果判断的语法错误：步骤下标 → 提示文案 */
const stepAssertErrors = computed(() => {
  const out: Record<number, string> = {}
  scenario.value.forEach((s, i) => {
    const { error } = parseAssertions(s.assertText)
    if (error) out[i] = error
  })
  return out
})

const canSubmit = computed(() => {
  // 结果判断语法错误：直接禁用提交，避免把非法规则发给后端后在压测中静默全判失败
  if (assertError.value) return false
  if (isScenario.value) {
    if (Object.keys(stepAssertErrors.value).length) return false
    return scenario.value.some((s) => String(s.url).trim().startsWith('http'))
      && scenario.value.some((s) => (Number(s.weight) || 0) > 0)
  }
  return !!targetUrl.value.trim()
})

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
    // 回填的历史任务按单接口恢复（多接口步骤不予还原），模式与步骤同步复位
    mode.value = 'single'
    scenario.value = []
    stepPick.value = []
    headersText.value = ''
    assertText.value = ''
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
