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
          <span class="text-[11px] font-normal text-fg-sub">七步完成性能检测闭环：项目 → 部署(可选) → 数据 → 压测 → 结果 → 调用链 → 优化</span>
        </div>
        <div class="flex gap-2 shrink-0">
          <button
            class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs font-medium bg-neon-green/15 text-neon-green border border-neon-green/40 hover:bg-neon-green/25 transition-all"
            @click="startFresh"
          >
            <Rocket class="w-3.5 h-3.5" /> 快速开始
          </button>
          <button
            class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs font-medium bg-neon-blue/15 text-neon-blue border border-neon-blue/40 hover:bg-neon-blue/25 transition-all"
            @click="router.push('/ai?auto=1')"
          >
            <Zap class="w-3.5 h-3.5" /> AI 开始
          </button>
        </div>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-7 gap-3">
        <div v-for="(s, idx) in steps" :key="s.title"
             class="text-left p-4 rounded-xl border border-white/10 bg-white/[0.03]">
          <span class="w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-bold bg-white/10 text-fg-sub mb-2">{{ idx + 1 }}</span>
          <div class="text-sm font-medium text-fg-main">{{ s.title }}</div>
          <div class="text-[11px] mt-1 text-fg-sub">{{ s.desc }}</div>
        </div>
      </div>
      <div class="text-[11px] text-fg-sub mt-3">
        点击「快速开始」开启全新流程：选择项目 → 部署（可选，自动 / 手动接入 agent）→ 灌数据（Dry-Run 试运行防呆）→ 压测 → 看报告 → 析链路 → 优化；「AI 开始」由 AI 助手自动串联全流程。
      </div>
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
import { LayoutDashboard, Gauge, Database, Bug, Bot, Users, Zap, Boxes, DatabaseZap, Rocket } from 'lucide-vue-next'
import { taskStatusClass, issueStatusClass } from '../utils/format'
import { appApi, stressApi, slowSqlApi, sourceApi, traceApi, dataGenApi } from '../api'
import { useRepoStore } from '../stores/repoStore'

const router = useRouter()
const apps = ref<Array<{ appName: string; status: string }>>([])
const repos = ref<Array<Record<string, any>>>([])
const repoStore = useRepoStore()
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

/** 快速开始七步引导：平等展示，点击直达向导对应步骤 */
const steps = [
  { title: '导入 / 选择项目', desc: '上传源码或 git 导入被测项目' },
  { title: '部署（可选）', desc: '自动部署或生成带 agent 启动命令' },
  { title: '灌注测试数据', desc: '联表 Mock 数据 + 试运行防呆' },
  { title: '配置压测任务', desc: '选项目自动带出接口清单' },
  { title: '获取压测结果', desc: '查看 QPS/RT/容量评估报告' },
  { title: '分析调用链', desc: '压测流量产生调用链后分析' },
  { title: '问题优化', desc: 'N+1 / 慢 SQL 源码定位与修复' }
]

function startFresh() {
  // 快速开始 = 每次全新流程，从第 1 步进入向导
  router.push('/quick-start?step=0')
}

const quickActions = [
  { label: '分析最近慢 SQL 根因并给出修复方案', desc: '调用链 + 源码关联 + 大模型多方案' },
  { label: '当前性能能承受多少并发、何时扩容', desc: '容量评估 + 扩容时机建议' },
  { label: '生成一份性能检测报告', desc: '汇总压测 / JVM / 慢 SQL / 问题' }
]

async function refresh() {
  const [a, t, g, tr, s, i] = await Promise.allSettled([
    appApi.list(), stressApi.list({ limit: 5 }),
    dataGenApi.list(10), traceApi.list({ limit: 5 }),
    slowSqlApi.list({ limit: 5 }), sourceApi.issues({ limit: 5 })
  ])
  // 项目列表走全局缓存（首次加载后不再重复拉取）
  repoStore.load().then(() => { repos.value = repoStore.repos }).catch(() => {})
  if (a.status === 'fulfilled') apps.value = a.value.data
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
