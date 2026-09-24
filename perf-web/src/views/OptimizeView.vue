<template>
  <div>
    <div class="page-title">
      <Target class="w-5 h-5 text-neon-green" />
      性能优化
      <span class="text-xs font-normal text-fg-sub ml-2">问题关联 · 大模型多方案 · 修复提示词</span>
    </div>

    <!-- 页面级项目筛选（控制问题列表与一键创建） -->
    <div class="glass-card p-3 mb-4 flex items-center gap-3">
      <span class="text-sm font-medium">项目</span>
      <select v-model="issueApp" class="input-select w-44 text-xs" aria-label="项目">
        <option value="">全部项目（{{ issues.length }}）</option>
        <option v-for="a in issueApps" :key="a" :value="a">{{ a }}（{{ issues.filter(x => x.appName === a).length }}）</option>
      </select>
      <span class="text-[11px] text-fg-sub">筛选对问题列表与「一键创建」全局生效，创建的问题自动归属当前项目</span>
    </div>

    <!-- 数据库连接配置（慢 SQL 分析 EXPLAIN 用） -->
    <DbConnPanel ref="dbConnPanelRef" :project-names="projectNames" />

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
              <button
                v-if="detail.issue.status !== 'resolved'"
                class="text-xs !px-3 !py-1.5 rounded-lg border transition-all"
                :class="detail.issue.stressTaskId ? 'border-neon-green/40 text-neon-green hover:bg-neon-green/10' : 'border-white/15 text-fg-sub hover:border-white/30'"
                :disabled="verifying"
                :title="detail.issue.stressTaskId ? '重放场景快照压测并对比基线' : '该问题未关联压测场景快照，请先补绑'"
                @click="runVerify"
              >{{ verifying ? '验证中…' : '验证修复' }}</button>
              <button
                v-if="detail.suggestions?.length"
                class="text-xs !px-3 !py-1.5 rounded-lg border border-neon-blue/40 text-neon-blue hover:bg-neon-blue/10 transition-all"
                :disabled="applying"
                title="记录修复来源（分支/PR/补丁）并自动触发闭环验证"
                @click="openApplyFix"
              >{{ applying ? '回写中…' : '回写修复' }}</button>
              <span v-if="detail.issue.issueType === 'slow_sql'" class="text-[11px] text-fg-sub">
                EXPLAIN{{ issueConn ? `连接：${issueConn.conn.connName}（${issueConn.reason}）` : '：未配置连接，仅基于证据分析' }}
              </span>
            </div>
          </div>

          <!-- 压测问题前置：必须先关联源码仓库 -->
          <div v-if="needBindRepo" class="p-3 rounded-lg bg-amber-400/5 border border-amber-400/30 mb-4">
            <div class="text-xs text-amber-400 mb-2">⚠ 压测问题必须先关联项目源码才能分析（用于定位接口对应的 Controller 源码并生成修复方案）</div>
            <div class="flex gap-2">
              <select v-model="bindRepoId" class="input-select flex-1 text-xs" aria-label="选择项目源码作为分析目标">
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

          <!-- 修复效果验证：重放同场景压测 + 与快照基线对比（达标自动关闭问题） -->
          <div
            v-if="verifyResult"
            class="p-3 rounded-lg border mb-4"
            :class="verifyResult.passed ? 'bg-neon-green/5 border-neon-green/30' : 'bg-white/5 border-white/10'"
          >
            <div class="flex items-center justify-between mb-2">
              <div class="text-xs font-medium" :class="verifyResult.passed ? 'text-neon-green' : 'text-fg-main'">
                修复效果验证
                <span v-if="verifyResult.passed" class="ml-1 font-normal">· 达标已自动关闭</span>
                <span v-else-if="verifyResult.running" class="ml-1 font-normal text-neon-blue">· 重放中…</span>
                <span v-else-if="verifyResult.replayFailed" class="ml-1 font-normal text-red-400">· 重放失败</span>
                <span v-else-if="verifyResult.noSnapshot" class="ml-1 font-normal text-amber-400">· 无场景快照</span>
                <span v-else class="ml-1 font-normal text-amber-400">· 未达标</span>
              </div>
              <span class="text-[11px] text-fg-sub">{{ fmtTime(verifyResult.verifyTime) }}</span>
            </div>

            <div class="text-[11px] text-fg-sub leading-relaxed mb-3">{{ verifyResult.message }}</div>

            <!-- 前后指标概览（重放未完成时指标尚无意义，不展示） -->
            <div v-if="verifyResult.replayTaskId && !verifyResult.running" class="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3">
              <div class="p-2 rounded bg-black/20">
                <div class="text-[10px] text-fg-sub">P95 前</div>
                <div class="text-sm text-fg-main">{{ fmtNum(verifyResult.p95Before) }}<span class="text-[10px] text-fg-sub ml-0.5">ms</span></div>
              </div>
              <div class="p-2 rounded bg-black/20">
                <div class="text-[10px] text-fg-sub">P95 后</div>
                <div class="text-sm" :class="verifyResult.p95DropPct > 0 ? 'text-neon-green' : 'text-fg-main'">
                  {{ fmtNum(verifyResult.p95After) }}<span class="text-[10px] text-fg-sub ml-0.5">ms</span>
                </div>
              </div>
              <div class="p-2 rounded bg-black/20">
                <div class="text-[10px] text-fg-sub">P95 降幅</div>
                <div class="text-sm" :class="verifyResult.p95DropPct >= 30 ? 'text-neon-green' : 'text-amber-400'">
                  {{ fmtNum(verifyResult.p95DropPct) }}<span class="text-[10px] text-fg-sub ml-0.5">%</span>
                </div>
              </div>
              <div class="p-2 rounded bg-black/20">
                <div class="text-[10px] text-fg-sub">QPS</div>
                <div class="text-sm text-fg-main">{{ fmtNum(verifyResult.qpsBefore) }} → {{ fmtNum(verifyResult.qpsAfter) }}</div>
              </div>
            </div>

            <!-- 基线对比明细（复用基线对比表格语义） -->
            <table v-if="verifyResult.rows?.length" class="w-full text-[11px]" aria-label="修复验证重放结果（慢 SQL 与耗时）">
              <thead>
                <tr class="text-fg-sub">
                  <th class="text-left font-normal py-1">指标</th>
                  <th class="text-right font-normal py-1">基线</th>
                  <th class="text-right font-normal py-1">重放</th>
                  <th class="text-right font-normal py-1">变化</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, ri) in verifyResult.rows" :key="ri" class="border-t border-white/5">
                  <td class="py-1 text-fg-main">{{ row.label }}</td>
                  <td class="py-1 text-right text-fg-sub">{{ row.base }}</td>
                  <td class="py-1 text-right text-fg-main">{{ row.cur }}</td>
                  <td
                    class="py-1 text-right"
                    :class="row.regressed ? 'text-red-400' : row.improved ? 'text-neon-green' : 'text-fg-sub'"
                  >{{ row.deltaPct || '—' }}</td>
                </tr>
              </tbody>
            </table>

            <!-- 无场景快照时补绑入口 -->
            <div v-if="verifyResult.noSnapshot" class="mt-3 flex gap-2">
              <select v-model="bindStressId" class="input-select flex-1 text-xs" aria-label="选择压测任务作为验证基线">
                <option :value="0">— 选择压测任务作为验证基线 —</option>
                <option v-for="s in stressTasks" :key="s.id" :value="s.id">{{ s.taskName || s.targetUrl }}</option>
              </select>
              <button class="btn-primary text-xs !px-3 !py-1.5" :disabled="!bindStressId || bindingStress" @click="doBindStress">
                {{ bindingStress ? '绑定中…' : '补绑场景快照' }}
              </button>
            </div>
          </div>

          <!-- 修复回写（P2-2）：记录修复来源 + 自动触发闭环验证 -->
          <div v-if="applyPanelOpen" class="p-3 rounded-lg bg-neon-blue/5 border border-neon-blue/30 mb-4">
            <div class="text-xs font-medium text-neon-blue mb-2">回写修复来源（自动触发闭环验证）</div>
            <div class="grid grid-cols-1 md:grid-cols-4 gap-2 mb-2">
              <select v-model="applyType" class="input-select text-xs" aria-label="回写修复来源（自动触发闭环验证）">
                <option value="branch">分支名</option>
                <option value="pr">PR 链接</option>
                <option value="patch">补丁摘要</option>
              </select>
              <input
                v-model="applyRef"
                class="input-text text-xs md:col-span-2"
                :placeholder="applyRefPlaceholder"
              />
              <select v-model="applySuggestionId" class="input-select text-xs" aria-label="补丁摘要">
                <option :value="0">— 默认方案（已选定/首条）—</option>
                <option v-for="s in detail.suggestions" :key="s.id" :value="s.id">{{ s.title }}</option>
              </select>
            </div>
            <div class="flex items-center gap-2">
              <button class="btn-primary text-xs !px-3 !py-1.5" :disabled="applying || !applyRef.trim()" @click="submitApplyFix">
                {{ applying ? '回写中…' : '确认回写并验证' }}
              </button>
              <button class="text-xs text-fg-sub hover:text-fg-main" @click="applyPanelOpen = false">取消</button>
              <span class="text-[11px] text-fg-sub">同一来源重复提交不会重复发起重放压测（幂等）</span>
            </div>
            <div v-if="applyMsg" class="text-[11px] mt-2" :class="applyMsg.ok ? 'text-neon-green' : 'text-red-400'">{{ applyMsg.text }}</div>
          </div>

          <!-- 修复回写记录 -->
          <div v-if="appliedRecord" class="p-3 rounded-lg bg-white/5 border border-white/10 mb-4">
            <div class="text-xs font-medium mb-1 flex items-center justify-between">
              <span>已回写修复</span>
              <span class="text-[11px] text-fg-sub font-normal">{{ fmtTime(appliedRecord.appliedAt) }}</span>
            </div>
            <div class="text-[11px] text-fg-sub leading-relaxed">
              来源类型 {{ appliedRecord.applyType }} · {{ appliedRecord.applyRef }}
              <template v-if="appliedRecord.verifyTaskId"> · 验证任务 #{{ appliedRecord.verifyTaskId }}</template>
            </div>
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
          <select v-model="fastSlowSql" class="input-select w-full mb-2" aria-label="选择最近慢 SQL 创建问题">
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
          <select v-model="fastStress" class="input-select w-full mb-2" aria-label="压测任务 → URL 关联">
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
import { issueStatusClass as statusClass, fmtTime } from '../utils/format'
import DbConnPanel from '../components/optimize/DbConnPanel.vue'
import { sourceApi, slowSqlApi, stressApi } from '../api'
import { useRepoStore } from '../stores/repoStore'

