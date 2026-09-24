<template>
  <div>
    <div class="page-title">
      <Bot class="w-5 h-5 text-neon-green" />
      AI 助手
      <span class="text-xs font-normal text-fg-sub ml-2">自然语言执行压测 / 查指标 / 分析根因 / 生成报告（Function Calling）</span>
    </div>

    <!-- 对话流（全宽） -->
    <div class="glass-card p-5 flex flex-col relative" style="height: calc(100vh - 180px)">
        <div class="flex items-center justify-between mb-3">
          <div class="text-sm font-medium">对话</div>
          <button class="text-xs text-fg-sub hover:text-fg-main" @click="clearSession">清空会话</button>
        </div>

        <div ref="scrollEl" class="flex-1 overflow-y-auto space-y-4 pr-1 min-h-[320px]">
          <div v-if="!messages.length" class="text-xs text-fg-sub py-16 text-center">
            输入 <code class="text-neon-blue">/</code> 使用指令，输入 <code class="text-neon-blue">@</code> 引用项目/问题，或直接自然语言提问
          </div>
          <div v-for="(m, i) in messages" :key="i">
            <div v-if="m.role === 'user'" class="flex justify-end mb-3">
              <div class="max-w-[80%] px-4 py-2.5 rounded-2xl rounded-br-sm bg-neon-green/15 border border-neon-green/30 text-sm text-fg-main whitespace-pre-wrap">{{ m.content }}</div>
            </div>
            <div v-else class="flex justify-start">
              <div class="max-w-[85%] min-w-0">
                <!-- ReAct 推理过程（Thought / Action / Observation） -->
                <div v-if="m.thought"
                  class="mb-2 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-[11px] text-fg-sub flex items-start gap-1.5">
                  <Loader2 v-if="m.streaming" class="w-3 h-3 mt-px shrink-0 animate-spin text-neon-blue" aria-hidden="true" />
                  <span class="truncate">{{ m.thought }}</span>
                </div>
                <div v-for="(tool, ti) in m.toolCalls" :key="ti"
                  class="mb-2 px-3 py-2 rounded-lg bg-neon-blue/5 border border-neon-blue/20 text-xs">
                  <span class="text-neon-blue">调用工具</span>
                  <span class="text-fg-main font-mono ml-1">{{ tool.name }}</span>
                  <pre class="text-[11px] text-fg-sub mt-1.5 whitespace-pre-wrap font-mono max-h-[140px] overflow-y-auto">{{ tool.arguments }}</pre>
                  <pre v-if="tool.result" class="text-[11px] text-fg-sub mt-1 pt-1 border-t border-white/10 whitespace-pre-wrap font-mono max-h-[140px] overflow-y-auto">{{ tool.result }}</pre>
                </div>
                <div v-if="m.content || !m.streaming"
                  class="px-4 py-2.5 rounded-2xl rounded-bl-sm bg-white/8 border border-white/10 text-sm text-fg-main whitespace-pre-wrap break-words"
                  style="background:rgba(var(--overlay), 0.08)">{{ m.content }}<span v-if="m.streaming" class="inline-block w-1.5 h-3.5 ml-0.5 bg-neon-green animate-pulse align-middle"></span></div>
                <div v-if="m.roleLabel" class="mt-1 text-[11px] text-fg-sub">
                  角色 {{ m.roleLabel }} · {{ m.mode === 'rule' ? '规则演示模式' : '大模型 ReAct' }}<span v-if="m.rounds"> · {{ m.rounds }} 轮推理</span>
                </div>
              </div>
            </div>
          </div>
          <div v-if="loading && !messages.some((m) => m.streaming)" class="flex items-center gap-2 text-xs text-fg-sub">
            <span class="w-2 h-2 rounded-full bg-neon-green animate-pulse"></span>
            AI 正在处理…
          </div>
        </div>

        <!-- 输入框（含命令面板） -->
        <div class="mt-4 relative">
          <!-- 命令面板 -->
          <div v-if="panelOpen" class="absolute bottom-full left-0 right-0 mb-2 glass-card p-2 max-h-[300px] overflow-y-auto z-10">
            <div class="px-2 py-1 text-[11px] text-fg-sub">
              {{ panelMode === 'slash' ? '指令（/）' : panelMode === 'project' ? '引用项目（@）' : '引用性能问题（@）' }}
            </div>
            <button v-for="(it, i) in panelItems" :key="it.key"
              class="w-full text-left px-3 py-2 rounded-lg text-xs transition-all flex items-center gap-2"
              :class="i === panelIdx ? 'bg-neon-green/15 text-fg-main' : 'hover:bg-white/5 text-fg-sub'"
              @mousedown.prevent="pickItem(it)"
            >
              <component :is="it.icon" class="w-3.5 h-3.5 shrink-0" :class="i === panelIdx ? 'text-neon-green' : 'text-fg-sub'" />
              <span class="font-medium">{{ it.title }}</span>
              <span class="text-fg-sub truncate ml-auto">{{ it.desc }}</span>
            </button>
            <div v-if="!panelItems.length" class="px-3 py-4 text-xs text-fg-sub text-center">
              未命中{{ panelMode === 'slash' ? '指令' : '项目/问题' }} — 将作为普通文本发送
            </div>
          </div>

          <div class="flex gap-2">
            <input
              ref="inputEl" v-model="input" class="input-text flex-1"
              placeholder="输入 / 呼出指令，@ 引用项目/问题，或直接自然语言提问"
              @input="onInput" @keydown="onKeydown" @blur="closePanelSoon"
            />
            <button v-if="loading" class="btn-primary" style="background:linear-gradient(135deg,#ef4444,#f97316)" @click="stopStream">
              <Square class="w-4 h-4" /> 停止
            </button>
            <button v-else class="btn-primary" :disabled="!input.trim()" @click="send(input)">
              <Send class="w-4 h-4" /> 发送
            </button>
          </div>
        </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Bot, Send, Zap, Database, Gauge, FileText, Wrench, Bug, Boxes, Loader2, Square } from 'lucide-vue-next'
