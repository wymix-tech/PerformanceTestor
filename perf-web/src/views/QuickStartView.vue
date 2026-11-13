<template>
  <div>
    <div class="page-title">
      <Rocket class="w-5 h-5 text-neon-green" />
      快速开始
      <span class="text-xs font-normal text-fg-sub ml-2">带着项目上下文，六步完成性能检测闭环</span>
    </div>

    <!-- 步骤条 -->
    <div class="glass-card p-4 mb-5">
      <div class="flex items-center">
        <template v-for="(s, i) in stepDefs" :key="s.title">
          <button
            class="flex items-center gap-2 shrink-0 group"
            @click="goStep(i)"
          >
            <span
              class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-all"
              :class="badgeClass(i)"
            >
              <Check v-if="done(i)" class="w-4 h-4" />
              <template v-else>{{ i + 1 }}</template>
            </span>
            <span
              class="text-xs hidden md:block"
              :class="i === step ? 'text-fg-main font-medium' : done(i) ? 'text-neon-green' : 'text-fg-sub'"
            >{{ s.title }}</span>
          </button>
          <div v-if="i < stepDefs.length - 1" class="flex-1 h-0.5 mx-2 rounded" :class="done(i) ? 'bg-neon-green/50' : 'bg-white/10'" />
        </template>
      </div>
    </div>

    <!-- Step 1 项目 -->
    <div v-if="step === 0" class="glass-card p-5">
      <div class="flex items-center justify-between mb-1">
        <div class="text-sm font-medium">选择被测项目</div>
        <button
          class="text-xs inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-neon-blue/40 bg-neon-blue/10 text-neon-blue hover:bg-neon-blue/20 transition-all"
          @click="importPanelOpen = !importPanelOpen"
        >
          + 导入新项目
        </button>
      </div>
      <div class="text-[11px] text-fg-sub mb-4">项目贯穿后续所有步骤（接口清单 / 源码分析 / 数据归属）。</div>

      <!-- 内联导入面板（无项目自动展开；已有项目点「+ 导入新项目」展开） -->
      <div v-if="importPanelOpen" class="rounded-xl border border-neon-blue/30 bg-neon-blue/5 p-4 mb-4">
        <div class="text-sm font-medium mb-1">{{ repos.length ? '导入新项目' : '还没有项目 — 直接在这里导入' }}</div>
        <div class="text-[11px] text-fg-sub mb-3">支持 zip 上传 / Git 地址 / 本地目录三种方式，导入完成后自动选中新项目</div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div class="space-y-2">
            <label class="text-xs text-fg-sub font-medium">① Zip 上传</label>
            <input type="file" accept=".zip" class="input-text w-full text-xs" @change="(e: any) => zipFile = e.target.files?.[0] ?? null" />
            <button class="btn-primary w-full justify-center text-xs" :disabled="!zipFile || importing" @click="doImportZip">
              {{ importing ? '上传中…' : '上传导入' }}
            </button>
          </div>
          <div class="space-y-2">
            <label class="text-xs text-fg-sub font-medium">② Git 地址</label>
            <input v-model="gitUrl" class="input-text w-full text-xs" placeholder="https://git.xxx.com/group/repo.git" @change="fetchRemoteBranches" />
            <div class="flex gap-2">
              <select v-model="gitBranch" class="input-select w-28 text-xs" :disabled="!gitUrl || remoteBranchLoading">
                <option value="">{{ remoteBranchLoading ? '分支获取中…' : remoteBranches.length ? '选择分支' : '默认分支' }}</option>
                <option v-for="b in remoteBranches" :key="b" :value="b">{{ b }}</option>
              </select>
              <button class="btn-secondary px-2 text-xs" :disabled="remoteBranchLoading || !gitUrl" title="重新获取分支列表" @click="fetchRemoteBranches">↻</button>
            </div>
            <input v-model="gitUser" class="input-text w-full text-xs" placeholder="用户名（私有仓库，可空）" />
            <input v-model="gitToken" type="password" class="input-text w-full text-xs" placeholder="访问令牌 / 密码（公有仓库留空）" autocomplete="new-password" />
            <button class="btn-secondary w-full justify-center text-xs" :disabled="!gitUrl.trim() || importing" @click="doImportGit">
              {{ importing ? '提交中…' : '提交导入' }}
            </button>
          </div>
          <div class="space-y-2">
            <label class="text-xs text-fg-sub font-medium">③ 本地目录</label>
            <input v-model="localPath" class="input-text w-full text-xs" placeholder="/workspace/perf-demo" />
            <button class="btn-secondary w-full justify-center text-xs" :disabled="!localPath.trim() || importing" @click="doImportLocal">
              {{ importing ? '提交中…' : '导入' }}
            </button>
          </div>
        </div>
        <div v-if="importMsg" class="text-xs mt-3" :class="importMsg.ok ? 'text-neon-green' : 'text-red-400'">
          {{ importMsg.text }}
        </div>
      </div>

      <!-- 项目列表 -->
      <div v-if="repos.length" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        <button
          v-for="r in repos" :key="r.id"
          class="text-left p-4 rounded-xl border transition-all"
          :class="repoId === r.id ? 'border-neon-green bg-neon-green/10' : 'border-white/10 bg-white/[0.03] hover:border-white/30'"
          @click="selectRepo(r)"
        >
          <div class="text-sm text-fg-main">{{ r.repoName }}</div>
          <div class="text-[11px] text-fg-sub mt-1">{{ r.appName || '未绑定应用' }} · {{ r.fileCount }} 文件</div>
          <div v-if="repoId === r.id" class="text-[11px] text-neon-green mt-1">✓ 已选</div>
        </button>
      </div>
      <div v-else-if="!importMsg" class="text-xs text-fg-sub py-6 text-center">选择上方任一方式导入项目</div>

      <!-- 已选择提示下一步（导航统一走底部按钮） -->
      <div v-if="repoId" class="mt-4 rounded-xl border border-neon-green/30 bg-neon-green/5 px-4 py-3">
        <div class="text-xs text-neon-green">✓ 已选择「{{ repoName }}」，点击底部「下一步」灌注数据</div>
      </div>
    </div>

    <!-- Step 3 数据准备：与压测中心「数据准备」共用 DataGenForm，能力完全一致；
         仅把上游步骤选择的项目、以及部署后 agent 上报探测到的数据库连接自动带入 -->
    <div v-else-if="step === 2">
      <!-- 探测结果提示收敛为一行：组件内已有「✓ 项目与数据库连接已自动带入」标记，此处不再重复整块提示 -->
      <DataGenForm
        mode="compact"
        :initial-repo-id="repoId"
        :initial-conn-id="autoConnId"
        :initial-db-url="autoDbUrl"
        @created="onGenCreated"
      />
    </div>

    <!-- Step 2 部署（可选：自动 / 手动） -->
    <DeployStepPanel
      v-else-if="step === 1"
      :repo-id="repoId"
      :repo-name="repoName"
      :app-name="appName"
      @deployed="onDeployed"
    />

    <!-- Step 4 压测 -->
    <div v-else-if="step === 3" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">配置并发起压测 <span class="text-neon-green text-xs ml-1">项目：{{ repoName }}</span></div>
      <div class="text-[11px] text-fg-sub mb-4">参数与压测中心一致（接口/方法/引擎/预热/分档并发）。需要完整配置？<router-link class="text-neon-blue hover:underline" to="/stress">打开压测中心</router-link></div>
      <!-- 接口清单加载/失败反馈：列表为空时下拉会被整块隐藏，此处保证状态可见且可重试 -->
      <div class="flex items-center gap-2 mb-3 text-[11px]">
        <span v-if="resyncing" class="text-fg-sub">与源码同步中…</span>
        <span v-else-if="endpointsLoading" class="text-fg-sub">接口扫描中…</span>
        <template v-else-if="endpoints.length">
          <span class="text-neon-green">已扫描到 {{ endpoints.length }} 个接口，可在上方下拉选择</span>
        </template>
        <template v-else-if="endpointMsg">
          <span class="text-red-400">{{ endpointMsg.text }}</span>
          <button class="text-neon-blue hover:underline cursor-pointer" @click="loadEndpoints">重试</button>
        </template>
        <span v-else-if="repoId" class="text-fg-sub">该项目暂无接口清单</span>
        <!-- 源码变更后重新拉取并重建索引，再重扫接口（与压测中心「↻ 与源码重新同步」一致） -->
        <button
          v-if="repoId"
          class="text-neon-blue hover:underline cursor-pointer shrink-0"
          :disabled="resyncing || endpointsLoading"
          @click="resyncAndScan"
        >{{ resyncing ? '同步中…' : '↻ 与源码重新同步' }}</button>
        <span v-if="resyncMsg" class="shrink-0" :class="resyncMsg.ok ? 'text-neon-green' : 'text-red-400'">
          {{ resyncMsg.text }}
        </span>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <!-- 压测参数公共组件（与压测中心共用同一表单实现） -->
        <StressForm
          ref="stressFormRef"
          :endpoints="endpoints"
          :auto-base-url="instBaseUrl"
          default-target-url="http://127.0.0.1:8081/api/user/list?limit=200"
          :disabled="busy"
          submit-label="发起压测"
          @submit="startStress"
        />
        <div class="rounded-xl bg-white/5 p-4 text-xs space-y-1.5">
          <div class="text-fg-sub font-medium mb-1">任务状态</div>
          <div v-if="!stressTask" class="text-fg-sub">尚未发起</div>
          <template v-else>
            <div><span :class="taskStatusClass(stressTask.status)">{{ stressTask.status }}</span> <span class="text-fg-sub ml-2">#{{ stressTask.id }} {{ stressTask.concurrency }} 并发 × {{ stressTask.durationSec }}s</span></div>
            <div class="text-fg-sub font-mono text-[11px] truncate">{{ stressTask.targetUrl }}</div>
          </template>
        </div>
      </div>
    </div>

    <!-- Step 5 获取结果 -->
    <div v-else-if="step === 4" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">获取压测结果</div>
      <div class="text-[11px] text-fg-sub mb-4">任务完成后自动拉取报告摘要。<router-link class="text-neon-blue hover:underline" to="/stress">打开完整压测报告</router-link></div>
      <div v-if="!stressTask" class="text-xs text-fg-sub py-6 text-center">
        <div class="mb-3">本向导内未发起压测，或会话已被清空</div>
        <router-link class="btn-primary inline-flex" to="/stress">前往压测中心查看历史报告</router-link>
      </div>
      <div v-else-if="!report" class="text-xs text-fg-sub py-8 text-center flex items-center justify-center gap-2">
        <span class="w-2 h-2 rounded-full bg-neon-green animate-pulse"></span> 任务 {{ stressTask.status }}，等待完成…
      </div>
      <div v-else class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">总请求</div><div class="text-xl font-bold neon-text mt-1">{{ report.totalRequests ?? '—' }}</div></div>
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">平均 QPS</div><div class="text-xl font-bold neon-text mt-1">{{ report.qps ?? '—' }}</div></div>
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">P95 (ms)</div><div class="text-xl font-bold neon-text mt-1">{{ report.p95Ms ?? '—' }}</div></div>
        <div class="rounded-xl bg-white/5 p-4"><div class="text-[11px] text-fg-sub">错误率</div><div class="text-xl font-bold neon-text mt-1">{{ report.errorRate != null ? (report.errorRate * 100).toFixed(2) + '%' : '—' }}</div></div>
        <div v-if="capacityText" class="col-span-full rounded-xl bg-neon-green/5 border border-neon-green/20 p-3 text-xs text-fg-sub">{{ capacityText }}</div>
      </div>
    </div>

    <!-- Step 6 分析调用链 -->
    <div v-else-if="step === 5" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">分析调用链 <span class="text-neon-green text-xs ml-1">{{ appName }}</span></div>
      <div class="text-[11px] text-fg-sub mb-4">压测流量产生的调用链（热点方法耗时树）。<router-link class="text-neon-blue hover:underline" to="/trace">打开调用链分析</router-link></div>
      <div v-if="!traces.length" class="text-xs text-fg-sub py-8 text-center">暂无调用链，回上一步压测产生流量后自动捕获</div>
      <div v-else class="space-y-2">
        <button v-for="t in traces" :key="t.id" class="w-full flex items-center justify-between px-4 py-3 rounded-lg bg-white/5 hover:bg-white/10 transition-all text-left" @click="router.push('/trace')">
          <div class="min-w-0">
            <div class="text-xs text-fg-main truncate font-mono">{{ t.rootMethod || t.entryMethod || t.traceId }}</div>
            <div class="text-[11px] text-fg-sub mt-0.5">{{ t.startTime || '' }}</div>
          </div>
          <div class="text-sm text-amber-400 shrink-0 ml-3">{{ Number(t.totalMs || t.durationMs || 0).toFixed(1) }}ms</div>
        </button>
      </div>
    </div>

    <!-- Step 7 问题优化 -->
    <div v-else class="glass-card p-5">
      <div class="text-sm font-medium mb-1">问题优化</div>
      <div class="text-[11px] text-fg-sub mb-4">平台自动识别的性能问题（N+1 / 慢 SQL / 瓶颈），可让 AI 关联源码给出修复方案。<router-link class="text-neon-blue hover:underline" to="/optimize">打开性能优化</router-link></div>
      <div v-if="!issues.length" class="text-xs text-fg-sub py-6 text-center">暂无问题记录 — 可让 AI 立即分析</div>
      <div v-else class="space-y-2 mb-4">
        <div v-for="i in issues" :key="i.id" class="flex items-center justify-between px-4 py-2.5 rounded-lg bg-white/5">
          <div class="min-w-0">
            <div class="text-xs text-fg-main truncate">{{ i.title }}</div>
            <div class="text-[11px] text-fg-sub mt-0.5">{{ i.issueType }} · {{ i.status }}</div>
          </div>
          <span class="text-[11px] px-2 py-0.5 rounded-full shrink-0" :class="issueStatusClass(i.status)">{{ i.status }}</span>
        </div>
      </div>
      <button class="btn-primary justify-center" @click="goAiAnalyze">
        <Bot class="w-4 h-4" /> 让 AI 分析根因并给出修复方案
      </button>
    </div>

    <!-- 底部导航 -->
    <div class="flex items-center justify-between mt-5">
      <button class="btn-secondary" :disabled="step === 0" @click="goStep(step - 1)">上一步</button>
      <div class="text-xs text-fg-sub">步骤 {{ step + 1 }} / {{ stepDefs.length }} · 所选项目：<span class="text-neon-green">{{ repoName || '未选择' }}</span></div>
      <button v-if="step < stepDefs.length - 1" class="btn-primary" @click="goStep(step + 1)">下一步<ArrowRight class="w-4 h-4 ml-1" /></button>
      <button v-else class="btn-primary" @click="finishRound">完成本轮，回到总览<Check class="w-4 h-4 ml-1" /></button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Rocket, Check, ArrowRight, Bot } from 'lucide-vue-next'
