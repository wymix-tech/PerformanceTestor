<template>
  <div>
    <div class="page-title">
      <Target class="w-5 h-5 text-neon-green" />
      性能优化
      <span class="text-xs font-normal text-fg-sub ml-2">问题关联 · 大模型多方案 · 修复提示词</span>
    </div>

    <!-- 页面级项目筛选（项目即应用；控制问题列表与一键创建） -->
    <div class="glass-card p-3 mb-4 flex items-center gap-3">
      <span class="text-sm font-medium">项目</span>
      <select v-model="issueApp" class="input-select w-44 text-xs">
        <option value="">全部项目（{{ issues.length }}）</option>
        <option v-for="a in issueApps" :key="a" :value="a">{{ a }}（{{ issues.filter(x => x.appName === a).length }}）</option>
      </select>
      <span class="text-[11px] text-fg-sub">项目即应用：筛选对问题列表与「一键创建」全局生效，创建的问题自动归属当前项目</span>
    </div>

    <!-- 数据库连接配置 -->
    <div class="mb-4">
      <div class="glass-card p-4">
        <button class="text-sm font-medium flex items-center gap-2" @click="connOpen = !connOpen">
          <span class="text-fg-sub">{{ connOpen ? '▾' : '▸' }}</span>数据库连接配置
          <span class="text-[11px] font-normal text-fg-sub">（分析慢 SQL 时自动执行 EXPLAIN 获取真实执行计划）</span>
        </button>
        <div v-if="connOpen" class="mt-4">
          <div class="grid grid-cols-1 md:grid-cols-7 gap-2 items-end">
            <div><label class="text-xs text-fg-sub">名称</label>
              <input v-model="connForm.connName" class="input-text w-full" placeholder="perf-demo-主库" /></div>
            <div><label class="text-xs text-fg-sub">主机标识（匹配慢 SQL dbHost）</label>
              <input v-model="connForm.host" class="input-text w-full" placeholder="127.0.0.1:3306/demo" /></div>
            <div><label class="text-xs text-fg-sub">绑定项目（项目即应用，选后自动接管 EXPLAIN）</label>
              <select v-model="connForm.appName" class="input-select w-full">
                <option value="">不绑定（按主机标识匹配）</option>
                <option v-for="name in projectNames" :key="name" :value="name">{{ name }}</option>
              </select></div>
            <div class="md:col-span-2"><label class="text-xs text-fg-sub">JDBC URL</label>
              <input v-model="connForm.dbUrl" class="input-text w-full" placeholder="jdbc:mysql://127.0.0.1:3306/demo" /></div>
            <div><label class="text-xs text-fg-sub">用户名</label>
              <input v-model="connForm.username" class="input-text w-full" placeholder="root" /></div>
            <div><label class="text-xs text-fg-sub">密码</label>
              <input v-model="connForm.password" type="password" class="input-text w-full" /></div>
          </div>
          <div class="flex items-center gap-2 mt-2">
            <label class="flex items-center gap-1.5 text-xs text-fg-sub">
              <input v-model="connForm.isDefault" type="checkbox" /> 设为默认（host 与项目都未匹配时使用）
            </label>
            <div class="flex-1"></div>
            <button class="btn-primary text-xs !px-3 !py-1.5" @click="saveConn">保存连接</button>
            <button class="btn-primary text-xs !px-3 !py-1.5 bg-neon-blue/80" @click="testConn">
              {{ testing ? '测试中…' : '测试连接' }}
            </button>
          </div>
          <div v-if="connTestResult" class="text-[11px] mt-2" :class="connTestResult.ok ? 'text-neon-green' : 'text-red-400'">
            {{ connTestResult.ok ? '连接成功' : '连接失败：' + connTestResult.error }}
          </div>
          <div v-if="connMsg" class="text-[11px] mt-2" :class="connMsg.ok ? 'text-neon-green' : 'text-red-400'">{{ connMsg.text }}</div>
          <div v-if="dbConns.length" class="mt-3 space-y-1.5">
            <div v-for="c in dbConns" :key="c.id"
              class="flex items-center gap-3 px-3 py-2 rounded text-xs border transition-all"
              :class="c.isDefault === 1 ? 'bg-neon-green/10 border-neon-green/30' : 'bg-white/5 border-white/10'">
              <span v-if="c.isDefault === 1" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-green/15 text-neon-green shrink-0">✓ 默认</span>
              <span class="text-fg-main shrink-0">{{ c.connName }}</span>
              <span v-if="c.appName" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/15 text-neon-blue shrink-0">项目：{{ c.appName }}</span>
              <span class="text-fg-sub font-mono truncate flex-1">{{ c.dbUrl }}</span>
              <button class="text-neon-blue hover:underline shrink-0" @click="makeDefault(c)" v-if="c.isDefault !== 1">设默认</button>
              <button class="text-red-400 hover:underline shrink-0" @click="removeConn(c)">删除</button>
            </div>
          </div>
          <div v-else class="text-xs text-fg-sub py-4 text-center">暂无连接配置，填写上方表单保存（保存后分析慢 SQL 时才会执行 EXPLAIN）</div>
        </div>
      </div>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-4">
        <div class="text-sm font-medium mb-3">性能问题（{{ filteredIssues.length }}）<span class="text-[11px] font-normal text-fg-sub ml-2">状态：pending 待分析 → analyzed 已出方案 → fixed 已修复</span></div>
        <div class="flex flex-wrap items-center gap-2 mb-3">
          <button
            v-for="t in issueTypes" :key="t.key"
            class="px-3 py-1 rounded-full text-[11px] border transition-all"
            :class="issueType === t.key ? 'bg-neon-green/15 text-neon-green border-neon-green/30' : 'text-fg-sub border-white/10'"
            @click="issueType = t.key; loadIssues()"
          >{{ t.label }}</button>
        </div>
        <div v-if="!filteredIssues.length" class="text-xs text-fg-sub py-8 text-center">暂无问题，可从下方检测结果一键创建</div>
        <div v-else class="space-y-2 max-h-[560px] overflow-y-auto pr-1">
          <div v-for="i in filteredIssues" :key="i.id" class="px-3 py-2.5 rounded-lg cursor-pointer border transition-all"
            :class="detail?.issue?.id === i.id ? 'bg-neon-green/10 border-neon-green/40' : 'bg-white/5 border-white/10 hover:border-neon-green/30'"
            @click="showIssueDetail(i.id)">
            <div class="text-xs text-fg-main truncate" :title="i.title">{{ i.title }}</div>
            <div class="flex justify-between mt-1 text-[11px] text-fg-sub">
              <span class="flex items-center gap-1.5 min-w-0">
                <span v-if="i.appName" class="px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue shrink-0">{{ i.appName }}</span>
                <span class="truncate">{{ i.issueType }}<template v-if="i.severity"> · {{ i.severity }}</template></span>
              </span>
              <span :class="statusClass(i.status)">{{ i.status }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="glass-card p-5 xl:col-span-2">
        <div v-if="!detail" class="text-xs text-fg-sub py-16 text-center">选择问题查看详情 / 多方案对比</div>
        <template v-else>
          <div class="flex items-center justify-between mb-2">
            <div class="text-sm font-medium">{{ detail.issue.title }}</div>
            <div class="flex items-center gap-2">
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(detail.issue.status)">{{ detail.issue.status }}</span>
              <button
                v-if="detail.issue.status === 'pending' && !needBindRepo"
                class="btn-primary text-xs !px-3 !py-1.5"
                :disabled="analyzing"
                @click="runAnalyze"
              >{{ analyzing ? '分析中…' : '执行分析生成方案' }}</button>
              <span v-if="detail.issue.issueType === 'slow_sql'" class="text-[11px] text-fg-sub">
                EXPLAIN{{ issueConn ? `连接：${issueConn.conn.connName}（${issueConn.reason}）` : '：未配置连接，仅基于证据分析' }}
              </span>
            </div>
          </div>

          <!-- 压测问题前置：必须先关联源码仓库 -->
          <div v-if="needBindRepo" class="p-3 rounded-lg bg-amber-400/5 border border-amber-400/30 mb-4">
            <div class="text-xs text-amber-400 mb-2">⚠ 压测问题必须先关联项目源码才能分析（用于定位接口对应的 Controller 源码并生成修复方案）</div>
            <div class="flex gap-2">
              <select v-model="bindRepoId" class="input-select flex-1 text-xs">
                <option :value="0">— 选择项目源码 —</option>
                <option v-for="r in repos" :key="r.id" :value="r.id">
                  {{ r.repoName }}{{ r.appName ? `（${r.appName}）` : '' }}
                </option>
              </select>
              <button class="btn-primary text-xs !px-3 !py-1.5" :disabled="!bindRepoId || bindingIssueRepo" @click="doBindRepo">
                {{ bindingIssueRepo ? '关联中…' : '关联并自动定位源码' }}
              </button>
            </div>
          </div>
          <div class="text-xs text-fg-sub mb-2">{{ detail.issue.issueType }} · 严重度 {{ detail.issue.severity }}
            <span v-if="detail.issue.sourceFile"> · {{ detail.issue.sourceFile.relativePath }}:{{ detail.issue.lineStart }}-{{ detail.issue.lineEnd }}</span>
          </div>

          <div v-if="evidenceSqlText" class="p-3 rounded-lg bg-black/30 border border-white/10 mb-4">
            <div class="text-xs text-fg-sub mb-1">完整 SQL</div>
            <pre class="text-[11px] text-fg-main whitespace-pre-wrap break-all leading-relaxed max-h-[180px] overflow-y-auto font-mono">{{ evidenceSqlText }}</pre>
          </div>

          <div v-if="detail.issue.rootCause" class="p-3 rounded-lg bg-white/5 mb-4">
            <div class="text-xs text-fg-sub mb-1">根因分析</div>
            <div class="text-xs text-fg-main leading-relaxed">{{ detail.issue.rootCause }}</div>
          </div>

          <!-- 数据库执行计划（慢 SQL 分析时 EXPLAIN 采集） -->
          <div v-if="evidenceExplain" class="p-3 rounded-lg bg-black/30 border mb-4" :class="evidenceConsistent ? 'border-neon-blue/20' : 'border-red-400/40'">
            <div class="text-xs mb-1 font-medium" :class="evidenceConsistent ? 'text-neon-blue' : 'text-red-400'">
              数据库执行计划（EXPLAIN 实测）
              <span v-if="evidenceTarget" class="font-normal text-fg-sub ml-1">目标：{{ evidenceTarget }}</span>
            </div>
            <div v-if="!evidenceConsistent" class="text-[11px] text-red-400 mb-2 bg-red-400/10 rounded px-2 py-1">
              ⚠ EXPLAIN 执行库（{{ evidenceTarget }}）与慢 SQL 实际执行库（{{ evidenceActualDb }}）不一致，结果可能失真——请核对该项目的连接绑定或补充匹配的连接配置
            </div>
            <pre class="text-[11px] text-fg-main whitespace-pre-wrap leading-relaxed max-h-[220px] overflow-y-auto font-mono">{{ evidenceExplain }}</pre>
          </div>

          <div v-if="detail.suggestions?.length" class="mb-4">
            <div class="text-sm font-medium mb-3">修复方案（{{ detail.suggestions.length }}）</div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div
                v-for="s in detail.suggestions" :key="s.id"
                class="p-4 rounded-lg border transition-all cursor-pointer"
                :class="s.selected ? 'border-neon-green/50 bg-neon-green/5' : 'border-white/10 bg-white/5 hover:border-neon-green/30'"
                @click="selectSuggestion(s)"
              >
                <div class="text-sm text-fg-main flex items-center justify-between">
                  {{ s.title }}
                  <span v-if="s.selected" class="text-[11px] text-neon-green shrink-0 ml-2">✓ 已选定</span>
                </div>
                <div class="text-[11px] text-fg-sub mt-2 leading-relaxed">{{ s.benefit }}</div>
                <div class="text-[11px] text-fg-sub mt-1 leading-relaxed" v-if="s.risk">风险：{{ s.risk }}</div>
                <div v-if="s.filesJson" class="text-[11px] text-neon-blue mt-2 break-all">涉及：{{ formatFiles(s.filesJson) }}</div>
                <button class="btn-primary mt-3 text-xs !px-3 !py-1.5" @click.stop="selectSuggestion(s)">
                  {{ s.selected ? '重新生成提示词' : '选定并生成提示词' }}
                </button>
              </div>
            </div>
          </div>

          <div v-if="selectedPrompt" class="p-4 rounded-lg bg-black/40 border border-neon-green/20">
            <div class="flex items-center justify-between mb-2">
              <div class="text-sm font-medium text-neon-green">修复提示词（方案：{{ selectedSuggestion?.title }}）</div>
              <div class="flex items-center gap-3">
                <button class="text-xs text-neon-blue hover:underline" @click="copyPrompt">复制</button>
                <button class="text-xs text-neon-blue hover:underline" @click="downloadPrompt">下载</button>
              </div>
            </div>
            <pre class="text-[11px] text-fg-main whitespace-pre-wrap leading-relaxed max-h-[380px] overflow-y-auto font-mono">{{ selectedPrompt }}</pre>
          </div>
          <div v-else-if="detail.suggestions?.length" class="text-xs text-fg-sub py-4 text-center">点击上方方案卡片即可选定并生成修复提示词</div>
        </template>
      </div>
    </div>

    <!-- 快速创建 -->
    <div class="glass-card p-5 mt-4">
      <div class="text-sm font-medium mb-3">从检测结果一键创建问题（自动关联源码位置）</div>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div class="p-3 rounded-lg bg-white/5">
          <div class="text-xs text-fg-sub mb-2">慢 SQL → 调用链 / 实体定位（MyBatis・JPA・Hibernate 通用）</div>
          <select v-model="fastSlowSql" class="input-select w-full mb-2" title="">
            <option :value="0">— 最近慢 SQL —</option>
            <option v-for="s in recentSlow" :key="s.id" :value="s.id" :title="s.sqlText">{{ s.sqlText?.slice(0, 80) }}（{{ Number(s.costMs).toFixed(0) }}ms）</option>
          </select>
          <button class="btn-primary w-full justify-center text-xs" :disabled="!fastSlowSql || !currentRepo" @click="createFromSlowSql">创建问题</button>
        </div>
        <div class="p-3 rounded-lg bg-white/5">
          <div class="text-xs text-fg-sub mb-2">热点方法 → 精确匹配</div>
          <input v-model="hotClass" class="input-text w-full mb-2" placeholder="com.demo.controller.UserController" />
          <input v-model="hotMethod" class="input-text w-full mb-2" placeholder="listUsers" />
          <button class="btn-primary w-full justify-center text-xs" :disabled="!hotClass || !hotMethod" @click="createFromHot">创建问题</button>
        </div>
        <div class="p-3 rounded-lg bg-white/5">
          <div class="text-xs text-fg-sub mb-2">压测任务 → URL 关联</div>
          <select v-model="fastStress" class="input-select w-full mb-2">
            <option :value="0">— 最近压测任务 —</option>
            <option v-for="s in stressTasks" :key="s.id" :value="s.id">{{ s.taskName || s.targetUrl }}</option>
          </select>
          <button class="btn-primary w-full justify-center text-xs" :disabled="!fastStress || !currentRepo" @click="createFromStress">创建问题</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Target } from 'lucide-vue-next'