import { aiApi, sourceApi } from '../api'
import { useRepoStore } from '../stores/repoStore'

const route = useRoute()
const sessionId = ref(`s-${Date.now().toString(36)}`)
const messages = ref<Array<Record<string, any>>>([])
const input = ref('')
const loading = ref(false)
const inputEl = ref<HTMLInputElement | null>(null)
const scrollEl = ref<HTMLElement | null>(null)
/** 进行中的流式请求控制器（供"停止"中断） */
let abortCtl: AbortController | null = null

/** 后端 AiRole 枚举 → 中文角色名 */
const ROLE_LABELS: Record<string, string> = {
  GENERAL: '通用助手',
  STRESS_EXECUTOR: '压测执行',
  ROOT_CAUSE_ANALYST: '根因分析',
  FIX_ADVISOR: '修复建议'
}

/** AI 自动执行闭环编排指令（LLM 档自动串联工具多轮执行；规则档返回状态快照+执行计划） */
const AUTO_PIPELINE_PROMPT =
  '自动执行性能检测闭环：检查已接入项目后，向 demo 库（jdbc:mysql://127.0.0.1:3306/demo）灌入测试数据' +
  '（perf_user 1000 行、perf_order 2000 行，user_id 关联 perf_user.id），随后对 ' +
  'http://127.0.0.1:8081/api/user/list?limit=200 发起 20 并发 15 秒压测，压测期间采集 JVM 指标、热点调用链与慢 SQL，' +
  '将证据关联源码生成 2-3 个修复方案，最后汇总输出一份完整性能检测报告（含容量评估与扩容建议）。'

// ===== Slash / @ 命令面板 =====
interface PanelItem {
  key: string
  title: string
  desc: string
  icon: any
  /** slash 指令：选中后填入的完整文本；project/issue：插入引用标记 */
  insert: string
}