import StressForm from '../components/stress/StressForm.vue'
import DeployStepPanel from '../components/quickstart/DeployStepPanel.vue'
import DataGenForm from '../components/datagen/DataGenForm.vue'
import { taskStatusClass, issueStatusClass } from '../utils/format'
import { appApi, dataGenApi, sourceApi, stressApi, traceApi, dbConnApi, deployApi, slowSqlApi } from '../api'
import { useRepoStore } from '../stores/repoStore'
import { saveQuickStart, loadQuickStart, clearQuickStart } from '../stores/quickStartNav'

const route = useRoute()
const router = useRouter()
const repoStore = useRepoStore()

const stepDefs = [
  { title: '选择项目' }, { title: '部署(可选)' }, { title: '灌注数据' },
  { title: '发起压测' }, { title: '获取结果' }, { title: '分析调用链' }, { title: '问题优化' }
]

const step = ref(0)
const busy = ref(false)
/** 项目列表共享缓存（repoStore 消除各视图重复拉取） */
const repos = computed(() => repoStore.repos)
const repoId = ref<number>(0)
const conns = ref<Array<Record<string, any>>>([])
const connId = ref<number>(0)

// 部署（可选步骤）：跳过或执行后进入压测
// 部署（可选步骤）：SSH 连接配置与执行逻辑已抽到 DeployStepPanel 子组件
/** 部署后从 agent 上报探测到的被测应用数据库连接（自动预填灌数步骤） */
const autoDbUrl = ref('')