import { sourceApi, slowSqlApi, stressApi, dbConnApi } from '../api'

const repos = ref<Array<Record<string, any>>>([])
const currentRepo = ref<Record<string, any> | null>(null)
// 项目即应用：项目名集合（仓库名），连接绑定的可选值
const projectNames = computed(() => (repos.value || []).map((r: Record<string, any>) => r.repoName).filter(Boolean))

const issues = ref<Array<Record<string, any>>>([])
const detail = ref<Record<string, any> | null>(null)
const issueType = ref('')
// 类型筛选：key 为空 = 全部类型
const issueTypes = [
  { key: '', label: '全部' },
  { key: 'slow_sql', label: 'slow_sql' },
  { key: 'hot_method', label: 'hot_method' },
  { key: 'stress_bottleneck', label: 'stress_bottleneck' }
]
// 项目筛选：'' 为全部项目，按问题数据自动枚举
const issueApp = ref('')
const filteredIssues = computed(() =>
  issueApp.value ? issues.value.filter(i => i.appName === issueApp.value) : issues.value
)
const issueApps = computed(() =>
  [...new Set(issues.value.map(i => i.appName).filter(Boolean))]
)

// 页面级项目筛选变化时，联动刷新「一键创建」的最近慢 SQL 候选
watch(issueApp, () => {
  sourceApi.recentSlowSqls({ limit: 10, app: issueApp.value || undefined })
    .then((s) => (recentSlow.value = s.data))
})
const recentSlow = ref<Array<Record<string, any>>>([])
const stressTasks = ref<Array<Record<string, any>>>([])
const fastSlowSql = ref(0)
const fastStress = ref(0)
const hotClass = ref('')
const hotMethod = ref('')

