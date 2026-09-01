<template>
  <div class="glass-card p-5">
    <div class="text-sm font-medium mb-1">部署（可选） <span class="text-neon-green text-xs ml-1">项目：{{ repoName }}</span></div>
    <div class="text-[11px] text-fg-sub mb-3">被测应用需挂载监控 agent 才能采集调用链与 JVM 指标。已在运行（且已挂 agent）则直接跳过此步。</div>

    <!-- 方式切换 -->
    <div class="flex gap-2 mb-4">
      <button class="px-3 py-1.5 rounded-lg text-xs font-medium border transition-all"
              :class="deployMode === 'auto' ? 'border-neon-green bg-neon-green/15 text-neon-green' : 'border-white/10 text-fg-sub hover:border-white/30'"
              @click="deployMode = 'auto'">自动部署（平台编译 + SSH 上传启动）</button>
      <button class="px-3 py-1.5 rounded-lg text-xs font-medium border transition-all"
              :class="deployMode === 'manual' ? 'border-neon-green bg-neon-green/15 text-neon-green' : 'border-white/10 text-fg-sub hover:border-white/30'"
              @click="deployMode = 'manual'">手动部署（生成启动命令）</button>
    </div>

    <!-- 自动部署 -->
    <div v-if="deployMode === 'auto'" class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div class="space-y-3">
        <div>
          <label class="text-xs text-fg-sub">SSH 连接（被测服务器）</label>
          <select v-model="deployConnId" class="input-select w-full">
            <option :value="0">— 跳过部署，应用已在运行 —</option>
            <option v-for="c in deployConns" :key="c.id" :value="c.id">{{ c.connName }}（{{ c.host }}）</option>
          </select>
          <div class="flex items-center justify-between mt-1">
            <button class="text-[11px] text-neon-blue hover:underline" @click="connFormOpen = !connFormOpen">
              {{ connFormOpen ? '− 收起新增' : '+ 手动输入服务器信息新增连接' }}
            </button>
            <router-link class="text-[11px] text-fg-sub hover:text-neon-blue" to="/deploy">部署管理</router-link>
          </div>
          <div v-if="!deployConns.length" class="text-[11px] text-amber-400 mt-1">暂无 SSH 连接配置：可在下方手动输入 IP/端口/用户名/密码，或先跳过改用手动部署</div>
        </div>
        <!-- 内联新增连接（手动输入 IP/端口/用户名/密码，保存后自动选中） -->
        <div v-if="connFormOpen" class="rounded-xl border border-white/10 bg-white/5 p-3 space-y-2">
          <div class="text-[11px] text-fg-sub font-medium">新增 SSH 连接</div>
          <div class="grid grid-cols-2 gap-2">
            <input v-model="connForm.connName" class="input-text w-full text-xs" placeholder="连接名称，如：压测机-A" />
            <input v-model="connForm.host" class="input-text w-full text-xs" placeholder="主机 IP，如：192.168.1.100" />
            <input v-model.number="connForm.port" type="number" class="input-text w-full text-xs" placeholder="SSH 端口（默认 22）" />
            <input v-model="connForm.username" class="input-text w-full text-xs" placeholder="用户名（默认 root）" />
          </div>
          <input v-model="connForm.password" type="password" class="input-text w-full text-xs" placeholder="密码" />
          <div class="flex items-center gap-2">
            <button class="btn-secondary text-xs px-3 py-1.5" :disabled="connTesting" @click="testNewConn">
              {{ connTesting ? '测试中…' : '测试连接' }}
            </button>
            <button class="btn-primary text-xs px-3 py-1.5" :disabled="connSaving" @click="saveNewConn">
              {{ connSaving ? '保存中…' : '保存并使用' }}
            </button>
            <span v-if="connMsg" class="text-[11px] truncate" :class="connMsgOk ? 'text-neon-green' : 'text-red-400'">{{ connMsg }}</span>
          </div>
        </div>
        <template v-if="deployConnId">
          <div><label class="text-xs text-fg-sub">Maven 模块（可空=整仓）</label><input v-model="deployModule" class="input-text w-full text-xs" placeholder="如 perf-demo" /></div>
          <div><label class="text-xs text-fg-sub">应用端口（启动后健康检查）</label><input v-model.number="deployPort" type="number" class="input-text w-full text-xs" /></div>
          <button class="btn-primary w-full justify-center" :disabled="deploying" @click="runDeploy">
            {{ deploying ? '编译部署中…（约 1-2 分钟）' : '编译并部署启动' }}
          </button>
        </template>
      </div>
      <div class="rounded-xl bg-white/5 p-4 text-xs space-y-2">
        <div class="text-fg-sub font-medium">部署状态</div>
        <div v-if="!buildResult && !deployResult" class="text-fg-sub">未执行（跳过则直接压测本地已运行的应用）</div>
        <template v-else>
          <div v-if="buildResult">
            <span :class="buildResult.success ? 'text-neon-green' : 'text-red-400'">{{ buildResult.success ? '✓ 编译成功' : '✗ 编译失败' }}</span>
            <div class="text-[11px] text-fg-sub mt-1 font-mono break-all">{{ buildResult.jarPath || buildResult.logTail }}</div>
          </div>
          <div v-if="deployResult">
            <span :class="deployResult.success ? 'text-neon-green' : 'text-red-400'">{{ deployResult.success ? '✓ 已启动' : '✗ 启动失败' }}</span>
            <span class="text-fg-sub ml-2">PID {{ deployResult.pid }} · 健康检查 HTTP {{ deployResult.healthCode }} · {{ deployResult.processStatus }}</span>
          </div>
          <div v-if="deployLog" class="text-[11px] text-fg-sub whitespace-pre-wrap font-mono max-h-[160px] overflow-y-auto">{{ deployLog }}</div>
        </template>
      </div>
    </div>

    <!-- 手动部署：生成带 agent 的启动命令 -->
    <div v-else class="space-y-3">
      <div class="rounded-xl border border-neon-blue/30 bg-neon-blue/5 p-4">
        <div class="flex items-center justify-between mb-2">
          <div class="text-xs font-medium text-fg-main">① 上传 agent 包到被测服务器</div>
          <button class="text-[11px] text-neon-blue hover:underline" @click="copyText(manualScpCmd)">复制</button>
        </div>
        <pre class="text-[11px] text-fg-sub font-mono whitespace-pre-wrap">{{ manualScpCmd }}</pre>
        <div class="text-[11px] text-fg-sub mt-2">agent 包路径：perf-agent/target/perf-agent.jar（平台构建产物，本地已存在）。若目标目录不存在请先执行 <code class="text-neon-blue">mkdir -p /opt/perftestor</code></div>
      </div>
      <div class="rounded-xl border border-neon-blue/30 bg-neon-blue/5 p-4">
        <div class="flex items-center justify-between mb-2">
          <div class="text-xs font-medium text-fg-main">② 带 agent 启动被测应用</div>
          <button class="text-[11px] text-neon-blue hover:underline" @click="copyText(manualStartCmd)">复制</button>
        </div>
        <pre class="text-[11px] text-fg-sub font-mono whitespace-pre-wrap">{{ manualStartCmd }}</pre>
        <div class="text-[11px] text-fg-sub mt-2 leading-relaxed">
          仅需将 <code>/path/to/app.jar</code> 替换为你的应用 jar 路径。<code>reportUrl</code> 已自动指向当前平台地址（{{ platformAddr }}）、
          <code>appName</code> 已按所选项目生成（当前项目：{{ repoName || '未选择' }}，数据按此归属）<template v-if="instAppPort">、<code>appPort={{ instAppPort }}</code> 取自该项目的应用实例</template>。
        </div>
      </div>
      <div class="rounded-xl border border-neon-green/30 bg-neon-green/5 p-4">
        <div class="text-xs font-medium text-fg-main mb-2">③ 验证接入</div>
        <div class="text-[11px] text-fg-sub leading-relaxed">
          启动后 5 秒内 agent 会向平台注册并上报指标。可在总览「接入应用」卡片确认应用上线，
          或压测后查看调用链数据是否产生。确认后点击底部「下一步」继续。
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { deployApi } from '../../api'