/** agent 上报探测到的数据库所匹配的平台连接配置 ID（0 = 无匹配，由组件退回手填 URL） */
const autoConnId = computed(() => {
  if (!autoDbUrl.value) return 0
  const hit = conns.value.find((c) => normalizeUrl(c.dbUrl) === normalizeUrl(autoDbUrl.value))
  return hit ? hit.id : 0
})

/** 归一化 JDBC URL 用于比对（忽略用户名、参数顺序与末尾斜杠差异） */
function normalizeUrl(url: string): string {
  return (url || '').trim().replace(/\/+$/, '').toLowerCase()
}

/** 灌数任务创建成功：标记本步骤完成，并在正式灌数后自动进入压测步骤 */
function onGenCreated(payload: { task: any; dryRun: boolean }) {
  if (!payload?.dryRun) {
    sessionDone.value[2] = true // 本轮正式灌数已发起
    setTimeout(advance, 1200)
  }
}
const endpoints = ref<Array<Record<string, any>>>([])
/** 接口清单加载态与结果反馈（为空时下拉会被隐藏，必须将失败原因显式呈现） */
const endpointsLoading = ref(false)
const endpointMsg = ref<{ ok: boolean; text: string } | null>(null)
/** 与源码重新同步状态（重拉源码 + 重建索引 + 重扫接口） */
const resyncing = ref(false)
const resyncMsg = ref<{ ok: boolean; text: string } | null>(null)
const stressFormRef = ref<InstanceType<typeof StressForm> | null>(null)
/** agent 上报的服务地址（StressForm 自动预填；模板命令与预填共用实例探测） */
const instBaseUrl = computed(() => {
  const inst = appInstances.value.find((a) => a.appName === appName.value || a.appName === repoName.value)
  return inst?.appHost ? 'http://' + inst.appHost + (inst.appPort ? ':' + inst.appPort : '') : ''
})
const stressTask = ref<any>(null)
const report = ref<any>(null)
const capacityText = ref('')