function statusClass(s: string) {
  const map: Record<string, string> = {
    analyzed: 'text-neon-green bg-neon-green/10',
    analyzing: 'text-neon-blue bg-neon-blue/10',
    open: 'text-amber-400 bg-amber-400/10',
    failed: 'text-red-400 bg-red-400/10'
  }
  return map[s] || 'text-fg-sub bg-white/5'
}

async function loadRepos() {
  const r = await sourceApi.repos()
  repos.value = r.data
  currentRepo.value = repos.value[0] || null
}

async function loadIssues() {
  const r = await sourceApi.issues({ issueType: issueType.value || undefined, limit: 50 })
  issues.value = r.data
}

async function showIssueDetail(id: number) {
  const r = await sourceApi.issueDetail(id)
  detail.value = r.data
}

// 执行大模型分析（无 API Key 时服务端自动降级规则模板），成功后刷新详情与列表
const analyzing = ref(false)

// 压测问题分析前置：必须已关联源码仓库
const needBindRepo = computed(() =>
  !!detail.value &&
  detail.value.issue?.issueType === 'stress_bottleneck' &&
  !detail.value.issue?.sourceRepoId
)
const bindRepoId = ref(0)
const bindingIssueRepo = ref(false)
async function doBindRepo() {
  if (!detail.value || !bindRepoId.value || bindingIssueRepo.value) return
  bindingIssueRepo.value = true
  try {
    await sourceApi.bindIssueRepo(detail.value.issue.id, bindRepoId.value)
    await showIssueDetail(detail.value.issue.id)
    await loadIssues()
  } finally {
    bindingIssueRepo.value = false
  }
}

