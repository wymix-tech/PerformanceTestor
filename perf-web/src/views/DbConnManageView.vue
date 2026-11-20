<template>
  <div>
    <div class="page-title">
      <HardDrive class="w-5 h-5 text-neon-green" />
      数据库连接
      <span class="text-xs font-normal text-fg-sub ml-2">统一维护连接配置 · 慢 SQL EXPLAIN / 灌数 / 压测复用</span>
    </div>

    <!-- 连接表单（新建 / 编辑） -->
    <div class="glass-card p-5 mb-4">
      <div class="text-sm font-medium mb-4 flex items-center gap-2">
        {{ editingId ? `编辑连接「${connForm.connName}」` : '新建连接' }}
        <span class="text-[11px] font-normal text-fg-sub">
          {{ editingId ? '密码留空 = 沿用已保存凭据' : 'agent 上报的自动关联连接可在此处补齐凭据' }}
        </span>
        <div class="flex-1"></div>
        <button v-if="editingId" class="text-xs text-fg-sub hover:text-fg-main" @click="resetForm">取消编辑</button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-6 gap-3">
        <div><label class="text-xs text-fg-sub">名称 *</label>
          <input v-model="connForm.connName" class="input-text w-full" placeholder="perf-demo-主库" /></div>
        <div><label class="text-xs text-fg-sub">主机标识<span class="ml-1 font-normal">留空自动从 URL 推导</span></label>
          <input v-model="connForm.host" class="input-text w-full" placeholder="127.0.0.1:3306/demo" /></div>
        <div><label class="text-xs text-fg-sub">绑定项目</label>
          <select v-model="connForm.appName" class="input-select w-full">
            <option value="">不绑定（按主机标识匹配）</option>
            <option v-for="name in projectNames" :key="name" :value="name">{{ name }}</option>
          </select></div>
        <div class="md:col-span-2"><label class="text-xs text-fg-sub">JDBC URL *</label>
          <input v-model="connForm.dbUrl" class="input-text w-full" placeholder="jdbc:mysql://127.0.0.1:3306/demo" /></div>
        <div><label class="text-xs text-fg-sub">用户名 *</label>
          <input v-model="connForm.username" class="input-text w-full" placeholder="root" /></div>
        <div><label class="text-xs text-fg-sub">密码</label>
          <input v-model="connForm.password" type="password" class="input-text w-full"
                 placeholder="留空 = 沿用已存凭据" autocomplete="new-password" /></div>
      </div>
      <div class="flex items-center gap-2 mt-3">
        <div class="flex-1"></div>
        <button class="btn-primary text-xs !px-3 !py-1.5" @click="saveConn">保存连接</button>
        <button class="btn-primary text-xs !px-3 !py-1.5 bg-neon-blue/80" @click="testConn">
          {{ testing ? '测试中…' : '测试连接' }}
        </button>
      </div>
      <div v-if="testResult" class="text-[11px] mt-2" :class="testResult.ok ? 'text-neon-green' : 'text-red-400'">
        {{ testResult.ok ? '连接成功' : '连接失败：' + testResult.error }}
      </div>
      <div v-if="formMsg" class="text-[11px] mt-2" :class="formMsg.ok ? 'text-neon-green' : 'text-red-400'">{{ formMsg.text }}</div>
    </div>

    <!-- 连接列表 -->
    <div class="glass-card p-5">
      <div class="text-sm font-medium mb-3">连接列表<span class="text-xs font-normal text-fg-sub ml-2">共 {{ conns.length }} 条</span></div>
      <div v-if="!conns.length" class="text-xs text-fg-sub py-8 text-center">
        暂无连接配置，从上方表单新建（agent 上报慢 SQL 时也会自动注册空凭据连接）
      </div>
      <div v-else class="space-y-2">
        <div v-for="c in conns" :key="c.id"
          class="px-4 py-3 rounded-lg bg-white/5 flex items-center justify-between gap-3 flex-wrap">
          <div class="min-w-0">
            <div class="text-sm text-fg-main flex items-center gap-2 flex-wrap">
              {{ c.connName }}
              <span v-if="c.appName" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/15 text-neon-blue">项目：{{ c.appName }}</span>
              <span v-if="!c.username" class="text-[10px] px-1.5 py-0.5 rounded bg-amber-400/15 text-amber-300">待补凭据</span>
            </div>
            <div class="text-[11px] text-fg-sub mt-1 flex items-center gap-2 flex-wrap">
              <span class="font-mono">{{ c.dbUrl }}</span>
              <span v-if="c.host && c.host !== c.dbUrl" class="font-mono">· {{ c.host }}</span>
              <span v-if="c.username">· {{ c.username }}</span>
            </div>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <button class="text-xs text-neon-blue hover:underline" @click="editConn(c)">编辑</button>
            <button class="text-xs text-neon-blue hover:underline" @click="testOne(c)">
              {{ testingId === c.id ? '测试中…' : '测试' }}
            </button>
            <button class="text-xs text-red-400 hover:underline" @click="removeConn(c)">删除</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { HardDrive } from 'lucide-vue-next'