// ===== 跨页面导航：持久化向导进度 =====
// 用户在向导内会跳到压测中心/调用链/性能优化/AI 助手看细节，而侧边栏没有「快速开始」
// 入口，一旦点出去就回不来。这里把进度写入 sessionStorage，由 MainLayout 顶栏渲染
// 全局「返回快速开始」按钮，任何页面都能一步回到当前步骤。
function cacheRunState() {
  saveQuickStart({
    step: step.value,
    stepTitle: stepDefs[step.value]?.title || '',
    repoId: repoId.value,
    connId: connId.value,
    sessionDone: [...sessionDone.value],
    stressTask: stressTask.value,
    report: report.value,
    capacityText: capacityText.value,
    traces: traces.value,
    issues: issues.value,
    savedAt: Date.now()
  })
}

function restoreRunState() {
  const s = loadQuickStart()
  if (!s) return false
  step.value = s.step ?? 0
  repoId.value = s.repoId ?? 0
  connId.value = s.connId ?? 0
  sessionDone.value = s.sessionDone?.length === stepDefs.length
    ? [...s.sessionDone]
    : [false, false, false, false, false, false, false]
  stressTask.value = s.stressTask ?? null
  report.value = s.report ?? null
  capacityText.value = s.capacityText ?? ''
  traces.value = s.traces ?? []
  issues.value = s.issues ?? []
  // 压测仍在进行中：恢复轮询，避免状态卡死
  if (stressTask.value && (stressTask.value.status === 'running' || stressTask.value.status === 'pending')) {
    startPolling()
  }
  return true
}
const traces = ref<Array<Record<string, any>>>([])
const issues = ref<Array<Record<string, any>>>([])
const appInstances = ref<Array<Record<string, any>>>([])
/** SSH 连接（可选部署步骤用） */
let pollTimer: any = null