async function runAnalyze() {
  if (!detail.value || analyzing.value) return
  if (needBindRepo.value) {
    window.alert('压测问题必须先关联项目源码才能分析，请先在上方完成关联')
    return
  }
  analyzing.value = true
  try {
    await sourceApi.analyze(detail.value.issue.id)
    await showIssueDetail(detail.value.issue.id)
    await loadIssues()
  } finally {
    analyzing.value = false
  }
}

async function selectSuggestion(s: Record<string, any>) {
  if (!detail.value) return
  await sourceApi.selectSuggestion(detail.value.issue.id, s.id)
  await showIssueDetail(detail.value.issue.id)
}

// 当前选定方案（单选）及其提示词（promptMd 存在 fix_suggestion 上）
const selectedSuggestion = computed(() =>
  detail.value?.suggestions?.find((s: Record<string, any>) => Number(s.selected) === 1) || null
)
const selectedPrompt = computed(() => selectedSuggestion.value?.promptMd || '')

// 证据中的数据库执行计划摘要（EXPLAIN 实测结果）
const evidenceExplain = computed(() => {
  const ev = detail.value?.issue?.evidenceJson
  if (!ev) return ''
  try {
    const parsed = typeof ev === 'string' ? JSON.parse(ev) : ev
    const ex = parsed?.dbExplain
    if (ex?.summary) return String(ex.summary)
    if (ex && typeof ex === 'object') return JSON.stringify(ex, null, 2)
    return ''
  } catch {
    return ''
  }
})

