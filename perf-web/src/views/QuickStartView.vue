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
    <div v-else-if="step === 1" class="glass-card p-5">
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

    <!-- Step 4 压测 -->
    <div v-else-if="step === 3" class="glass-card p-5">
      <div class="text-sm font-medium mb-1">配置并发起压测 <span class="text-neon-green text-xs ml-1">项目：{{ repoName }}</span></div>
      <div class="text-[11px] text-fg-sub mb-4">参数与压测中心一致（接口/方法/引擎/预热/分档并发）。需要完整配置？<router-link class="text-neon-blue hover:underline" to="/stress">打开压测中心</router-link></div>
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
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Rocket, Check, ArrowRight, Bot } from 'lucide-vue-next'
import StressForm from '../components/stress/StressForm.vue'
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
const deployConnId = ref<number>(0)
const deployModule = ref('')
const deployPort = ref(8081)
const deploying = ref(false)
const buildResult = ref<any>(null)
const deployResult = ref<any>(null)
const deployStatus = ref('')
const deployLog = ref('')
/** 部署方式：auto=平台编译+SSH 上传启动；manual=生成带 agent 的启动命令自行执行 */
const deployMode = ref<'auto' | 'manual'>('auto')
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
/** 手动部署：SCP 上传 agent 命令 */
const manualScpCmd = computed(() => {
  const c = deployConns.value.find((x) => x.id === deployConnId.value)
  const host = c ? `${c.username || 'root'}@${c.host}` : 'user@被测服务器IP'
  return `scp perf-agent/target/perf-agent.jar ${host}:/opt/perftestor/agent.jar`
})

/** 手动部署：带 agent 的应用启动命令（按当前平台环境与所选项目动态生成）
 *  - reportUrl：浏览器当前访问平台的地址（跨机访问时自动带平台 IP；dev 代理/同源部署均正确）
 *  - appName：所选项目名（平台按项目名做数据归属校验）
 *  - appPort：项目绑定应用实例的端口（agent 上报，压测表单服务地址自动预填用）
 *  注意：-javaagent 参数必须紧跟 jar 路径（k=v 逗号分隔），换行续行会在拼接处引入空格导致参数丢失 */
const manualStartCmd = computed(() => {
  const app = repoName.value || '项目名'
  const platform = window.location.host || '127.0.0.1:8080'
  const inst = appInstances.value.find((a) => a.appName === appName.value || a.appName === repoName.value)
  const appPortArg = inst?.appPort ? `,appPort=${inst.appPort}` : ''
  return `java -javaagent:/opt/perftestor/agent.jar=reportUrl=http://${platform}/api/agent/report,appName=${app}${appPortArg},sampleRate=1.0 -jar /path/to/app.jar`
})

/** 模板提示用：当前平台地址与所选项目应用实例端口 */
const platformAddr = computed(() => window.location.host || '127.0.0.1:8080')
const instAppPort = computed(() => {
  const inst = appInstances.value.find((a) => a.appName === appName.value || a.appName === repoName.value)
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
const endpoints = ref<Array<Record<string, any>>>([])
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
const deployConns = ref<Array<Record<string, any>>>([])
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
  loadEndpoints()
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

async function loadEndpoints() {
  endpoints.value = []
  if (!repoId.value) return
  try {
    // agent 上报的应用实例自动预填服务地址（host + 可选端口，经 StressForm autoBaseUrl）
    const r = await sourceApi.endpoints(repoId.value)
    endpoints.value = r.data || [] // GET/POST 全保留（与压测中心一致）
    if (endpoints.value.length) {
      // 自动选中首个接口（表单内完成方法与 URL 拼接）
      stressFormRef.value?.selectFirstEndpoint()
    }
  } catch { /* 扫描失败不阻断 */ }
}

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
    const t = await traceApi.list({ app: appName.value || undefined, limit: 5 })
    traces.value = t.data || []
  } catch { /* 忽略 */ }
  try {
    const i = await sourceApi.issues({ limit: 5 })
    issues.value = i.data || []
  } catch { /* 忽略 */ }
}

/** 编译并部署启动：build → SFTP 上传 → 远程 nohup 启动（agent 自动挂载）→ 健康检查 */
async function runDeploy() {
  if (!deployConnId.value || !repoId.value) return
  deploying.value = true
  deployLog.value = ''
  try {
    const b = await deployApi.build({ repoId: repoId.value, module: deployModule.value || undefined })
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
      sessionDone.value[1] = true // deploy ok
      const st = await deployApi.status(deployConnId.value, deployPort.value).catch(() => null)
      deployStatus.value = st?.data?.status || ''
      // 探测被测应用上报的数据库连接（agent JdbcInterceptor 采集），供灌数步骤自动预填
      try {
        const sl = await slowSqlApi.list({ limit: 5 })
        const url = (sl.data || []).map((x: any) => x.dbUrl).find((u: string) => !!u)
        if (url) {
          autoDbUrl.value = url
          const match = conns.value.find((c: any) => url.startsWith(String(c.dbUrl || '').split('?')[0]))
          if (match) connId.value = match.id
        }
      } catch { /* 探测失败不影响流程 */ }
      setTimeout(advance, 1500) // 部署成功自动进入灌数步骤
    }
  } catch (e: any) {
    deployLog.value = '部署失败：' + (e?.response?.data?.message || e?.message || '请求异常')
  } finally {
    deploying.value = false
  }
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
  loadDeployConns()
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