/** 本轮向导会话完成标记：只认用户在本轮亲自完成的动作，与平台历史数据无关 */
const sessionDone = ref<boolean[]>([false, false, false, false, false, false, false])

const repoName = computed(() => repos.value.find((r) => r.id === repoId.value)?.repoName || '')
const appName = computed(() => repos.value.find((r) => r.id === repoId.value)?.appName || repoName.value)

/** 选中接口：自动带出方法与 URL（服务地址 + path）——与压测中心 applyEndpoint 一致 */

/** 打勾只认本轮向导内亲自完成的动作（浏览型步骤以到达为完成） */
function done(i: number) {
  return sessionDone.value[i]
}

function badgeClass(i: number) {
  if (done(i)) return 'bg-neon-green text-black'
  if (i === step.value) return 'bg-neon-blue text-black'
  return 'bg-white/10 text-fg-sub'
}

function goStep(i: number) {
  step.value = Math.max(0, Math.min(stepDefs.length - 1, i))
  // 浏览型步骤（分析调用链/问题优化）以到达为完成
  if (i >= 5) sessionDone.value[i] = true
  router.replace({ query: { step: String(step.value) } })
  onEnterStep()
}

/** 关键动作完成 → 自动推进到下一步（用户可随时手动回跳） */
function advance() {
  if (step.value < stepDefs.length - 1) goStep(step.value + 1)
}

// ===== Step1 内联项目导入（无项目时不离开向导） =====
const zipFile = ref<File | null>(null)
const gitUrl = ref('')
const gitBranch = ref('')
const gitUser = ref('')
const gitToken = ref('')
const remoteBranches = ref<string[]>([])
const remoteBranchLoading = ref(false)
const localPath = ref('')
const importing = ref(false)
const importMsg = ref<{ ok: boolean; text: string } | null>(null)
/** 导入面板开关：无项目时默认展开，否则点「+ 导入新项目」展开 */
const importPanelOpen = ref(false)
let importPoll: ReturnType<typeof setInterval> | null = null

