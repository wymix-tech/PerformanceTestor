<template>
  <div>
    <div class="page-title">
      <FolderGit class="w-5 h-5 text-neon-green" />
      项目管理
      <span class="text-xs font-normal text-fg-sub ml-2">项目即应用 · 源码采集 · 分支与同步管理</span>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">导入项目源码</div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">项目 / 应用名<span class="ml-2 font-normal">项目即应用：agent 上报的 appName 与此一致，压测/问题/分析自动归属</span></label>
            <input v-model="importForm.repoName" class="input-text w-full" placeholder="如 perf-demo（同时也是应用名）" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">zip 上传（≤200MB，防路径穿越）</label>
            <input type="file" accept=".zip" class="input-text w-full file-input" @change="onFile" />
            <button class="btn-primary w-full justify-center mt-2" :disabled="!zipFile" @click="uploadZip">上传导入</button>
          </div>
          <div class="border-t border-white/5 pt-3">
            <label class="text-xs text-fg-sub">Git 地址导入<span class="ml-2 font-normal">私有仓库填写凭据（令牌/密码仅存储于平台服务器，不回显）</span></label>
            <div class="flex gap-2 mt-1">
              <input v-model="gitUrl" class="input-text flex-1" placeholder="https://git.xxx.com/group/repo.git" @change="fetchRemoteBranches" />
              <select v-model="gitBranch" class="input-select w-36" :disabled="!gitUrl || remoteBranchLoading">
                <option value="">{{ remoteBranchLoading ? '分支获取中…' : remoteBranches.length ? '选择分支（可选）' : '默认分支' }}</option>
                <option v-for="b in remoteBranches" :key="b" :value="b">{{ b }}</option>
              </select>
              <button class="btn-primary" :disabled="remoteBranchLoading || !gitUrl" title="重新获取分支列表" @click="fetchRemoteBranches">↻</button>
            </div>
            <div class="flex gap-2 mt-2">
              <input v-model="gitUser" class="input-text flex-1" placeholder="用户名（私有仓库，可空）" />
              <input v-model="gitToken" type="password" class="input-text flex-1" placeholder="访问令牌 / 密码（公有仓库留空）" autocomplete="new-password" />
              <button class="btn-primary" :disabled="gitImporting" @click="importGit">
                {{ gitImporting ? '提交中…' : '导入' }}
              </button>
            </div>
            <div v-if="gitMsg" class="text-[11px] mt-2" :class="gitMsg.ok ? 'text-neon-green' : 'text-red-400'">{{ gitMsg.text }}</div>
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
        <div class="text-sm font-medium mb-3">项目列表</div>
        <div v-if="!repos.length" class="text-xs text-fg-sub py-8 text-center">暂无项目，从左侧导入</div>
        <div v-else class="space-y-2">
          <div v-for="r in repos" :key="r.id" class="px-4 py-3 rounded-lg bg-white/5 flex items-center justify-between">
            <div>
              <div class="text-sm text-fg-main flex items-center gap-2">
                {{ r.repoName }}
                <template v-if="r.repoType === 'git'">
                  <select
                    :key="(branchMap[r.id] || []).length + '-' + (r.branch || '')"
                    class="input-select !w-36 !py-0.5 text-[11px]"
                    :value="r.branch || ''"
                    @focus="loadBranches(r)"
                    @change="switchBranch(r, ($event.target as HTMLSelectElement).value)"
                  >
                    <!-- 已加载远端列表：渲染列表；当前分支不在列表中（本地未推送）时额外补一项 -->
                    <template v-if="(branchMap[r.id] || []).length">
                      <option v-for="b in branchMap[r.id]" :key="b" :value="b">{{ b }}</option>
                      <option v-if="r.branch && !branchMap[r.id].includes(r.branch)" :value="r.branch">{{ r.branch }}（当前）</option>
                    </template>
                    <!-- 未加载：显示当前分支（进入页面即自动拉取列表） -->
                    <option v-else value="">{{ branchLoadingId === r.id ? '分支加载中…' : (r.branch || '分支加载中…') }}</option>
                  </select>
                  <button class="text-neon-blue hover:underline text-[11px]" :disabled="branchLoadingId === r.id" @click="loadBranches(r)">
                    {{ branchLoadingId === r.id ? '同步中…' : '↻ 刷新分支' }}
                  </button>
                </template>
              </div>
              <div class="text-[11px] text-fg-sub mt-1 flex items-center gap-2">
                <span>{{ r.repoType }} · {{ r.fileCount }} 文件 · {{ formatSize(r.totalSize) }}</span>
                <!-- 项目即应用：展示归属（= 仓库名或导入时指定的应用名） -->
                <span v-if="r.appName" class="px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue">{{ r.appName }}</span>
              </div>
              <div v-if="r.status === 'failed' && r.errorMsg" class="text-[11px] text-red-400 mt-1 break-all max-w-md">{{ r.errorMsg }}</div>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-[11px] px-2 py-0.5 rounded-full flex items-center gap-1" :class="statusClass(r.status)">
                <span v-if="r.status === 'importing'" class="inline-block w-2 h-2 rounded-full bg-neon-blue animate-pulse"></span>
                {{ r.status === 'importing' ? '拉取中' : r.status }}
              </span>
              <button class="text-xs text-neon-blue hover:underline" :disabled="resyncingId === r.id || r.status === 'importing'" @click="resyncRepo(r)">
                {{ resyncingId === r.id ? '同步中…' : '↻ 重新同步' }}
              </button>
              <button class="text-xs text-neon-blue hover:underline" @click="showFiles(r)">文件</button>
            </div>
          </div>
        </div>

        <div v-if="fileList.length" class="mt-4">
          <div class="text-xs text-fg-sub mb-2">项目「{{ currentRepo?.repoName }}」文件（{{ fileList.length }}）</div>
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
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { FolderGit } from 'lucide-vue-next'
import { sourceApi } from '../api'

