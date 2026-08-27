<template>
  <div>
    <div class="page-title">
      <Code class="w-5 h-5 text-neon-green" />
      源码与优化
      <span class="text-xs font-normal text-fg-sub ml-2">源码采集 · 问题关联 · 大模型多方案 · 修复提示词</span>
    </div>

    <div class="flex gap-1 mb-4">
      <button
        v-for="tab in tabs" :key="tab.key"
        class="px-5 py-2 rounded-lg text-sm transition-all"
        :class="activeTab === tab.key ? 'bg-neon-green/15 text-neon-green border border-neon-green/30' : 'text-fg-sub border border-white/10 hover:text-fg-main'"
        @click="activeTab = tab.key"
      >{{ tab.label }}</button>
    </div>

    <!-- 仓库管理 -->
    <div v-if="activeTab === 'repo'" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">导入源码仓库</div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">仓库名</label>
            <input v-model="importForm.repoName" class="input-text w-full" placeholder="如 perf-demo" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">zip 上传（≤200MB，防路径穿越）</label>
            <input type="file" accept=".zip" class="input-text w-full file-input" @change="onFile" />
            <button class="btn-primary w-full justify-center mt-2" :disabled="!zipFile" @click="uploadZip">上传导入</button>
          </div>
          <div class="border-t border-white/5 pt-3">
            <label class="text-xs text-fg-sub">Git 地址导入</label>
            <div class="flex gap-2 mt-1">
              <input v-model="gitUrl" class="input-text flex-1" placeholder="https://git.xxx.com/group/repo.git" />
              <button class="btn-primary" @click="importGit">导入</button>
            </div>
          </div>
          <div class="border-t border-white/5 pt-3">
            <label class="text-xs text-fg-sub">本地目录导入（平台服务器路径）</label>
            <div class="flex gap-2 mt-1">
              <input v-model="localPath" class="input-text flex-1" placeholder="/data/src/demo" />
              <button class="btn-primary" @click="importLocal">导入</button>
            </div>
          </div>
        </div>
      </div>

      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">仓库列表</div>
        <div v-if="!repos.length" class="text-xs text-fg-sub py-8 text-center">暂无仓库</div>
        <div v-else class="space-y-2">
          <div v-for="r in repos" :key="r.id" class="px-4 py-3 rounded-lg bg-white/5 flex items-center justify-between">
            <div>
              <div class="text-sm text-fg-main">{{ r.repoName }}</div>
              <div class="text-[11px] text-fg-sub mt-1">{{ r.repoType }} · {{ r.fileCount }} 文件 · {{ formatSize(r.totalSize) }}</div>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(r.status)">{{ r.status }}</span>
              <button class="text-xs text-neon-blue hover:underline" @click="showFiles(r)">文件</button>
            </div>
          </div>
        </div>

        <div v-if="fileList.length" class="mt-4">
          <div class="text-xs text-fg-sub mb-2">仓库 #{{ currentRepo?.id }} 文件（{{ fileList.length }}）</div>
          <div class="max-h-[300px] overflow-y-auto space-y-1">
            <div v-for="f in fileList" :key="f.id" class="flex items-center justify-between px-3 py-1.5 rounded bg-white/5 text-xs">
              <span class="text-fg-main truncate font-mono">{{ f.relativePath }}</span>
              <button class="text-neon-blue hover:underline shrink-0" @click="viewFile(f)">查看</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 文件内容查看 -->
    <div v-if="fileContent !== null" class="glass-card p-5 mt-4">
      <div class="text-sm font-medium mb-3 flex items-center justify-between">
        <span class="font-mono">{{ currentFile?.relativePath }}</span>
        <button class="text-xs text-fg-sub hover:text-fg-main" @click="fileContent = null">关闭</button>
      </div>
      <pre class="text-xs text-fg-main leading-relaxed bg-black/30 rounded-lg p-4 overflow-x-auto max-h-[420px] font-mono"><code>{{ fileContent }}</code></pre>
    </div>

    <!-- 问题列表 -->
    <div v-if="activeTab === 'issue'" class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-4">
        <div class="text-sm font-medium mb-3">性能问题（{{ issues.length }}）</div>
        <div class="flex flex-wrap gap-2 mb-3">
          <button
            v-for="t in issueTypes" :key="t"
            class="px-3 py-1 rounded-full text-[11px] border transition-all"
            :class="issueType === t ? 'bg-neon-green/15 text-neon-green border-neon-green/30' : 'text-fg-sub border-white/10'"
            @click="issueType = t; loadIssues()"
          >{{ t }}</button>
        </div>
        <div v-if="!issues.length" class="text-xs text-fg-sub py-8 text-center">暂无问题，可从下方检测结果一键创建</div>
        <div v-else class="space-y-2 max-h-[560px] overflow-y-auto pr-1">
          <div v-for="i in issues" :key="i.id" class="px-3 py-2.5 rounded-lg cursor-pointer border transition-all"
            :class="detail?.issue?.id === i.id ? 'bg-neon-green/10 border-neon-green/40' : 'bg-white/5 border-white/10 hover:border-neon-green/30'"
            @click="showIssueDetail(i.id)">
            <div class="text-xs text-fg-main truncate">{{ i.title }}</div>
            <div class="flex justify-between mt-1 text-[11px] text-fg-sub">
              <span>{{ i.issueType }} · {{ i.severity }}</span>
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
            <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(detail.issue.status)">{{ detail.issue.status }}</span>
          </div>
          <div class="text-xs text-fg-sub mb-4">{{ detail.issue.issueType }} · 严重度 {{ detail.issue.severity }}
            <span v-if="detail.issue.sourceFile"> · {{ detail.issue.sourceFile.relativePath }}:{{ detail.issue.lineStart }}-{{ detail.issue.lineEnd }}</span>
          </div>

          <div v-if="detail.issue.rootCause" class="p-3 rounded-lg bg-white/5 mb-4">
            <div class="text-xs text-fg-sub mb-1">根因分析</div>
            <div class="text-xs text-fg-main leading-relaxed">{{ detail.issue.rootCause }}</div>
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
                  <span v-if="s.selected" class="text-[11px] text-neon-green">已选定</span>
                </div>
                <div class="text-[11px] text-fg-sub mt-2 leading-relaxed">{{ s.benefit }}</div>
                <div v-if="s.filesJson" class="text-[11px] text-neon-blue mt-2">涉及：{{ (typeof s.filesJson === 'string' ? JSON.parse(s.filesJson) : s.filesJson).join(', ') }}</div>
                <button v-if="!s.selected" class="btn-primary mt-3 text-xs !px-3 !py-1.5">选定并生成提示词</button>
              </div>
            </div>
          </div>

          <div v-if="detail.issue.promptMd" class="p-4 rounded-lg bg-black/40 border border-neon-green/20">
            <div class="flex items-center justify-between mb-2">
              <div class="text-sm font-medium text-neon-green">修复提示词</div>
              <button class="text-xs text-neon-blue hover:underline" @click="copyPrompt">复制</button>
            </div>
            <pre class="text-[11px] text-fg-main whitespace-pre-wrap leading-relaxed max-h-[380px] overflow-y-auto font-mono">{{ detail.issue.promptMd }}</pre>
          </div>
          <div v-else-if="detail.suggestions?.length" class="text-xs text-fg-sub py-4 text-center">选择方案后生成修复提示词</div>
        </template>
      </div>
    </div>

    <!-- 快速创建 -->
    <div v-if="activeTab === 'issue'" class="glass-card p-5 mt-4">
      <div class="text-sm font-medium mb-3">从检测结果一键创建问题（自动关联源码位置）</div>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div class="p-3 rounded-lg bg-white/5">
          <div class="text-xs text-fg-sub mb-2">慢 SQL → Mapper 定位</div>
          <select v-model="fastSlowSql" class="input-select w-full mb-2">
            <option :value="0">— 最近慢 SQL —</option>
            <option v-for="s in recentSlow" :key="s.id" :value="s.id">{{ s.sqlText?.slice(0, 40) }}（{{ Number(s.costMs).toFixed(0) }}ms）</option>
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
import { onMounted, ref } from 'vue'
import { Code } from 'lucide-vue-next'
import { sourceApi, slowSqlApi, stressApi } from '../api'