/**
 * 快速开始向导「部署」步骤：自动部署（平台编译 + SSH 上传启动）与手动部署（生成命令）。
 *
 * <p>从 QuickStartView 拆出，行为与视觉保持不变。
 * 部署成功后通过 deployed 事件通知父组件（用于步骤推进与灌数连接预填）。
 */
const props = defineProps<{
  /** 当前项目 ID */
  repoId: number
  /** 项目名（命令模板与数据归属用） */
  repoName: string
  /** 应用名（实例端口探测用） */
  appName: string
}>()

const emit = defineEmits<{
  /** 部署成功：父组件据此推进步骤并预填灌数连接 */
  (e: 'deployed'): void
}>()

const deployConns = ref<Array<Record<string, any>>>([])
const deployConnId = ref<number>(0)
const deployModule = ref('')
const deployPort = ref(8081)
const deploying = ref(false)
const buildResult = ref<any>(null)
const deployResult = ref<any>(null)
const deployStatus = ref('')
const deployLog = ref('')
const deployMode = ref<'auto' | 'manual'>('auto')

/** 拉取 SSH 连接列表（部署步骤下拉用） */
async function loadDeployConns() {
  const r = await deployApi.conns(50)
  deployConns.value = r.data || []
}

/** 内联新增 SSH 连接（手动输入 IP/端口/用户名/密码） */
const connFormOpen = ref(false)
const connForm = reactive({ connName: '', host: '', port: 22, username: 'root', password: '' })
const connTesting = ref(false)
const connSaving = ref(false)
const connMsg = ref('')
const connMsgOk = ref(false)