const importForm = ref<{ repoName: string; appName: string }>({ repoName: '', appName: '' })
const zipFile = ref<File | null>(null)
const gitUrl = ref('')
const gitBranch = ref('')
const gitUser = ref('')
const gitToken = ref('')
// 远端分支（导入前 ls-remote 枚举，填完地址自动拉取）
const remoteBranches = ref<string[]>([])
const remoteBranchLoading = ref(false)
async function fetchRemoteBranches() {
  const url = gitUrl.value.trim()
  remoteBranches.value = []
  gitBranch.value = ''
  if (!url) return
  remoteBranchLoading.value = true
  try {
    const r = await sourceApi.remoteBranches(url, gitUser.value || undefined, gitToken.value || undefined)
    remoteBranches.value = r.data || []
  } catch { /* 拉取失败：下拉显示"默认分支"，导入时仍可用默认分支 */ }
  finally {
    remoteBranchLoading.value = false
  }
}
const localPath = ref('')
const repos = ref<Array<Record<string, any>>>([])
const fileList = ref<Array<Record<string, any>>>([])
const currentRepo = ref<Record<string, any> | null>(null)
const currentFile = ref<Record<string, any> | null>(null)
const fileContent = ref<string | null>(null)

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
    failed: 'text-red-400 bg-red-400/10'
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
  await sourceApi.importZip(
    importForm.value.repoName || zipFile.value.name.replace('.zip', ''),
    zipFile.value
  )
  zipFile.value = null
  await loadRepos()
}

// git 导入：提交后异步拉取源码（importing → ready/failed），全程状态反馈 + 自动轮询
const gitImporting = ref(false)
const gitMsg = ref<{ ok: boolean; text: string } | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null

async function importGit() {
  if (!gitUrl.value.trim()) {
    gitMsg.value = { ok: false, text: '请填写 Git 地址' }
    return
  }
  if (gitImporting.value) return
  gitImporting.value = true
  gitMsg.value = { ok: true, text: '正在提交导入请求…' }
  try {
    await sourceApi.importGit({
      repoName: importForm.value.repoName || undefined,
      gitUrl: gitUrl.value.trim(),
      branch: gitBranch.value || undefined,
      gitUser: gitUser.value || undefined,
      gitToken: gitToken.value || undefined
    })
    gitUrl.value = ''
    gitBranch.value = ''
    remoteBranches.value = []
    gitMsg.value = { ok: true, text: '导入请求已提交，正在拉取源码并建立索引（下方列表可见进度）…' }
    await loadRepos()
    startPolling()
  } catch (e: any) {
    gitMsg.value = { ok: false, text: '导入失败：' + (e?.response?.data?.message || e?.message || '请求异常（请确认平台服务正常）') }
  } finally {
    gitImporting.value = false
  }
}

/** 轮询 importing 状态的仓库直到 ready/failed（2s 间隔，最长 120s） */
function startPolling() {
  if (pollTimer) clearTimeout(pollTimer)
  let elapsed = 0
  const tick = async () => {
    elapsed += 2
    await loadRepos()
    if (repos.value.some((r) => r.status === 'importing') && elapsed < 120) {
      pollTimer = setTimeout(tick, 2000)
    }
  }
  pollTimer = setTimeout(tick, 2000)
}

async function importLocal() {
  await sourceApi.importLocal({
    repoName: importForm.value.repoName || undefined,
    localPath: localPath.value
  })
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

// 仓库重新同步：重新拉取源码并重建索引（本地/git 源更新后点击），完成后刷新仓库列表
const resyncingId = ref<number>(0)
async function resyncRepo(r: Record<string, any>) {
  if (resyncingId.value) return
  resyncingId.value = r.id
  try {
    await sourceApi.resync(r.id)
    await loadRepos()
  } finally {
    resyncingId.value = 0
  }
}

// git 分支：按仓库缓存列表；下拉聚焦时从远端同步；切换即重同步
const branchMap = ref<Record<number, string[]>>({})
const branchLoadingId = ref<number>(0)
async function loadBranches(r: Record<string, any>) {
  if (branchLoadingId.value) return
  branchLoadingId.value = r.id
  try {
    const res = await sourceApi.branches(r.id)
    branchMap.value = { ...branchMap.value, [r.id]: res.data || [] }
  } catch {
    branchMap.value = { ...branchMap.value, [r.id]: [r.branch].filter(Boolean) }
  } finally {
    branchLoadingId.value = 0
  }
}

async function switchBranch(r: Record<string, any>, branch: string) {
  if (!branch || branch === r.branch) return
  resyncingId.value = r.id
  try {
    await sourceApi.switchBranch(r.id, branch)
    await loadRepos()
  } finally {
    resyncingId.value = 0
  }
}

onMounted(async () => {
  await loadRepos()
  // 页面打开时若仍有导入中的仓库，继续轮询
  if (repos.value.some((r) => r.status === 'importing')) startPolling()
  // git 仓库自动同步分支列表（进页面即展示，无需点击）
  for (const r of repos.value) {
    if (r.repoType === 'git') await loadBranches(r)
  }
})
</script>