// 当前问题的 EXPLAIN 连接自动切换结果：与后端 matchConn 严格同构（host 匹配 → 项目绑定 → 默认兜底）
const issueConn = computed<{ conn: Record<string, any>; reason: string } | null>(() => {
  const conns = dbConns.value
  if (!conns.length || !detail.value) return null
  let dbHost = ''
  try {
    const ev = detail.value.issue?.evidenceJson
    dbHost = ev ? (typeof ev === 'string' ? JSON.parse(ev).dbHost || '' : ev.dbHost || '') : ''
  } catch { /* 忽略解析失败 */ }
  const sorted = [...conns].sort((a, b) => Number(b.isDefault) - Number(a.isDefault))
  if (dbHost) {
    const byHost = sorted.find((c) => c.host && (dbHost.includes(c.host) || c.host.includes(dbHost)))
    if (byHost) return { conn: byHost, reason: '主机匹配' }
  }
  const app = detail.value.issue?.appName
  if (app) {
    const byApp = sorted.find((c) => c.appName === app)
    if (byApp) return { conn: byApp, reason: `项目绑定 ${app}（路由）` }
  }
  const def = sorted.find((c) => Number(c.isDefault) === 1)
  if (def) return { conn: def, reason: '默认兜底（注意核对实际库）' }
  return { conn: sorted[0], reason: '兜底（注意核对实际库）' }
})