const tabs = [
  { key: 'repo', label: '源码仓库' },
  { key: 'issue', label: '问题与优化' }
]
const activeTab = ref('repo')
const importForm = ref<{ repoName: string }>({ repoName: '' })
const zipFile = ref<File | null>(null)
const gitUrl = ref('')
const localPath = ref('')
const repos = ref<Array<Record<string, any>>>([])
const fileList = ref<Array<Record<string, any>>>([])
const currentRepo = ref<Record<string, any> | null>(null)
const currentFile = ref<Record<string, any> | null>(null)
const fileContent = ref<string | null>(null)

const issues = ref<Array<Record<string, any>>>([])
const detail = ref<Record<string, any> | null>(null)
const issueType = ref('')
const issueTypes = ['', 'slow_sql', 'hot_method', 'stress_bottleneck']
const recentSlow = ref<Array<Record<string, any>>>([])
const stressTasks = ref<Array<Record<string, any>>>([])
const fastSlowSql = ref(0)
const fastStress = ref(0)
const hotClass = ref('')
const hotMethod = ref('')

function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  zipFile.value = input.files?.[0] || null
}

function formatSize(n: number) {
  if (!n) return '0 B'
  return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`
}

function statusClass(s: string) {
  const map: Record<string, string> = {
    ready: 'text-neon-green bg-neon-green/10',
    indexing: 'text-neon-blue bg-neon-blue/10',
    failed: 'text-red-400 bg-red-400/10',
    analyzing: 'text-neon-blue bg-neon-blue/10',
    analyzed: 'text-neon-green bg-neon-green/10',
    open: 'text-amber-400 bg-amber-400/10'
  }
  return map[s] || 'text-fg-sub bg-white/5'
}

async function loadRepos() {
  const r = await sourceApi.repos()
  repos.value = r.data
  currentRepo.value = repos.value[0] || null
}

async function uploadZip() {
  if (!zipFile.value) return
  await sourceApi.importZip(importForm.value.repoName || zipFile.value.name.replace('.zip', ''), zipFile.value)
  zipFile.value = null
  await loadRepos()
}

async function importGit() {
  await sourceApi.importGit({ repoName: importForm.value.repoName || undefined, gitUrl: gitUrl.value })
  gitUrl.value = ''
  await loadRepos()
}

async function importLocal() {
  await sourceApi.importLocal({ repoName: importForm.value.repoName || undefined, localPath: localPath.value })
  localPath.value = ''
  await loadRepos()
}

async function showFiles(r: Record<string, any>) {
  currentRepo.value = r
  const res = await sourceApi.files(r.id, { limit: 200 })
  fileList.value = res.data
}

async function viewFile(f: Record<string, any>) {
  currentFile.value = f
  const res = await sourceApi.fileContent(f.id)
  fileContent.value = res.data
}

async function loadIssues() {
  const r = await sourceApi.issues({ issueType: issueType.value || undefined, limit: 50 })
  issues.value = r.data
}

async function showIssueDetail(id: number) {
  const r = await sourceApi.issueDetail(id)
  detail.value = r.data
}

async function selectSuggestion(s: Record<string, any>) {
  if (!detail.value) return
  await sourceApi.selectSuggestion(detail.value.issue.id, s.id)
  await showIssueDetail(detail.value.issue.id)
}

async function copyPrompt() {
  if (!detail.value?.issue?.promptMd) return
  await navigator.clipboard.writeText(detail.value.issue.promptMd)
}

async function createFromSlowSql() {
  if (!fastSlowSql.value || !currentRepo.value) return
  const r = await sourceApi.fromSlowSql({ slowSqlId: fastSlowSql.value, repoId: currentRepo.value.id })
  await showIssueDetail(r.data.id)
  activeTab.value = 'issue'
  await loadIssues()
}

async function createFromHot() {
  if (!currentRepo.value) return
  const r = await sourceApi.fromHotMethod({ className: hotClass.value, methodName: hotMethod.value, repoId: currentRepo.value.id })
  await showIssueDetail(r.data.id)
  activeTab.value = 'issue'
  await loadIssues()
}

async function createFromStress() {
  if (!fastStress.value || !currentRepo.value) return
  const r = await sourceApi.fromStress({ stressTaskId: fastStress.value, repoId: currentRepo.value.id })
  await showIssueDetail(r.data.id)
  activeTab.value = 'issue'
  await loadIssues()
}

onMounted(async () => {
  await Promise.allSettled([loadRepos(), loadIssues(), slowSqlApi.list({ limit: 10 }), stressApi.list({ limit: 10 })])
  const s = await sourceApi.recentSlowSqls({ limit: 10 })
  recentSlow.value = s.data
  const t = await stressApi.list({ limit: 10 })
  stressTasks.value = t.data
})
</script>
