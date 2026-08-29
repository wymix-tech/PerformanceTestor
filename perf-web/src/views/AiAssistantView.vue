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
        <!-- AI 自动执行特色入口 -->
        <button
          class="w-full text-left px-4 py-3.5 rounded-xl mb-4 border border-neon-green/40 bg-neon-green/10 hover:bg-neon-green/20 transition-all relative overflow-hidden"
          @click="send(AUTO_PIPELINE_PROMPT)"
        >
          <div class="flex items-center gap-2 text-sm font-medium text-neon-green">
            <Zap class="w-4 h-4" /> AI 自动执行性能检测闭环
          </div>
          <div class="text-[11px] text-fg-sub mt-1.5 leading-relaxed">
            自动串联：灌测试数据 → 发起压测 → 采集 JVM/调用链/慢 SQL → 生成修复方案 → 输出检测报告
          </div>
          <div class="text-[11px] text-neon-green/80 mt-1.5">配置 API Key 后由 AI 多轮编排全自动执行</div>
        </button>
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
import { Bot, Send, Zap } from 'lucide-vue-next'
import { aiApi } from '../api'

const route = useRoute()
const sessionId = ref(`s-${Date.now().toString(36)}`)
const messages = ref<Array<Record<string, any>>>([])
const input = ref('')
const loading = ref(false)

const quickActions = [
  '自动执行性能检测闭环',
  '编译被测项目并部署到服务器启动（需告知 repoId 与 SSH 连接）',
  '分析最近慢 SQL 的根因并给出修复方案',
  '当前性能能承受多少并发、何时扩容',
  '查询最近 JVM 指标',
  '生成一份性能检测报告'
]

/** AI 自动执行闭环编排指令（LLM 档自动串联工具多轮执行；规则档返回状态快照+执行计划） */
const AUTO_PIPELINE_PROMPT =
  '自动执行性能检测闭环：检查已接入项目后，向 demo 库（jdbc:mysql://127.0.0.1:3306/demo）灌入测试数据' +
  '（perf_user 1000 行、perf_order 2000 行，user_id 关联 perf_user.id），随后对 ' +
  'http://127.0.0.1:8081/api/user/list?limit=200 发起 20 并发 15 秒压测，压测期间采集 JVM 指标、热点调用链与慢 SQL，' +
  '将证据关联源码生成 2-3 个修复方案，最后汇总输出一份完整性能检测报告（含容量评估与扩容建议）。'

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