const SLASH_COMMANDS: PanelItem[] = [
  { key: 'auto', title: '自动执行性能检测闭环', desc: '灌数→压测→观测→修复→报告', icon: Zap, insert: AUTO_PIPELINE_PROMPT },
  { key: 'stress', title: '发起压测', desc: '目标 URL + 并发 + 时长', icon: Gauge, insert: '帮我压测 http://127.0.0.1:8081/api/user/list?limit=200 ，并发 20 持续 15 秒' },
  { key: 'datagen', title: '灌注测试数据', desc: 'demo 库联表灌数', icon: Database, insert: '向 demo 库灌入测试数据：perf_user 1000 行、perf_order 2000 行（user_id 关联 perf_user.id）' },
  { key: 'slow', title: '慢 SQL 根因分析', desc: '关联源码给修复方案', icon: Wrench, insert: '分析最近慢 SQL 的根因并给出修复方案' },
  { key: 'capacity', title: '容量评估', desc: '当前性能能承受多少并发', icon: Gauge, insert: '当前性能能承受多少并发、何时扩容' },
  { key: 'jvm', title: '查询 JVM 指标', desc: '最近内存/线程/GC', icon: Boxes, insert: '查询最近 JVM 指标' },
  { key: 'report', title: '生成性能检测报告', desc: '汇总压测/指标/问题', icon: FileText, insert: '生成一份性能检测报告' }
]

const panelMode = ref<'' | 'slash' | 'project'>('')  // project 模式含项目与问题两类
const panelQuery = ref('')
const panelIdx = ref(0)
const repos = ref<Array<Record<string, any>>>([])
const repoStore = useRepoStore()
const issues = ref<Array<Record<string, any>>>([])

const panelItems = computed<PanelItem[]>(() => {
  const q = panelQuery.value.trim().toLowerCase()
  if (panelMode.value === 'slash') {
    return q ? SLASH_COMMANDS.filter((c) => (c.title + c.key).toLowerCase().includes(q)) : SLASH_COMMANDS
  }
  // @ 模式：项目 + 性能问题混排
  const items: PanelItem[] = [
    ...repos.value.map((r) => ({
      key: 'repo-' + r.id,
      title: r.repoName || `项目#${r.id}`,
      desc: `项目 · ${r.appName || '未绑定应用'}`,
      icon: Boxes,
      insert: `@项目「${r.repoName || r.id}」`
    })),
    ...issues.value.map((i) => ({
      key: 'issue-' + i.id,
      title: i.title || `问题#${i.id}`,
      desc: `问题 · ${i.issueType || ''} ${i.severity || ''}`,
      icon: Bug,
      insert: `@问题「${i.title || i.id}」`
    }))
  ]
  return q ? items.filter((x) => (x.title + x.desc).toLowerCase().includes(q)) : items
})

const panelOpen = computed(() => panelMode.value !== '')

/** 输入变化：检测光标前的触发符 */
function onInput() {
  const el = inputEl.value
  if (!el) return
  const pos = el.selectionStart ?? input.value.length
  const before = input.value.slice(0, pos)
  // / 必须在输入起始位置（避免句子中间误触发）
  const slashMatch = before.match(/^\/(\S*)$/)
  const atMatch = before.match(/@([^\s@]*)$/)
  if (slashMatch) {
    panelMode.value = 'slash'
    panelQuery.value = slashMatch[1]
    panelIdx.value = 0
  } else if (atMatch) {
    panelMode.value = 'project'
    panelQuery.value = atMatch[1]
    panelIdx.value = 0
  } else {
    panelMode.value = ''
  }
}

function onKeydown(e: KeyboardEvent) {
  if (!panelOpen.value) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    panelIdx.value = Math.min(panelIdx.value + 1, panelItems.value.length - 1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    panelIdx.value = Math.max(panelIdx.value - 1, 0)
  } else if (e.key === 'Enter' && panelItems.value.length) {
    e.preventDefault()
    pickItem(panelItems.value[panelIdx.value])
  } else if (e.key === 'Escape') {
    panelMode.value = ''
  }
}

/** 选中面板项：slash 整句替换；@ 插入引用标记（保留后续文本） */
function pickItem(it: PanelItem) {
  const el = inputEl.value
  const pos = el?.selectionStart ?? input.value.length
  const before = input.value.slice(0, pos)
  const after = input.value.slice(pos)
  if (panelMode.value === 'slash') {
    input.value = it.insert + (after.startsWith(' ') ? after : ' ' + after)
  } else {
    const trigger = before.match(/@([^\s@]*)$/)
    const start = before.length - (trigger ? trigger[0].length : 0)
    input.value = input.value.slice(0, start) + it.insert + ' ' + after
  }
  panelMode.value = ''
  inputEl.value?.focus()
}

function closePanelSoon() {
  setTimeout(() => { panelMode.value = '' }, 150)
}

