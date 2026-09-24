<template>
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
          <div><label class="text-xs text-fg-sub">绑定项目（选后自动接管 EXPLAIN）</label>
            <select v-model="connForm.appName" class="input-select w-full" aria-label="绑定项目（选后自动接管 EXPLAIN）">
              <option value="">不绑定（按主机标识匹配）</option>
              <option v-for="name in projectNames" :key="name" :value="name">{{ name }}</option>
            </select></div>
          <div class="md:col-span-2"><label class="text-xs text-fg-sub">JDBC URL</label>
            <input v-model="connForm.dbUrl" class="input-text w-full" placeholder="jdbc:mysql://127.0.0.1:3306/demo" /></div>
          <div><label class="text-xs text-fg-sub">用户名</label>
            <input v-model="connForm.username" class="input-text w-full" placeholder="root" /></div>
          <div>
            <label class="text-xs text-fg-sub">密码</label>
            <!-- 凭据加密存储、列表不返回明文：留空表示沿用库中已保存凭据 -->
            <input v-model="connForm.password" type="password" class="input-text w-full" placeholder="留空=沿用已存凭据" />
          </div>
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
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { dbConnApi } from '../../api'

/**
 * 数据库连接配置面板（慢 SQL 分析 EXPLAIN 用）。
 *
 * <p>从 OptimizeView 拆出，行为与视觉保持不变。
 * 密码留空时沿用库中已保存凭据——凭据已加密存储，列表接口不返回明文。
 */
const props = defineProps<{ projectNames?: string[] }>()

const connOpen = ref(false)
const dbConns = ref<Array<Record<string, any>>>([])
const connForm = ref({
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

onMounted(loadConns)
// projectNames 由父组件提供（绑定项目下拉）；暴露刷新供外部联动
defineExpose({ loadConns, dbConns })
</script>