const repos = ref<Array<Record<string, any>>>([])
const repoStore = useRepoStore()
const currentRepo = ref<Record<string, any> | null>(null)
/** 数据库连接面板引用：EXPLAIN 连接匹配需要其连接列表 */
const dbConnPanelRef = ref<InstanceType<typeof DbConnPanel> | null>(null)
// 项目名集合（仓库名），连接绑定的可选值
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

async function loadRepos() {
  await repoStore.load()
  repos.value = repoStore.repos
  currentRepo.value = repos.value[0] || null
}

async function loadIssues() {
  const r = await sourceApi.issues({ issueType: issueType.value || undefined, limit: 50 })
  issues.value = r.data
}

async function showIssueDetail(id: number) {
  const r = await sourceApi.issueDetail(id)
  detail.value = r.data
  // 切换问题时重置验证结论（结论属于单次操作结果，不跨问题保留）
  verifyResult.value = null
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

// ===== 修复效果自动验证 =====
// 一键验证是异步任务（需重放一次完整压测，数十秒到数分钟），
// 服务端发起后立即返回 running=true，前端轮询验证状态接口取最终结论
const VERIFY_POLL_INTERVAL_MS = 2000
// 轮询上限：后端 perf.verify.timeout-sec 默认 300s，留足余量后放弃
const VERIFY_POLL_MAX_TIMES = 200
const verifying = ref(false)
const verifyResult = ref<Record<string, any> | null>(null)

async function runVerify() {
  if (!detail.value || verifying.value) return
  verifying.value = true
  const issueId = detail.value.issue.id
  try {
    const r = (await sourceApi.verifyIssue(issueId)) as unknown as { data: Record<string, any> }
    let result = r.data
    if (result?.running) {
      result = await pollVerifyResult(issueId)
    }
    await showIssueDetail(issueId)
    // showIssueDetail 会清空结论，验证结果需在其后回填
    verifyResult.value = result
    await loadIssues()
  } finally {
    verifying.value = false
  }
}

/** 轮询验证结论直到脱离 running 态（期间持续更新面板，用户能看到重放任务编号与进度提示） */
async function pollVerifyResult(issueId: number): Promise<Record<string, any>> {
  for (let i = 0; i < VERIFY_POLL_MAX_TIMES; i++) {
    await new Promise(resolve => setTimeout(resolve, VERIFY_POLL_INTERVAL_MS))
    const r = (await sourceApi.verifyStatus(issueId)) as unknown as { data: Record<string, any> | null }
    if (r.data) {
      verifyResult.value = r.data
      if (!r.data.running) return r.data
    }
  }
  return { ...(verifyResult.value || {}), replayFailed: true, message: '验证等待超时，请在压测任务中确认重放结果后重试' }
}

// ===== 修复回写（P2-2）=====
const applyPanelOpen = ref(false)
const applying = ref(false)
const applyType = ref('branch')
const applyRef = ref('')
const applySuggestionId = ref(0)
const applyMsg = ref<{ ok: boolean; text: string } | null>(null)
/** 当前已回写记录（来自选定方案上的 appliedAt/applyRef） */
const appliedRecord = computed<Record<string, any> | null>(() => {
  const list = detail.value?.suggestions || []
  const hit = list.find((s: Record<string, any>) => s.appliedAt)
  if (!hit) return null
  return { appliedAt: hit.appliedAt, applyType: hit.applyType, applyRef: hit.applyRef, verifyTaskId: hit.verifyTaskId }
})
const applyRefPlaceholder = computed(() => {
  if (applyType.value === 'pr') return '如 https://git.example.com/group/repo/-/pulls/12'
  if (applyType.value === 'patch') return '如 补丁摘要：UserMapper 加复合索引 (status, create_time)'
  return '如 fix/user-list-n-plus-one'
})

function openApplyFix() {
  applyPanelOpen.value = true
  applyMsg.value = null
  applyRef.value = ''
  applySuggestionId.value = 0
}

async function submitApplyFix() {
  if (!detail.value || applying.value) return
  const ref = applyRef.value.trim()
  if (!ref) {
    applyMsg.value = { ok: false, text: '请填写修复来源标识' }
    return
  }
  applying.value = true
  applyMsg.value = null
  const issueId = detail.value.issue.id
  try {
    const r = (await sourceApi.applyFix(issueId, {
      applyType: applyType.value,
      applyRef: ref,
      suggestionId: applySuggestionId.value || undefined
    })) as unknown as { data: Record<string, any> }
    const d = r.data || {}
    applyMsg.value = { ok: true, text: d.idempotent ? d.message : (d.message || '修复已回写') }
    applyPanelOpen.value = false
    // 回写后自动验证为异步：轮询结论（与「验证修复」同一条链路）
    if (d.verify?.running) {
      verifyResult.value = d.verify
      await pollVerifyResult(issueId)
    } else if (d.verify) {
      verifyResult.value = { ...(verifyResult.value || {}), ...d.verify }
    }
    await showIssueDetail(issueId)
    // showIssueDetail 会清空结论，验证/回写结果需在其后回填
    if (d.verify) verifyResult.value = d.verify
  } catch (e: any) {
    applyMsg.value = { ok: false, text: '回写失败：' + (e?.response?.data?.message || e?.message || '请求异常') }
  } finally {
    applying.value = false
  }
}

// 补绑压测场景快照（无快照问题的验证前置）
const bindStressId = ref(0)
const bindingStress = ref(false)
async function doBindStress() {
  if (!detail.value || !bindStressId.value || bindingStress.value) return
  bindingStress.value = true
  try {
    await sourceApi.bindIssueStress(detail.value.issue.id, bindStressId.value)
    await showIssueDetail(detail.value.issue.id)
    await loadIssues()
  } finally {
    bindingStress.value = false
  }
}

/** 验证指标数值格式化（保留 1 位小数，整数不补零） */
function fmtNum(v: unknown): string {
  const n = Number(v)
  if (!isFinite(n)) return '—'
  return n === Math.floor(n) ? String(n) : n.toFixed(1)
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
  // 连接列表由 DbConnPanel 子组件持有并维护
  const conns = dbConnPanelRef.value?.dbConns ?? []
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
  // 应用优先级：项目绑定 → 页面筛选 → 不传（后端继承任务应用）
  const app = currentRepo.value.appName || issueApp.value || undefined
  const r = await sourceApi.fromStress({ stressTaskId: fastStress.value, repoId: currentRepo.value.id, appName: app })
  await showIssueDetail(r.data.id)
  await loadIssues()
}

onMounted(async () => {
  // 并行加载互不依赖的数据；单项失败不影响其余（下拉兜底为空）
  await Promise.allSettled([
    loadRepos(),
    loadIssues(),
    loadRecentSlowSqls(),
    loadStressTasksForIssue()
  ])
})

// 最近慢 SQL（一键创建问题用）
async function loadRecentSlowSqls() {
  const r = await sourceApi.recentSlowSqls({ limit: 10 })
  recentSlow.value = r.data || []
}

// 压测任务列表（从压测任务创建问题用）
async function loadStressTasksForIssue() {
  const r = await stressApi.list({ limit: 10 })
  stressTasks.value = r.data || []
}
</script>