function connFormValid(): string | null {
  if (!connForm.connName.trim()) return '请填写连接名称'
  if (!connForm.host.trim()) return '请填写主机 IP'
  if (!connForm.username.trim()) return '请填写用户名'
  if (!connForm.password) return '请填写密码'
  return null
}

async function testNewConn() {
  const err = connFormValid()
  if (err) { connMsgOk.value = false; connMsg.value = err; return }
  connTesting.value = true
  connMsg.value = ''
  try {
    const r = await deployApi.testConn({ ...connForm, port: connForm.port || 22 })
    const d = r.data as Record<string, any>
    connMsgOk.value = !!d?.success
    connMsg.value = connMsgOk.value ? `连接成功：${String(d.remoteInfo || '').split('\n')[0]}` : `连接失败：${d?.error || '未知错误'}`
  } catch (e: any) {
    connMsgOk.value = false
    connMsg.value = '连接失败：' + (e?.response?.data?.message || e?.message || '请求异常')
  } finally {
    connTesting.value = false
  }
}

async function saveNewConn() {
  const err = connFormValid()
  if (err) { connMsgOk.value = false; connMsg.value = err; return }
  connSaving.value = true
  connMsg.value = ''
  try {
    const r = await deployApi.addConn({ ...connForm, port: connForm.port || 22, workDir: '/opt/perftestor' })
    await loadDeployConns()
    const created = r.data as Record<string, any> | null
    if (created?.id) deployConnId.value = created.id // 保存后自动选中
    connMsgOk.value = true
    connMsg.value = `「${connForm.connName.trim()}」已保存并选中`
    connFormOpen.value = false
    Object.assign(connForm, { connName: '', host: '', port: 22, username: 'root', password: '' })
  } catch (e: any) {
    connMsgOk.value = false
    connMsg.value = '保存失败：' + (e?.response?.data?.message || e?.message || '请求异常')
  } finally {
    connSaving.value = false
  }
}

const manualScpCmd = computed(() => {
  const c = deployConns.value.find((x) => x.id === deployConnId.value)
  const host = c ? `${c.username || 'root'}@${c.host}` : 'user@被测服务器IP'
  return `scp perf-agent/target/perf-agent.jar ${host}:/opt/perftestor/agent.jar`
})

/**
 * 手动部署：带 agent 的应用启动命令（按当前平台环境与所选项目动态生成）
 *  - reportUrl：浏览器当前访问平台的地址（跨机访问时自动带平台 IP；dev 代理/同源部署均正确）
 *  - appName：所选项目名（平台按项目名做数据归属校验）
 *  - appPort：项目绑定应用实例的端口（agent 上报，压测表单服务地址自动预填用）
 *  注意：-javaagent 参数必须紧跟 jar 路径（k=v 逗号分隔），换行续行会在拼接处引入空格导致参数丢失
 */
const manualStartCmd = computed(() => {
  const app = props.repoName || '项目名'
  const platform = window.location.host || '127.0.0.1:8080'
  const inst = appInstances.value.find((a) => a.appName === props.appName || a.appName === props.repoName)
  const appPortArg = inst?.appPort ? `,appPort=${inst.appPort}` : ''
  return `java -javaagent:/opt/perftestor/agent.jar=reportUrl=http://${platform}/api/agent/report,appName=${app}${appPortArg},sampleRate=1.0 -jar /path/to/app.jar`
})

/** 模板提示用：当前平台地址与所选项目应用实例端口 */
const platformAddr = computed(() => window.location.host || '127.0.0.1:8080')
const appInstances = ref<Array<Record<string, any>>>([])
const instAppPort = computed(() => {
  const inst = appInstances.value.find((a) => a.appName === props.appName || a.appName === props.repoName)
  return inst?.appPort || 0
})

function copyText(t: string) {
  navigator.clipboard?.writeText(t).catch(() => {
    const ta = document.createElement('textarea')
    ta.value = t
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  })
}

/** 编译并部署启动：build → SFTP 上传 → 远程 nohup 启动（agent 自动挂载）→ 健康检查 */
async function runDeploy() {
  if (!deployConnId.value || !props.repoId) return
  deploying.value = true
  deployLog.value = ''
  try {
    const b = await deployApi.build({ repoId: props.repoId, module: deployModule.value || undefined })
    buildResult.value = b.data
    if (!b.data?.success || !b.data?.jarPath) {
      deployLog.value = b.data?.logTail || '编译失败'
      return
    }
    const d = await deployApi.start({
      connId: deployConnId.value,
      jarPath: b.data.jarPath,
      appPort: deployPort.value
    })
    deployResult.value = d.data
    if (d.data?.success) {
      const st = await deployApi.status(deployConnId.value, deployPort.value).catch(() => null)
      deployStatus.value = st?.data?.status || ''
      emit('deployed')
    }
  } catch (e: any) {
    deployLog.value = '部署失败：' + (e?.response?.data?.message || e?.message || '请求异常')
  } finally {
    deploying.value = false
  }
}

onMounted(loadDeployConns)
defineExpose({ loadDeployConns, deployConnId })
</script>
