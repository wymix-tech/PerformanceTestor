<template>
  <div>
    <div class="page-title">
      <Bot class="w-5 h-5 text-neon-green" />
      AI 助手
      <span class="text-xs font-normal text-fg-sub ml-2">自然语言执行压测 / 查指标 / 分析根因 / 生成报告（Function Calling）</span>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-3 gap-4" style="height: calc(100vh - 180px)">
      <!-- 快捷指令 -->
      <div class="glass-card p-5 flex flex-col">
        <div class="text-sm font-medium mb-4">快捷指令</div>
        <div class="space-y-3">
          <button
            v-for="q in quickActions" :key="q"
            class="w-full text-left px-4 py-3 rounded-lg bg-white/5 border border-white/10 hover:border-neon-green/40 hover:bg-neon-green/5 transition-all"
            @click="send(q)"
          >{{ q }}</button>
        </div>
        <div class="text-[11px] text-fg-sub mt-auto pt-6 leading-relaxed">
          当前为无 API Key 降级模式：AI 基于真实库数据按规则模板回答；配置 AI Key 后将启用大模型 Function Calling 多轮推理。
        </div>
      </div>

      <!-- 对话流 -->
      <div class="glass-card p-5 xl:col-span-2 flex flex-col">
        <div class="flex items-center justify-between mb-3">
          <div class="text-sm font-medium">对话（{{ sessionId }}）</div>
          <button class="text-xs text-fg-sub hover:text-fg-main" @click="clearSession">清空会话</button>
        </div>

        <div class="flex-1 overflow-y-auto space-y-4 pr-1 min-h-[320px]">
          <div v-if="!messages.length" class="text-xs text-fg-sub py-16 text-center">
            输入问题或点击左侧快捷指令，AI 将自动调用工具执行压测 / 查询指标 / 分析代码问题
          </div>
          <div v-for="(m, i) in messages" :key="i">
            <!-- 用户消息 -->
            <div v-if="m.role === 'user'" class="flex justify-end mb-3">
              <div class="max-w-[80%] px-4 py-2.5 rounded-2xl rounded-br-sm bg-neon-green/15 border border-neon-green/30 text-sm text-fg-main whitespace-pre-wrap">{{ m.content }}</div>
            </div>
            <!-- AI 消息 -->
            <div v-else class="flex justify-start">
              <div class="max-w-[85%]">
                <div v-for="(tool, ti) in m.toolCalls" :key="ti"
                  class="mb-2 px-3 py-2 rounded-lg bg-neon-blue/5 border border-neon-blue/20 text-xs">
                  <span class="text-neon-blue">调用工具</span>
                  <span class="text-fg-main font-mono ml-1">{{ tool.name }}</span>
                  <pre class="text-[11px] text-fg-sub mt-1.5 whitespace-pre-wrap font-mono max-h-[140px] overflow-y-auto">{{ tool.arguments }}</pre>
                </div>
                <div class="px-4 py-2.5 rounded-2xl rounded-bl-sm bg-white/8 border border-white/10 text-sm text-fg-main whitespace-pre-wrap" style="background:rgba(255,255,255,0.08)">{{ m.content }}</div>
              </div>
            </div>
          </div>
          <div v-if="loading" class="flex items-center gap-2 text-xs text-fg-sub">
            <span class="w-2 h-2 rounded-full bg-neon-green animate-pulse"></span>
            AI 正在处理…
          </div>
        </div>

        <!-- 输入框 -->
        <div class="mt-4 flex gap-2">
          <input
            v-model="input" class="input-text flex-1" placeholder="例如：帮我压测一下 /api/apps，50 并发 30 秒"
            @keyup.enter="send(input)"
          />
          <button class="btn-primary" :disabled="loading || !input.trim()" @click="send(input)">
            <Send class="w-4 h-4" /> 发送
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Bot, Send } from 'lucide-vue-next'
import { aiApi } from '../api'

const route = useRoute()
const sessionId = ref(`s-${Date.now().toString(36)}`)
const messages = ref<Array<Record<string, any>>>([])
const input = ref('')
const loading = ref(false)

const quickActions = [
  '分析最近慢 SQL 的根因并给出修复方案',
  '当前性能能承受多少并发、何时扩容',
  '查询最近 JVM 指标',
  '生成一份性能检测报告'
]

async function send(text: string) {
  const content = text.trim()
  if (!content || loading.value) return
  input.value = ''
  messages.value.push({ role: 'user', content })
  loading.value = true
  try {
    const r = await aiApi.chat({ sessionId: sessionId.value, message: content })
    messages.value.push({
      role: 'assistant',
      content: r.data.reply || '（无回复）',
      toolCalls: r.data.toolCalls || []
    })
  } catch {
    messages.value.push({ role: 'assistant', content: '请求失败，请确认 perf-server 已启动。' })
  } finally {
    loading.value = false
  }
}

async function clearSession() {
  await aiApi.clearSession(sessionId.value).catch(() => {})
  messages.value = []
}

onMounted(() => {
  const msg = route.query.msg as string | undefined
  if (msg) send(msg)
})
</script>