import { dbConnApi } from '../api'
import { useRepoStore } from '../stores/repoStore'

/**
 * 数据库连接管理页：统一维护 db_conn 连接配置。
 *
 * 连接来源：agent 上报慢 SQL 自动注册（空凭据，标「待补凭据」）或本页手填。
 * 消费方：慢 SQL EXPLAIN（按 host/绑定项目匹配）、灌数 connId 复用凭据。
 * 密码加密存储、接口不回显明文；编辑留空沿用已存凭据。
 */
const repoStore = useRepoStore()
const projectNames = computed(() => repoStore.repos.map((r: Record<string, any>) => r.repoName))

const conns = ref<Array<Record<string, any>>>([])
const editingId = ref<number | null>(null)
const testing = ref(false)
const testingId = ref<number>(0)
const testResult = ref<{ ok: boolean; error?: string } | null>(null)
const formMsg = ref<{ ok: boolean; text: string } | null>(null)

const emptyForm = () => ({ connName: '', host: '', appName: '', dbUrl: '', username: '', password: '' })
const connForm = ref(emptyForm())

async function loadConns() {
  const r = await dbConnApi.list()
  conns.value = r.data || []
}

function resetForm() {
  editingId.value = null
  connForm.value = emptyForm()
  testResult.value = null
  formMsg.value = null
}

async function saveConn() {
  const f = connForm.value
  if (!f.connName) { formMsg.value = { ok: false, text: '请填写连接名称' }; return }
  if (!f.dbUrl) { formMsg.value = { ok: false, text: '请填写 JDBC URL（如 jdbc:mysql://127.0.0.1:3306/demo）' }; return }
  if (!f.username) { formMsg.value = { ok: false, text: '请填写用户名' }; return }
  try {
    await dbConnApi.save({
      id: editingId.value ?? undefined,
      connName: f.connName, host: f.host || undefined, appName: f.appName || undefined, dbUrl: f.dbUrl,
      username: f.username, password: f.password
    })
    formMsg.value = {
      ok: true,
      text: editingId.value
        ? `连接「${f.connName}」已更新`
        : `连接「${f.connName}」已保存${f.appName ? `（已绑定项目 ${f.appName}，该项目的问题分析将自动使用此连接）` : ''}`
    }
    resetForm()
    await loadConns()
  } catch (e: any) {
    formMsg.value = { ok: false, text: '保存失败：' + (e?.response?.data?.message || e?.message || '请求异常') }
  }
}

function editConn(c: Record<string, any>) {
  editingId.value = c.id
  connForm.value = {
    connName: c.connName || '', host: c.host || '', appName: c.appName || '',
    dbUrl: c.dbUrl || '', username: c.username || '', password: ''
  }
  testResult.value = null
  formMsg.value = null
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function buildTestPayload(f: typeof connForm.value) {
  return { dbUrl: f.dbUrl, username: f.username, password: f.password }
}

async function testConn() {
  const f = connForm.value
  if (!f.dbUrl || !f.username) {
    testResult.value = { ok: false, error: '请先填写 JDBC URL 与用户名' }
    return
  }
  testing.value = true
  testResult.value = null
  try {
    const r = await dbConnApi.test(buildTestPayload(f))
    testResult.value = r.data
  } catch (e: any) {
    testResult.value = { ok: false, error: e?.response?.data?.message || e?.message || '请求失败' }
  } finally {
    testing.value = false
  }
}

/** 列表行快捷测试：密码留空沿用已存凭据（服务端解密后拨测） */
async function testOne(c: Record<string, any>) {
  testingId.value = c.id
  try {
    const r = await dbConnApi.test({ dbUrl: c.dbUrl, username: c.username, password: '' })
    const d = r.data || { ok: false, error: '无返回' }
    formMsg.value = { ok: !!d.ok, text: d.ok ? `连接「${c.connName}」测试成功` : `连接「${c.connName}」测试失败：${d.error}` }
  } catch (e: any) {
    formMsg.value = { ok: false, text: `连接「${c.connName}」测试失败：` + (e?.response?.data?.message || e?.message || '请求失败') }
  } finally {
    testingId.value = 0
  }
}

async function removeConn(c: Record<string, any>) {
  if (!window.confirm(`确认删除连接「${c.connName}」吗？`)) return
  try {
    await dbConnApi.remove(c.id)
    if (editingId.value === c.id) resetForm()
  } catch { /* 保持静默，刷新后状态为准 */ }
  await loadConns()
}

onMounted(async () => {
  await Promise.all([loadConns(), repoStore.load()])
})
</script>