// ===== SSE 流式对话 =====
/** 发送并流式渲染：axios 不支持流式，改用 fetch + ReadableStream 逐帧解析 */
async function send(text: string) {
  const content = text.trim()
  if (!content || loading.value) return
  input.value = ''
  panelMode.value = ''
  messages.value.push({ role: 'user', content })

  const assistant: Record<string, any> = {
    role: 'assistant', content: '', toolCalls: [], thought: '', streaming: true
  }
  messages.value.push(assistant)
  loading.value = true

  const controller = new AbortController()
  abortCtl = controller
  try {
    const res = await fetch('/api/ai/chat/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('perf_token') || ''}`
      },
      body: JSON.stringify({ sessionId: sessionId.value, message: content }),
      signal: controller.signal
    })
    if (!res.ok || !res.body) {
      throw new Error(`HTTP ${res.status}`)
    }
    await readSse(res.body, assistant)
  } catch (err: any) {
    if (err?.name !== 'AbortError') {
      assistant.content += '\n（请求失败，请确认 perf-server 已启动。）'
    }
  } finally {
    assistant.streaming = false
    assistant.thought = ''
    abortCtl = null
    loading.value = false
    await scrollToBottom()
  }
}

/** 读取 SSE 字节流：按空行切帧，逐帧交给 applyEvent */
async function readSse(body: ReadableStream<Uint8Array>, msg: Record<string, any>) {
  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
    let sep = buffer.indexOf('\n\n')
    while (sep >= 0) {
      handleFrame(buffer.slice(0, sep), msg)
      buffer = buffer.slice(sep + 2)
      sep = buffer.indexOf('\n\n')
    }
  }
  // 连接关闭时残留的最后一帧（服务端可能未以空行收尾）
  if (buffer.trim()) handleFrame(buffer, msg)
}

/** 解析单帧 SSE：event 名 + data（统一 JSON 编码，避免正文换行破坏帧格式） */
function handleFrame(frame: string, msg: Record<string, any>) {
  let eventName = 'message'
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      data += line.slice(5).trim()
    }
  }
  if (!data) return
  let payload: any
  try {
    payload = JSON.parse(data)
  } catch {
    return
  }
  applyEvent(eventName, payload, msg)
}

/** 按事件类型增量更新助手消息 */
function applyEvent(eventName: string, payload: any, msg: Record<string, any>) {
  switch (eventName) {
    case 'token':
      msg.content += payload || ''
      scrollToBottom()
      break
    case 'thought':
      msg.thought = payload || ''
      break
    case 'tool':
      msg.toolCalls.push({ name: payload.name, arguments: payload.arguments || '', result: '' })
      break
    case 'observation': {
      const last = msg.toolCalls[msg.toolCalls.length - 1]
      if (last && last.name === payload.name) {
        last.result = payload.result || ''
      } else {
        msg.toolCalls.push({ name: payload.name, arguments: '', result: payload.result || '' })
      }
      break
    }
    case 'done':
      msg.mode = payload.mode
      msg.rounds = payload.rounds
      msg.roleLabel = ROLE_LABELS[payload.role] || ''
      break
    case 'error':
      msg.content += `\n（${payload}）`
      break
  }
}

/** 中断进行中的流式请求 */
function stopStream() {
  abortCtl?.abort()
  const last = messages.value[messages.value.length - 1]
  if (last) last.streaming = false
  loading.value = false
}

async function scrollToBottom() {
  await nextTick()
  const el = scrollEl.value
  if (el) el.scrollTop = el.scrollHeight
}

async function clearSession() {
  abortCtl?.abort()
  await aiApi.clearSession(sessionId.value).catch(() => {})
  messages.value = []
  loading.value = false
}

onMounted(async () => {
  // 预载项目与问题，供 @ 引用（项目走全局缓存；失败静默，@ 面板显示为空）
  const [, i] = await Promise.allSettled([
    repoStore.load().then(() => { repos.value = repoStore.repos }),
    sourceApi.issues({ limit: 20 })
  ])
  if (i.status === 'fulfilled') issues.value = i.value.data || []

  const msg = route.query.msg as string | undefined
  if (msg) {
    send(msg)
    return
  }
  // 总览快速开始引导等入口：/ai?auto=1 直接启动 AI 自动执行闭环
  if (route.query.auto) {
    send(AUTO_PIPELINE_PROMPT)
  }
})
</script>