// 慢 SQL 完整 SQL 文本（标题被截断时，在下方完整展示）
const evidenceSqlText = computed(() => {
  try {
    const ev = detail.value?.issue?.evidenceJson
    const parsed = ev ? (typeof ev === 'string' ? JSON.parse(ev) : ev) : {}
    return parsed?.sqlText || parsed?.sql || ''
  } catch { return '' }
})

// EXPLAIN 目标库与一致性（证据中的 target/actualDb/consistent 由后端分析时写入）
const evidenceTarget = computed(() => {
  try {
    const ev = detail.value?.issue?.evidenceJson
    const parsed = ev ? (typeof ev === 'string' ? JSON.parse(ev) : ev) : {}
    return parsed?.dbExplain?.target || ''
  } catch { return '' }
})
const evidenceActualDb = computed(() => {
  try {
    const ev = detail.value?.issue?.evidenceJson
    const parsed = ev ? (typeof ev === 'string' ? JSON.parse(ev) : ev) : {}
    return parsed?.dbExplain?.actualDb || ''
  } catch { return '' }
})
const evidenceConsistent = computed(() => {
  try {
    const ev = detail.value?.issue?.evidenceJson
    const parsed = ev ? (typeof ev === 'string' ? JSON.parse(ev) : ev) : {}
    return parsed?.dbExplain?.consistent !== false
  } catch { return true }
})

// 涉及文件格式化：兼容对象数组（file/path/name 键）与字符串数组
function formatFiles(filesJson: unknown): string {
  let arr: any[] = []
  try {
    const parsed = typeof filesJson === 'string' ? JSON.parse(filesJson) : filesJson
    if (Array.isArray(parsed)) arr = parsed
    else if (parsed && typeof parsed === 'object') arr = [parsed]
  } catch {
    return String(filesJson)
  }
  return arr
    .map((it) => {
      if (typeof it === 'string') return it
      const file = it.file || it.path || it.name || ''
      const action = it.action || it.change || ''
      return action ? `${file}（${action}）` : file
    })
    .filter(Boolean)
    .join('、')
}

async function copyPrompt() {
  if (!selectedPrompt.value) return
  await navigator.clipboard.writeText(selectedPrompt.value)
}

