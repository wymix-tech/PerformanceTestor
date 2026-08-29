<template>
  <div>
    <div class="page-title">
      <LayoutDashboard class="w-5 h-5 text-neon-green" />
      总览
      <span class="text-xs font-normal text-fg-sub ml-2">实时性能检测 · 压测 · 容量 · AI 优化闭环</span>
      <button class="text-xs text-neon-blue hover:underline ml-auto" @click="refresh">刷新</button>
    </div>

    <!-- 统计卡片 -->
    <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mb-6">
      <div v-for="m in metrics" :key="m.label" class="metric-card">
        <div class="flex items-center justify-between mb-3">
          <span class="text-xs text-fg-sub">{{ m.label }}</span>
          <component :is="m.icon" class="w-4 h-4 text-neon-green/70" />
        </div>
        <div class="text-2xl font-bold neon-text">{{ m.value }}</div>
        <div class="text-[11px] text-fg-sub mt-1">{{ m.hint }}</div>
      </div>
    </div>

    <!-- 快速开始引导 -->
    <div class="glass-card p-5 mb-6">
      <div class="flex items-center justify-between mb-4">
        <div class="text-sm font-medium flex items-center gap-2">
          <Rocket class="w-4 h-4 text-neon-green" /> 快速开始
          <span class="text-[11px] font-normal text-fg-sub">六步完成性能检测闭环：项目 → 数据 → 压测 → 结果 → 调用链 → 优化</span>
        </div>
        <div class="text-xs text-fg-sub">
          已完成 <span class="text-neon-green font-bold">{{ doneCount }}</span>/{{ steps.length }}
          <div class="w-28 h-1.5 rounded bg-white/10 mt-1 overflow-hidden">
            <div class="h-full bg-neon-green rounded transition-all" :style="{ width: (doneCount / steps.length) * 100 + '%' }"></div>
          </div>
        </div>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
        <button
          v-for="(s, idx) in steps" :key="s.title"
          class="text-left p-4 rounded-xl border transition-all relative"
          :class="stepClass(s.done, idx === currentIdx)"
          @click="router.push(s.to)"
        >
          <div class="flex items-center justify-between mb-2">
            <span
              class="w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-bold shrink-0"
              :class="s.done ? 'bg-neon-green text-black' : idx === currentIdx ? 'bg-neon-blue text-black' : 'bg-white/10 text-fg-sub'"
            >
              <Check v-if="s.done" class="w-3.5 h-3.5" />
              <template v-else>{{ idx + 1 }}</template>
            </span>
            <ArrowRight class="w-3.5 h-3.5 opacity-0 group-hover:opacity-100" :class="s.done ? 'text-fg-sub' : 'text-neon-blue'" />
          </div>
          <div class="text-sm font-medium" :class="s.done ? 'text-fg-sub' : 'text-fg-main'">{{ s.title }}</div>
          <div class="text-[11px] mt-1" :class="s.done ? 'text-neon-green' : 'text-fg-sub'">
            {{ s.done ? s.doneHint : s.todoHint }}
          </div>
          <div
            v-if="idx === currentIdx && !s.done"
            class="mt-2 inline-block text-[11px] px-2 py-0.5 rounded-full bg-neon-blue/15 text-neon-blue"
          >当前步骤 · 去完成</div>
        </button>
      </div>
      <div v-if="doneCount < steps.length" class="text-[11px] text-fg-sub mt-3 flex items-center justify-between">
        <span>建议：首次使用按 1→6 顺序执行；数据准备支持 Dry-Run 试运行防呆，压测前先灌入贴近业务语义的测试数据，报告才可信。</span>
        <button
          class="shrink-0 ml-3 inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-neon-green/40 bg-neon-green/10 text-neon-green hover:bg-neon-green/20 transition-all"
          @click="router.push('/ai?auto=1')"
        >
          <Zap class="w-3 h-3" /> 让 AI 自动执行
        </button>
      </div>
      <div v-else class="text-[11px] text-neon-green mt-3">✓ 全部步骤已完成，可回到压测中心继续多轮压测对比，或让 AI 助手生成一份完整性能检测报告。</div>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4 flex items-center gap-2">
          <Bot class="w-4 h-4 text-neon-green" /> AI 快捷指令
        </div>
        <div class="space-y-3">
          <button
            v-for="q in quickActions" :key="q.label"
            class="w-full text-left px-4 py-3 rounded-lg bg-white/5 border border-white/10 hover:border-neon-green/40 hover:bg-neon-green/5 transition-all"
            @click="goAi(q.label)"
          >
            <div class="text-sm text-fg-main">{{ q.label }}</div>
            <div class="text-[11px] text-fg-sub mt-0.5">{{ q.desc }}</div>
          </button>
        </div>
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3 flex items-center gap-2">
          <Gauge class="w-4 h-4 text-neon-blue" /> 最近压测任务
        </div>
        <div v-if="!stressTasks.length" class="text-xs text-fg-sub py-6 text-center">暂无压测任务</div>
        <div v-else class="space-y-2">
          <div v-for="t in stressTasks" :key="t.id" class="flex items-center justify-between px-3 py-2 rounded-lg bg-white/5">
            <div class="min-w-0">
              <div class="text-xs text-fg-main truncate">{{ t.taskName || t.targetUrl }}</div>
              <div class="text-[11px] text-fg-sub">{{ t.concurrency }} 并发 · {{ t.durationSec }}s</div>
            </div>
            <span class="text-[11px] px-2 py-0.5 rounded-full shrink-0" :class="taskStatusClass(t.status)">{{ t.status }}</span>
          </div>
        </div>
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3 flex items-center gap-2">
          <Database class="w-4 h-4 text-neon-blue" /> 最近慢 SQL
        </div>
        <div v-if="!slowSqls.length" class="text-xs text-fg-sub py-6 text-center">暂无慢 SQL</div>
        <div v-else class="space-y-2">
          <div v-for="s in slowSqls" :key="s.id" class="px-3 py-2 rounded-lg bg-white/5">
            <div class="text-xs text-fg-main truncate font-mono">{{ s.sqlText }}</div>
            <div class="text-[11px] mt-1 flex items-center justify-between">
              <span class="text-fg-sub">{{ s.source }}</span>
              <span class="text-amber-400">{{ Number(s.costMs).toFixed(1) }}ms</span>
            </div>
          </div>
        </div>
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3 flex items-center gap-2">
          <Bug class="w-4 h-4 text-amber-400" /> 最近性能问题
        </div>
        <div v-if="!issues.length" class="text-xs text-fg-sub py-6 text-center">暂无性能问题</div>
        <div v-else class="space-y-2">
          <div v-for="i in issues" :key="i.id" class="flex items-start justify-between gap-2 px-3 py-2 rounded-lg bg-white/5">
            <div class="min-w-0">
              <div class="text-xs text-fg-main truncate">{{ i.title }}</div>
              <div class="text-[11px] text-fg-sub mt-0.5">{{ i.issueType }} · {{ i.severity }}</div>
            </div>
            <span class="text-[11px] px-2 py-0.5 rounded-full shrink-0" :class="issueStatusClass(i.status)">{{ i.status }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { LayoutDashboard, Gauge, Database, Bug, Bot, Users, Zap, Boxes, DatabaseZap, Route, Wrench, Rocket, Check, ArrowRight } from 'lucide-vue-next'
// Zap 同时用于 AI 自动执行入口
import { taskStatusClass, issueStatusClass } from '../utils/format'
import { appApi, stressApi, slowSqlApi, sourceApi, traceApi, dataGenApi } from '../api'

const router = useRouter()
const apps = ref<Array<{ appName: string; status: string }>>([])
const repos = ref<Array<Record<string, any>>>([])
const stressTasks = ref<Array<Record<string, any>>>([])
const genTasks = ref<Array<Record<string, any>>>([])
const traces = ref<Array<Record<string, any>>>([])
const slowSqls = ref<Array<Record<string, any>>>([])
const issues = ref<Array<Record<string, any>>>([])

const metrics = computed(() => [
  { label: '被测项目', value: repos.value.length, hint: '已导入源码仓库', icon: Boxes },
  { label: '接入应用', value: apps.value.length, hint: '探针已注册实例', icon: Users },
  { label: '压测任务', value: stressTasks.value.length, hint: '最近任务', icon: Gauge },
  { label: '灌数任务', value: genTasks.value.length, hint: '数据准备记录', icon: DatabaseZap },
  { label: '慢 SQL 数', value: slowSqls.value.length, hint: '最近捕获', icon: Database },
  { label: '待处理问题', value: issues.value.filter((i) => i.status === 'open').length, hint: '未分析', icon: Bug }
])

/** 快速开始六步引导：完成状态由平台数据自动判定，点击直达对应页面 */
const steps = computed(() => [
  {
    title: '导入 / 选择项目',
    done: repos.value.length > 0,
    doneHint: `已导入 ${repos.value.length} 个项目`,
    todoHint: '上传源码或 git 导入被测项目',
    to: '/projects'
  },
  {
    title: '灌注测试数据',
    done: genTasks.value.some((t) => t.status === 'finished'),
    doneHint: '已有灌数任务完成',
    todoHint: '联表 Mock 数据 + 试运行防呆',
    to: '/stress?tab=datagen'
  },
  {
    title: '配置压测任务',
    done: stressTasks.value.length > 0,
    doneHint: `已创建 ${stressTasks.value.length} 个任务`,
    todoHint: '选项目自动带出接口清单',
    to: '/stress?tab=stress'
  },
  {
    title: '获取压测结果',
    done: stressTasks.value.some((t) => t.status === 'finished'),
    doneHint: '已有任务完成出报告',
    todoHint: '查看 QPS/RT/容量评估报告',
    to: '/stress?tab=stress'
  },
  {
    title: '分析调用链',
    done: traces.value.length > 0,
    doneHint: `已捕获 ${traces.value.length} 条链路`,
    todoHint: '压测流量产生调用链后分析',
    to: '/trace'
  },
  {
    title: '问题优化',
    done: issues.value.length > 0,
    doneHint: `已识别 ${issues.value.length} 个问题`,
    todoHint: 'N+1 / 慢 SQL 源码定位与修复',
    to: '/optimize'
  }
])

const doneCount = computed(() => steps.value.filter((s) => s.done).length)
/** 第一个未完成步骤即当前引导位置 */
const currentIdx = computed(() => {
  const idx = steps.value.findIndex((s) => !s.done)
  return idx === -1 ? steps.value.length : idx
})

function stepClass(done: boolean, isCurrent: boolean) {
  if (done) return 'bg-neon-green/5 border-neon-green/25 cursor-pointer hover:border-neon-green/50'
  if (isCurrent) return 'bg-neon-blue/5 border-neon-blue/40 cursor-pointer hover:border-neon-blue shadow-[0_0_18px_rgba(14,165,233,0.15)]'
  return 'bg-white/[0.03] border-white/10 cursor-pointer hover:border-white/25'
}

const quickActions = [
  { label: '分析最近慢 SQL 根因并给出修复方案', desc: '调用链 + 源码关联 + 大模型多方案' },
  { label: '当前性能能承受多少并发、何时扩容', desc: '容量评估 + 扩容时机建议' },
  { label: '生成一份性能检测报告', desc: '汇总压测 / JVM / 慢 SQL / 问题' }
]

async function refresh() {
  const [a, r, t, g, tr, s, i] = await Promise.allSettled([
    appApi.list(), sourceApi.repos(50), stressApi.list({ limit: 5 }),
    dataGenApi.list(10), traceApi.list({ limit: 5 }),
    slowSqlApi.list({ limit: 5 }), sourceApi.issues({ limit: 5 })
  ])
  if (a.status === 'fulfilled') apps.value = a.value.data
  if (r.status === 'fulfilled') repos.value = r.value.data
  if (t.status === 'fulfilled') stressTasks.value = t.value.data
  if (g.status === 'fulfilled') genTasks.value = g.value.data
  if (tr.status === 'fulfilled') traces.value = tr.value.data
  if (s.status === 'fulfilled') slowSqls.value = s.value.data
  if (i.status === 'fulfilled') issues.value = i.value.data
}

function goAi(message: string) {
  router.push({ path: '/ai', query: { msg: message } })
}

onMounted(refresh)
</script>