/** 拉取远端分支列表（凭据可空，失败回落默认分支） */
async function fetchRemoteBranches() {
  const url = gitUrl.value.trim()
  remoteBranches.value = []
  gitBranch.value = ''
  if (!url) return
  remoteBranchLoading.value = true
  try {
    const r = await sourceApi.remoteBranches(url, gitUser.value || undefined, gitToken.value || undefined)
    remoteBranches.value = r.data || []
  } catch { /* 失败仍可用默认分支导入 */ }
  finally {
    remoteBranchLoading.value = false
  }
}

function selectRepo(r: any) {
  // 再次点击已选卡片 → 取消选择（连带清空该项目上下文）
  if (repoId.value === r.id) {
    repoId.value = 0
    sessionDone.value[0] = false
    endpoints.value = []
    traces.value = []
    issues.value = []
    return
  }
  repoId.value = r.id
  sessionDone.value[0] = true // 本轮亲自选择项目
  // 接口清单由 repoId 的 watcher 自动加载，此处不再重复调用
  loadTracesAndIssues()
}

/** zip 同步导入：刷新列表并自动选中新项目 */
async function reloadReposAndSelect(text: string) {
  await repoStore.reload()
  const newest = repoStore.repos[0]
  if (newest) {
    selectRepo(newest)
    importMsg.value = { ok: true, text: `「${newest.repoName}」${text}，已自动选中，可进入下一步` }
  }
}

async function doImportZip() {
  if (!zipFile.value) return
  importing.value = true
  try {
    await sourceApi.importZip(zipFile.value.name.replace(/\.zip$/, ''), zipFile.value)
    zipFile.value = null
    await reloadReposAndSelect('导入成功')
  } catch (e: any) {
    importMsg.value = { ok: false, text: '导入失败：' + (e?.response?.data?.message || e?.message || '请求异常') }
  } finally {
    importing.value = false
  }
}

async function doImportGit() {
  if (!gitUrl.value.trim()) return
  importing.value = true
  try {
    const url = gitUrl.value.trim()
    // 名称自动派生：URL 末段去 .git（与后端派生规则一致）
    const name = url.replace(/\.git$/, '').split('/').pop() || 'git 项目'
    await sourceApi.importGit({
      repoName: name,
      gitUrl: url,
      branch: gitBranch.value || undefined,
      gitUser: gitUser.value || undefined,
      gitToken: gitToken.value || undefined
    })
    gitUrl.value = ''
    gitBranch.value = ''
    remoteBranches.value = []
    afterImportStarted(name)
  } catch (e: any) {
    importMsg.value = { ok: false, text: '导入失败：' + (e?.response?.data?.message || e?.message || '请求异常') }
  } finally {
    importing.value = false
  }
}

async function doImportLocal() {
  if (!localPath.value.trim()) return
  importing.value = true
  try {
    const path = localPath.value.trim().replace(/\/+$/, '')
    const name = path.split('/').pop() || '本地项目'
    await sourceApi.importLocal({ repoName: name, localPath: path })
    localPath.value = ''
    afterImportStarted(name)
  } catch (e: any) {
    importMsg.value = { ok: false, text: '导入失败：' + (e?.response?.data?.message || e?.message || '请求异常') }
  } finally {
    importing.value = false
  }
}

/** git/local 异步导入轮询：出现新仓库即自动选中，最长 120s */
function afterImportStarted(fallbackName: string) {
  importMsg.value = { ok: true, text: `${fallbackName}导入请求已提交，正在拉取源码并建立索引（完成后自动选中）…` }
  stopImportPoll()
  const known = new Set(repoStore.repos.map((r) => r.id))
  let waited = 0
  importPoll = setInterval(async () => {
    waited += 2000
    try {
      await repoStore.load(true)
      const fresh = repoStore.repos.find((x: any) => !known.has(x.id))
      if (fresh) {
        selectRepo(fresh)
        importMsg.value = { ok: true, text: `「${fresh.repoName}」导入完成，已自动选中，可进入下一步` }
        stopImportPoll()
      } else if (waited > 120000) {
        importMsg.value = { ok: false, text: '导入超时（>120s），请到项目管理页查看状态' }
        stopImportPoll()
      }
    } catch { /* 忽略单次失败 */ }
  }, 2000)
}