function downloadPrompt() {
  if (!selectedPrompt.value) return
  const blob = new Blob([selectedPrompt.value], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `fix-prompt-issue-${detail.value?.issue?.id ?? ''}.md`
  a.click()
  URL.revokeObjectURL(url)
}

// ============ 数据库连接配置（慢 SQL 分析 EXPLAIN 用） ============
const connOpen = ref(false)
const dbConns = ref<Array<Record<string, any>>>([])
const connForm = ref<{ connName: string; host: string; appName: string; dbUrl: string; username: string; password: string; isDefault: boolean }>({
  connName: '', host: '', appName: '', dbUrl: '', username: '', password: '', isDefault: false
})
const testing = ref(false)
const connTestResult = ref<{ ok: boolean; error?: string } | null>(null)
const connMsg = ref<{ ok: boolean; text: string } | null>(null)

async function loadConns() {
  const r = await dbConnApi.list()
  dbConns.value = r.data || []
}

async function saveConn() {
  const f = connForm.value
  // 校验缺失时明确提示，不再静默返回
  if (!f.connName) { connMsg.value = { ok: false, text: '请填写连接名称' }; return }
  if (!f.dbUrl) { connMsg.value = { ok: false, text: '请填写 JDBC URL（如 jdbc:mysql://127.0.0.1:3306/demo）' }; return }
  if (!f.username) { connMsg.value = { ok: false, text: '请填写用户名' }; return }
  try {
    await dbConnApi.save({
      connName: f.connName, host: f.host || undefined, appName: f.appName || undefined, dbUrl: f.dbUrl,
      username: f.username, password: f.password, isDefault: f.isDefault ? 1 : 0
    })
    connMsg.value = { ok: true, text: `连接「${f.connName}」已保存${f.appName ? `（已绑定项目 ${f.appName}，该项目的问题分析将自动使用此连接）` : ''}` }
    connForm.value = { connName: '', host: '', appName: '', dbUrl: '', username: '', password: '', isDefault: false }
    await loadConns()
  } catch (e: any) {
    connMsg.value = { ok: false, text: '保存失败：' + (e?.response?.data?.message || e?.message || '请求异常') }
  }
}

async function testConn() {
  const f = connForm.value
  if (!f.dbUrl || !f.username) {
    connTestResult.value = { ok: false, error: '请先填写 JDBC URL 与用户名' }
    return
  }
  testing.value = true
  connTestResult.value = null
  try {
    const r = await dbConnApi.test({ dbUrl: f.dbUrl, username: f.username, password: f.password })
    connTestResult.value = r.data
  } catch (e: any) {
    connTestResult.value = { ok: false, error: e?.response?.data?.message || e?.message || '请求失败' }
  } finally {
    testing.value = false
  }
}

async function makeDefault(c: Record<string, any>) {
  try {
    await dbConnApi.setDefault(c.id)
  } catch { /* 保持静默，刷新后状态为准 */ }
  await loadConns()
}

async function removeConn(c: Record<string, any>) {
  try {
    await dbConnApi.remove(c.id)
  } catch { /* 同上 */ }
  await loadConns()
}

async function createFromSlowSql() {
  if (!fastSlowSql.value || !currentRepo.value) return
  const r = await sourceApi.fromSlowSql({ slowSqlId: fastSlowSql.value, repoId: currentRepo.value.id })
  await showIssueDetail(r.data.id)
  await loadIssues()
}

async function createFromHot() {
  if (!currentRepo.value) return
  const r = await sourceApi.fromHotMethod({
    className: hotClass.value, methodName: hotMethod.value,
    repoId: currentRepo.value.id, appName: issueApp.value || undefined
  })
  await showIssueDetail(r.data.id)
  await loadIssues()
}

async function createFromStress() {
  if (!fastStress.value || !currentRepo.value) return
  // 应用优先级：项目绑定（强绑定）→ 页面筛选 → 不传（后端继承任务应用）
  const app = currentRepo.value.appName || issueApp.value || undefined
  const r = await sourceApi.fromStress({ stressTaskId: fastStress.value, repoId: currentRepo.value.id, appName: app })
  await showIssueDetail(r.data.id)
  await loadIssues()
}

onMounted(async () => {
  await Promise.allSettled([loadRepos(), loadIssues(), slowSqlApi.list({ limit: 10 }), stressApi.list({ limit: 10 }), loadConns()])
  const s = await sourceApi.recentSlowSqls({ limit: 10 })
  recentSlow.value = s.data
  const t = await stressApi.list({ limit: 10 })
  stressTasks.value = t.data
})
</script>
