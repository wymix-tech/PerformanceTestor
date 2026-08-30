<template>
  <div>
    <div class="glass-card p-5 mb-5">
      <div class="text-sm font-medium mb-1">部署管理</div>
      <div class="text-[11px] text-fg-sub">
        维护被测服务器 SSH 连接：平台编译项目后经 SSH 上传 jar 与监控 agent 并远程启动。
        凭据仅服务端使用，不出现在 AI 上下文。
      </div>
    </div>

    <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <!-- 新增连接 -->
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4 flex items-center gap-2">
          <Plus class="w-4 h-4 text-neon-green" /> 新增 SSH 连接
        </div>
        <div class="space-y-3 text-xs">
          <div><label class="text-fg-sub">连接名称 *</label><input v-model="form.connName" class="input-text w-full" placeholder="如：压测机-A" /></div>
          <div class="grid grid-cols-3 gap-3">
            <div class="col-span-2"><label class="text-fg-sub">主机 IP *</label><input v-model="form.host" class="input-text w-full" placeholder="如：192.168.1.100" /></div>
            <div><label class="text-fg-sub">SSH 端口</label><input v-model.number="form.port" type="number" class="input-text w-full" placeholder="22" /></div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-fg-sub">用户名 *</label><input v-model="form.username" class="input-text w-full" placeholder="如：root" /></div>
            <div><label class="text-fg-sub">远程工作目录</label><input v-model="form.workDir" class="input-text w-full" placeholder="/opt/perftestor" /></div>
          </div>
          <div>
            <label class="text-fg-sub">认证方式</label>
            <select v-model="authType" class="input-select w-full">
              <option value="password">密码</option>
              <option value="privateKey">私钥（PEM 文本）</option>
            </select>
          </div>
          <div v-if="authType === 'password'">
            <label class="text-fg-sub">密码（密码 / 私钥至少一项）</label>
            <input v-model="form.password" type="password" class="input-text w-full" placeholder="••••••••" />
          </div>
          <div v-else>
            <label class="text-fg-sub">私钥内容（密码 / 私钥至少一项）</label>
            <textarea v-model="form.privateKey" class="input-text w-full font-mono" rows="4" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>
          </div>
          <div class="flex items-center gap-2 pt-1">
            <button class="btn-secondary" :disabled="testing" @click="doTest">
              {{ testing ? '测试中…' : '测试连接' }}
            </button>
            <button class="btn-primary" :disabled="saving" @click="doSave">
              {{ saving ? '保存中…' : '保存' }}
            </button>
            <span v-if="testMsg" class="text-[11px] truncate" :class="testOk ? 'text-neon-green' : 'text-red-400'">{{ testMsg }}</span>
            <span v-else-if="saveMsg" class="text-[11px] truncate" :class="saveOk ? 'text-neon-green' : 'text-red-400'">{{ saveMsg }}</span>
          </div>
        </div>
      </div>

      <!-- 连接列表 -->
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4 flex items-center gap-2">
          <Server class="w-4 h-4 text-neon-blue" /> 已保存的连接
          <span class="text-[11px] text-fg-sub font-normal">（{{ conns.length }}）</span>
        </div>
        <div v-if="!conns.length" class="text-xs text-fg-sub py-8 text-center">
          暂无连接配置 — 左侧添加后即可在「快速开始 → 部署」中选择
        </div>
        <div v-else class="space-y-2">
          <div v-for="c in conns" :key="c.id" class="flex items-center justify-between px-4 py-3 rounded-xl bg-white/5 hover:bg-white/10 transition-all">
            <div class="min-w-0">
              <div class="text-xs text-fg-main flex items-center gap-2">
                <span class="font-medium">{{ c.connName }}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-white/10 text-fg-sub">{{ c.authType === 'privateKey' ? '私钥' : '密码' }}</span>
              </div>
              <div class="text-[11px] text-fg-sub mt-0.5 font-mono truncate">
                {{ c.username }}@{{ c.host }}:{{ c.port || 22 }} · {{ c.workDir || '/opt/perftestor' }}
              </div>
            </div>
            <div class="flex items-center gap-1 shrink-0 ml-3">
              <button class="text-[11px] text-neon-blue hover:underline px-1.5" :disabled="testingId === c.id" @click="testSaved(c)">
                {{ testingId === c.id ? '测试中' : '测试' }}
              </button>
              <button class="text-[11px] text-red-400 hover:underline px-1.5" @click="doDelete(c)">删除</button>
            </div>
          </div>
          <div v-if="savedTestMsg" class="text-[11px] mt-1" :class="savedTestOk ? 'text-neon-green' : 'text-red-400'">{{ savedTestMsg }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { Plus, Server } from 'lucide-vue-next'
import { deployApi } from '../api'

const conns = ref<Array<Record<string, any>>>([])
const authType = ref<'password' | 'privateKey'>('password')
const form = reactive({
  connName: '',
  host: '',
  port: 22,
  username: 'root',
  workDir: '/opt/perftestor',
  password: '',
  privateKey: ''
})

const testing = ref(false)
const testingId = ref(0)
const saving = ref(false)
const testMsg = ref('')
const testOk = ref(false)
const saveMsg = ref('')
const saveOk = ref(false)
const savedTestMsg = ref('')
const savedTestOk = ref(false)

/** 表单 → 请求体（去掉空私钥/密码，避免误判认证方式） */
function payload(): Record<string, unknown> {
  const p: Record<string, unknown> = {
    connName: form.connName.trim(),
    host: form.host.trim(),
    port: form.port || 22,
    username: form.username.trim(),
    workDir: (form.workDir || '').trim() || '/opt/perftestor'
  }
  if (authType.value === 'privateKey') {
    if (form.privateKey.trim()) p.privateKey = form.privateKey
  } else if (form.password) {
    p.password = form.password
  }
  return p
}

function valid(): string | null {
  if (!form.connName.trim()) return '请填写连接名称'
  if (!form.host.trim()) return '请填写主机 IP'
  if (!form.username.trim()) return '请填写用户名'
  if (authType.value === 'password' && !form.password) return '请填写密码'
  if (authType.value === 'privateKey' && !form.privateKey.trim()) return '请填写私钥内容'
  return null
}

async function doTest() {
  const err = valid()
  if (err) { testOk.value = false; testMsg.value = err; return }
  testing.value = true
  testMsg.value = ''
  try {
    const r = await deployApi.testConn(payload())
    const d = r.data as Record<string, any>
    testOk.value = !!d?.success
    testMsg.value = testOk.value ? `连接成功：${String(d.remoteInfo || '').split('\n')[0]}` : `连接失败：${d?.error || '未知错误'}`
  } catch (e: any) {
    testOk.value = false
    testMsg.value = '连接失败：' + (e?.response?.data?.message || e?.message || '请求异常')
  } finally {
    testing.value = false
  }
}

async function doSave() {
  const err = valid()
  if (err) { saveOk.value = false; saveMsg.value = err; return }
  saving.value = true
  saveMsg.value = ''
  try {
    await deployApi.addConn(payload())
    saveOk.value = true
    saveMsg.value = `「${form.connName.trim()}」已保存`
    Object.assign(form, { connName: '', host: '', port: 22, username: 'root', workDir: '/opt/perftestor', password: '', privateKey: '' })
    await load()
  } catch (e: any) {
    saveOk.value = false
    saveMsg.value = '保存失败：' + (e?.response?.data?.message || e?.message || '请求异常')
  } finally {
    saving.value = false
  }
}

async function testSaved(c: Record<string, any>) {
  testingId.value = c.id
  savedTestMsg.value = ''
  try {
    // 按已保存连接 ID 测试，凭据不出服务端
    const r = await deployApi.testConnById(c.id)
    const d = r.data as Record<string, any>
    savedTestOk.value = !!d?.success
    savedTestMsg.value = `「${c.connName}」${savedTestOk.value ? '连接成功：' + String(d.remoteInfo || '').split('\n')[0] : '连接失败：' + (d?.error || '未知错误')}`
  } catch (e: any) {
    savedTestOk.value = false
    savedTestMsg.value = '测试失败：' + (e?.response?.data?.message || e?.message || '请求异常')
  } finally {
    testingId.value = 0
  }
}

async function doDelete(c: Record<string, any>) {
  if (!confirm(`删除连接「${c.connName}」？`)) return
  await deployApi.delConn(c.id)
  if (savedTestMsg.value) savedTestMsg.value = ''
  await load()
}

async function load() {
  const r = await deployApi.conns(50)
  conns.value = r.data || []
}

onMounted(load)
</script>