function stopImportPoll() {
  if (importPoll) { clearInterval(importPoll); importPoll = null }
}

/**
 * 加载项目接口清单。
 *
 * <p>接口清单为空时 StressForm 会整块隐藏「接口」下拉（v-if="endpoints.length"），
 * 表现为「无法选择接口」，故此处必须把加载态与失败原因暴露到界面，并支持重试。
 */
async function loadEndpoints() {
  endpoints.value = []
  endpointMsg.value = null
  if (!repoId.value) return
  endpointsLoading.value = true
  try {
    // agent 上报的应用实例自动预填服务地址（host + 可选端口，经 StressForm autoBaseUrl）
    // http 拦截器已解包为 { code, message, data }（axios 类型无法感知，需断言）
    const r = (await sourceApi.endpoints(repoId.value)) as unknown as {
      code?: number; message?: string; data?: Array<Record<string, any>>
    }
    // 后端业务错误同样以 HTTP 200 返回（code!=0），需显式判定
    if (typeof r?.code === 'number' && r.code !== 0) {
      endpointMsg.value = { ok: false, text: r.message || '接口扫描失败' }
      return
    }
    endpoints.value = r?.data || [] // GET/POST 全保留（与压测中心一致）
    if (endpoints.value.length) {
      // 等表单挂载完成再回填（会话恢复到压测步骤时 StressForm 尚未渲染）
      await nextTick()
      stressFormRef.value?.selectFirstEndpoint()
    } else {
      endpointMsg.value = { ok: false, text: '项目源码中未扫描到接口，可到「项目管理」重新同步后再试' }
    }
  } catch (e) {
    endpointMsg.value = {
      ok: false,
      text: '接口扫描失败：' + ((e as { message?: string })?.message || '请求异常')
    }
  } finally {
    endpointsLoading.value = false
  }
}

/**
 * 与源码重新同步：重新拉取源码并重建索引，再重扫接口。
 *
 * <p>源码变更后（新增/修改了 Controller）接口清单不会自动更新，
 * 向导此前无此入口，只能去项目管理页操作，故补齐（与压测中心一致）。
 */
async function resyncAndScan() {
  if (!repoId.value || resyncing.value) return
  resyncing.value = true
  resyncMsg.value = null
  try {
    const r = (await sourceApi.resync(repoId.value)) as unknown as { code?: number; message?: string }
    if (typeof r?.code === 'number' && r.code !== 0) {
      resyncMsg.value = { ok: false, text: r.message || '同步失败' }
      return
    }
    await loadEndpoints()
    resyncMsg.value = endpoints.value.length
      ? { ok: true, text: `已同步，扫描到 ${endpoints.value.length} 个接口` }
      : { ok: false, text: '同步完成，但未扫描到接口' }
  } catch (e) {
    resyncMsg.value = {
      ok: false,
      text: '同步失败：' + ((e as { message?: string })?.message || '请求异常')
    }
  } finally {
    resyncing.value = false
  }
}

/**
 * 接口清单跟随项目自动加载。
 *
 * <p>此前仅在 selectRepo()（用户主动点选项目卡片）内调用，而跨页面返回 / 刷新页面时
 * repoId 由 restoreRunState() 从会话缓存直接恢复，不会经过 selectRepo，
 * 导致 endpoints 始终为空 → 压测步骤无接口可选。改为监听 repoId 后，
 * 主动点选、会话恢复、跨页返回三条路径都会自动加载。
 */
watch(
  () => repoId.value,
  (id) => {
    if (id) loadEndpoints()
    else {
      endpoints.value = []
      endpointMsg.value = null
    }
  }
)

// 灌数逻辑已全部交由 DataGenForm 公共组件（与压测中心「数据准备」一致），
// 向导仅负责把项目与探测到的数据库连接预填进去，并通过 @created 感知进度。

/** 发起压测：StressForm 提交标准请求体，向导补充任务名与应用归属 */
async function startStress(payload: Record<string, unknown>) {
  busy.value = true
  try {
    const body = {
      ...payload,
      taskName: `向导压测-${repoName.value}`,
      appName: appName.value || undefined
    }
    const r = await stressApi.create(body)
    stressTask.value = r.data
    sessionDone.value[3] = true // 本轮压测已发起
    startPolling()
    // 压测已发起 → 自动进入结果步骤（轮询拉取报告）
    setTimeout(advance, 1200)
  } finally {
    busy.value = false
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!stressTask.value) return
    const r = await stressApi.list({ limit: 10 })
    const t = (r.data || []).find((x: any) => x.id === stressTask.value.id)
    if (t) stressTask.value = t
    if (t?.status === 'finished') {
      stopPolling()
      const rep = await stressApi.report(stressTask.value.id)
      // rep.data 为整个压测任务对象（含 result 字段：统计指标/分档/容量评估）
      const data = rep.data || {}
      const result = data.result || {}
      report.value = result
      sessionDone.value[4] = true // 本轮报告已获取
      const cap = result.capacity
      if (cap) {
        const maxC = cap.maxConcurrency ?? '—'
        const maxQ = cap.maxQps ?? '—'
        const tag = cap.extrapolated ? '（外推估算）' : ''
        capacityText.value = `容量评估：最大可承受并发 ${maxC}，最大 QPS ${maxQ}${tag}`
      } else {
        capacityText.value = ''
      }
      cacheRunState() // 压测结果已生成：写入 session 缓存供跨路由恢复
      loadTracesAndIssues()
    }
  }, 2000)
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

async function loadTracesAndIssues() {
  try {
    const t = await traceApi.list({ app: appName.value || undefined, size: 5 })
    traces.value = t.items
  } catch { /* 忽略 */ }
  try {
    const i = await sourceApi.issues({ limit: 5 })
    issues.value = i.data || []
  } catch { /* 忽略 */ }
}

/**
 * 部署成功回调（DeployStepPanel 发出）：标记步骤完成 → 探测被测应用的数据库连接
 * （agent JdbcInterceptor 上报的慢 SQL 中带 dbUrl，用于灌数步骤自动预填）→ 自动进入下一步
 */
async function onDeployed() {
  sessionDone.value[1] = true // deploy ok
  try {
    const sl = await slowSqlApi.list({ size: 5 })
    const url = sl.items.map((x: any) => x.dbUrl).find((u: string) => !!u)
    if (url) {
      autoDbUrl.value = url
      const match = conns.value.find((c: any) => url.startsWith(String(c.dbUrl || '').split('?')[0]))
      if (match) connId.value = match.id
    }
  } catch { /* 探测失败不影响流程 */ }
  setTimeout(advance, 1500) // 部署成功自动进入灌数步骤
}

function goAiAnalyze() {
  router.push({ path: '/ai', query: { msg: `分析项目「${repoName.value}」最近的慢 SQL 与热点调用链根因，并给出修复方案` } })
}

/** 完成本轮，回到总览：本轮结束，清除会话缓存（下次进入为全新向导）。
 *  finishing 标记用于阻止卸载前的最后一次 watch 把状态又写回去。 */
let finishing = false
function finishRound() {
  finishing = true
  clearQuickStart()
  router.push('/dashboard')
}

function onEnterStep() {
  if (step.value === 4 && stressTask.value?.status === 'finished' && !report.value) startPolling()
}

onMounted(async () => {
  // 快速开始 = 全新引导流程；但用户在向导内会跳到压测中心/调用链/性能优化看细节，
  // 而侧边栏没有本页入口，点出去就回不来。故按入口区分：
  //   · 带 step 参数（总览页 /quick-start?step=0）= 明确开新一轮 → 以 URL 为准，step=0 时清空旧进度
  //   · 不带参数（从其他页面返回 / 刷新）          = 恢复上次进度，回到原步骤
  const q = route.query.step
  if (q !== undefined && Number.isFinite(Number(q))) {
    // step=0 是总览页的入口语义（开新一轮）；其余值来自向导内 goStep 的 replace，
    // 说明用户只是在本向导内刷新，数据仍需恢复，仅 step 以 URL 为准
    if (Number(q) === 0) clearQuickStart()
    else restoreRunState()
    step.value = Math.max(0, Math.min(stepDefs.length - 1, Number(q)))
  } else {
    restoreRunState()
  }
  const [c, a] = await Promise.allSettled([
    dbConnApi.list(), appApi.list()
  ])
  await repoStore.load().catch(() => {})
  importPanelOpen.value = repoStore.repos.length === 0 // 无项目自动展开导入面板
  if (c.status === 'fulfilled') conns.value = c.value.data || []
  if (a.status === 'fulfilled') appInstances.value = a.value.data || []
})

// 向导进度自动持久化：任何影响进度的状态变化都写入，供跨页面返回时恢复
watch(
  [step, repoId, connId, sessionDone, stressTask, report, traces, issues],
  () => {
    if (!finishing) cacheRunState()
  },
  { deep: true }
)

onUnmounted(stopPolling)
</script>
