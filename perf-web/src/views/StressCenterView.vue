<template>
  <div>
    <div class="page-title">
      <Gauge class="w-5 h-5 text-neon-green" />
      压测中心
      <span class="text-xs font-normal text-fg-sub ml-2">压测 · 容量评估 · 扩容建议 · 数据准备</span>
    </div>

    <div class="flex gap-1 mb-4">
      <button
        v-for="tab in tabs" :key="tab.key"
        class="px-5 py-2 rounded-lg text-sm transition-all"
        :class="activeTab === tab.key ? 'bg-neon-green/15 text-neon-green border border-neon-green/30' : 'text-fg-sub border border-white/10 hover:text-fg-main'"
        @click="activeTab = tab.key"
      >{{ tab.label }}</button>
    </div>

    <!-- 压测 Tab -->
    <div v-if="activeTab === 'stress'" class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">新建压测任务</div>
        <div v-if="rerunFilled" class="text-xs text-neon-green bg-neon-green/10 border border-neon-green/20 rounded px-3 py-2 mb-3">
          已填入上次压测配置，可调整后点击「开始压测」重新发起
        </div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">项目</label>
            <select v-model="form.repoId" class="input-select w-full" @change="loadEndpoints">
              <option :value="0">— 手动填写 URL —</option>
              <option v-for="r in repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
            </select>
          </div>
          <div v-if="form.repoId > 0">
            <label class="text-xs text-fg-sub flex items-center justify-between">
              <span>{{ currentRepoType === 'git' ? '分支与接口' : '接口' }}</span>
              <button class="text-neon-blue hover:underline text-[11px]" :disabled="resyncing" @click="resyncAndScan">
                {{ resyncing ? '同步中…' : '↻ 与源码重新同步' }}
              </button>
            </label>
            <div v-if="currentRepoType === 'git'" class="flex gap-2 mb-2">
              <select
                :key="branchOptions.length + '-' + currentRepoBranch"
                class="input-select flex-1"
                :value="currentRepoBranch"
                :disabled="resyncing"
                @focus="loadBranchOptions"
                @change="changeBranch(($event.target as HTMLSelectElement).value)"
              >
                <!-- 已加载列表：渲染全部远端分支；当前分支不在列表中时补当前项 -->
                <template v-if="branchOptions.length">
                  <option v-for="b in branchOptions" :key="b" :value="b">{{ b }}</option>
                  <option v-if="currentRepoBranch && !branchOptions.includes(currentRepoBranch)" :value="currentRepoBranch">
                    {{ currentRepoBranch }}（当前）
                  </option>
                </template>
                <option v-else value="">{{ currentRepoBranch || '分支加载中…' }}</option>
              </select>
            </div>
            <select v-model="form.endpointIdx" class="input-select w-full" @change="applyEndpoint">
              <option :value="-1">— 选择接口（{{ endpoints.length }} 个）—</option>
              <option v-for="(e, i) in endpoints" :key="i" :value="i">
                [{{ e.httpMethod }}] {{ e.path }}（{{ e.className }}#{{ e.methodName || '?' }}）
              </option>
            </select>
          </div>
          <div>
            <label class="text-xs text-fg-sub">服务地址（选项目后按 agent 上报自动带出，可修改）</label>
            <input v-model="form.baseUrl" class="input-text w-full" placeholder="http://127.0.0.1:8081" @input="composeUrl()" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">目标 URL（{{ form.repoId > 0 ? '自动拼接，可微调' : 'http/https' }}）</label>
            <input v-model="form.targetUrl" class="input-text w-full" placeholder="http://host:port/api/xxx" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-fg-sub">方法</label>
              <select v-model="form.method" class="input-select w-full"><option>GET</option><option>POST</option></select>
            </div>
            <div>
              <label class="text-xs text-fg-sub">引擎</label>
              <select v-model="form.engineType" class="input-select w-full">
                <option value="http">http（内置）</option>
                <option value="jmeter" disabled title="jmeter 引擎尚未实现，敬请期待">jmeter（未实现）</option>
              </select>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-fg-sub">并发数</label>
              <input v-model.number="form.concurrency" type="number" class="input-text w-full" min="1" max="1000" />
            </div>
            <div>
              <label class="text-xs text-fg-sub">时长（秒）</label>
              <input v-model.number="form.durationSec" type="number" class="input-text w-full" min="1" max="3600" />
            </div>
          </div>
          <div>
            <label class="text-xs text-fg-sub">分档并发（逗号分隔，容量曲线）</label>
            <input v-model="form.stageConcurrencies" class="input-text w-full" placeholder="如 10,50,100" />
          </div>
          <button class="btn-primary w-full justify-center" :disabled="running" @click="startStress">
            {{ running ? '压测进行中…' : '开始压测' }}
          </button>
        </div>
      </div>

      <div class="glass-card p-5 xl:col-span-2">
        <div class="text-sm font-medium mb-3 flex items-center justify-between">
          <span>最近任务</span>
          <button class="text-xs text-neon-blue hover:underline" @click="loadTasks">刷新</button>
        </div>
        <div v-if="!tasks.length" class="text-xs text-fg-sub py-8 text-center">暂无压测任务</div>
        <div v-else class="space-y-2 max-h-[430px] overflow-y-auto pr-1">
          <div v-for="t in tasks" :key="t.id" class="px-4 py-3 rounded-lg bg-white/5 hover:bg-white/10 transition-all cursor-pointer"
            @click="showReport(t.id)">
            <div class="flex items-center justify-between">
              <div class="text-sm text-fg-main truncate">{{ t.taskName || t.targetUrl }}</div>
              <span class="flex items-center gap-2 shrink-0">
                <span v-if="t.appName" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue">{{ t.appName }}</span>
                <span v-if="t.appVersion" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-purple/10 text-neon-purple">v{{ t.appVersion }}</span>
                <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(t.status)">{{ t.status }}</span>
              </span>
            </div>
            <div class="flex items-center justify-between mt-2 text-[11px] text-fg-sub">
              <span>{{ t.concurrency }} 并发 · {{ t.durationSec }}s · {{ t.engineType }}</span>
              <span v-if="t.finishTime">{{ fmtTime(t.finishTime) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 压测报告 -->
    <div v-if="activeTab === 'stress' && report" class="glass-card p-5 mt-4">
      <div class="text-sm font-medium mb-4 flex items-center justify-between">
        <span class="flex items-center gap-2">
          压测报告 — {{ report.task.taskName || report.task.targetUrl }}
          <span v-if="report.task.appVersion" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-purple/10 text-neon-purple">v{{ report.task.appVersion }}</span>
        </span>
        <span class="flex items-center gap-3">
          <button v-if="taskFinished" class="text-xs text-neon-green hover:underline" @click="rerunTask">↻ 再来一次</button>
          <button class="text-xs text-neon-blue hover:underline" @click="runCapacity">容量评估 →</button>
        </span>
      </div>
      <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 mb-4">
        <div class="metric-card"><div class="text-xs text-fg-sub">总请求</div><div class="text-lg font-bold">{{ report.result?.totalRequests ?? '—' }}</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">QPS</div><div class="text-lg font-bold neon-text">{{ report.result?.qps ?? '—' }}</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">P50</div><div class="text-lg font-bold">{{ report.result?.p50Ms ?? '—' }}ms</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">P95</div><div class="text-lg font-bold">{{ report.result?.p95Ms ?? '—' }}ms</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">P99</div><div class="text-lg font-bold">{{ report.result?.p99Ms ?? '—' }}ms</div></div>
        <div class="metric-card"><div class="text-xs text-fg-sub">错误率</div><div class="text-lg font-bold" :class="(report.result?.errorRate ?? 0) > 5 ? 'text-red-400' : 'text-neon-green'">{{ report.result?.errorRate ?? 0 }}%</div></div>
      </div>

      <!-- 版本对比：选择同应用历史任务，比对核心指标（版本间性能回归/提升一目了然） -->
      <div v-if="compareCandidates.length" class="mb-4 p-4 rounded-lg bg-white/5 border border-white/10">
        <div class="flex items-center gap-3 mb-3">
          <span class="text-xs text-fg-sub shrink-0">版本对比</span>
          <select v-model.number="compareTaskId" class="input-select flex-1" @change="loadCompare(compareTaskId)">
            <option :value="0">选择要对比的历史任务…</option>
            <option v-for="t in compareCandidates" :key="t.id" :value="t.id">
              {{ compareLabel(t) }}
            </option>
          </select>
          <span v-if="compareLoading" class="text-[11px] text-fg-sub">加载中…</span>
        </div>
        <div v-if="compareReport" class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead>
              <tr class="text-fg-sub border-b border-white/10">
                <th class="text-left py-1.5 font-normal">指标</th>
                <th class="text-right py-1.5 font-normal">基线 {{ compareReport.task.appVersion ? 'v' + compareReport.task.appVersion : '未知版本' }}</th>
                <th class="text-right py-1.5 font-normal">当前 {{ report.task.appVersion ? 'v' + report.task.appVersion : '未知版本' }}</th>
                <th class="text-right py-1.5 font-normal">变化</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in compareRows" :key="row.label" class="border-b border-white/5">
                <td class="py-1.5 text-fg-dim">{{ row.label }}</td>
                <td class="text-right text-fg-sub">{{ row.base }}</td>
                <td class="text-right text-fg-main font-medium">{{ row.cur }}</td>
                <td class="text-right font-medium" :class="row.cls || 'text-fg-sub'">{{ row.delta || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div>
          <div class="text-xs text-fg-sub mb-2">QPS 实时曲线</div>
          <MetricChart :option="sampleOption" height="220px" />
        </div>
        <div>
          <div class="text-xs text-fg-sub mb-2">分档并发容量曲线（QPS / P95）</div>
          <MetricChart :option="stageOption" height="220px" />
        </div>
      </div>
      <div v-if="capacity" class="mt-4 p-4 rounded-lg bg-neon-green/5 border border-neon-green/20">
        <div class="text-sm font-medium text-neon-green mb-2">容量评估：目标 P95 ≤ {{ capacity.targetP95Ms }}ms</div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <div>最大可承受并发：<b class="text-lg text-neon-green">{{ capacity.maxConcurrency }}</b></div>
          <div>对应最大 QPS：<b class="text-lg text-neon-green">{{ capacity.maxQps }}</b></div>
          <div>采样档位：{{ capacity.curve?.length ?? 0 }} 个</div>
          <div v-if="capacity.extrapolated" class="text-amber-400">外推估算（超出实测范围）</div>
        </div>
      </div>
    </div>

    <!-- 容量建议 Tab -->
    <div v-else-if="activeTab === 'capacity'" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">扩容建议（基于监控数据）</div>
        <div v-if="!advice?.capacity" class="text-xs text-fg-sub py-10 text-center">暂无建议，先在压测 Tab 完成分档压测并评估容量</div>
        <div v-else class="space-y-3">
          <div
            v-for="(a, i) in advice.capacity.advices" :key="i"
            class="px-4 py-3 rounded-lg border"
            :class="riskClass(a.riskLevel)"
          >
            <div class="flex items-center justify-between">
              <div class="text-sm text-fg-main">{{ a.title }}</div>
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="riskClass(a.riskLevel)">{{ a.riskLevel }}</span>
            </div>
            <div class="text-xs text-fg-sub mt-1.5">{{ a.description }}</div>
            <div class="text-xs mt-1.5 flex gap-3">
              <span class="text-neon-green">方式：{{ a.scaleType }}</span>
              <span v-if="a.suggestedInstances">实例：{{ a.suggestedInstances }}</span>
            </div>
          </div>
        </div>
      </div>
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">JVM 配置建议</div>
        <div v-if="advice?.capacity?.jvmAdvice" class="space-y-3">
          <div class="px-4 py-3 rounded-lg bg-white/5">
            <div class="text-xs text-fg-sub">堆峰值 {{ advice.capacity.jvmAdvice.peakHeapUsedMb }}MB / 上限 {{ advice.capacity.jvmAdvice.heapMaxMb }}MB（{{ advice.capacity.jvmAdvice.peakHeapRatio }}%）</div>
            <div class="text-sm text-fg-main mt-1">建议：{{ advice.capacity.jvmAdvice.heapSize }}</div>
          </div>
          <div class="px-4 py-3 rounded-lg bg-white/5">
            <div class="text-xs text-fg-sub">GC 收集器</div>
            <div class="text-sm text-fg-main mt-1">{{ advice.capacity.jvmAdvice.gcCollector }}</div>
          </div>
          <div v-if="advice.capacity.jvmAdvice.params?.length" class="px-4 py-3 rounded-lg bg-white/5">
            <div class="text-xs text-fg-sub mb-2">关键参数</div>
            <div class="flex flex-wrap gap-2">
              <code v-for="(p, i) in advice.capacity.jvmAdvice.params" :key="i" class="text-[11px] px-2 py-1 rounded bg-white/5 text-neon-blue font-mono">{{ p }}</code>
            </div>
          </div>
        </div>
        <div v-else class="text-xs text-fg-sub py-10 text-center">暂无 JVM 配置建议</div>
      </div>
    </div>

    <!-- 数据准备 Tab -->
    <div v-else-if="activeTab === 'datagen'" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">自动灌数据</div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">连接配置（与「问题与优化」页共用，选中自动填充）</label>
            <select v-model="gen.connId" class="input-select w-full" @change="applyConn">
              <option :value="0">— 手动填写连接 —</option>
              <option v-for="c in conns" :key="c.id" :value="c.id">{{ c.connName }}（{{ c.dbUrl }}）</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-fg-sub">JDBC URL（jdbc:mysql://）</label>
            <input v-model="gen.dbUrl" class="input-text w-full" placeholder="jdbc:mysql://host:3306/db" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-fg-sub">用户</label><input v-model="gen.dbUser" class="input-text w-full" /></div>
            <div><label class="text-xs text-fg-sub">密码</label><input v-model="gen.dbPassword" type="password" class="input-text w-full" /></div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-fg-sub">目标表</label>
              <select v-model="gen.tableName" class="input-select w-full" @change="loadColumns">
                <option value="">— 先读取表清单 —</option>
                <option v-for="t in tables" :key="t" :value="t">{{ t }}</option>
              </select>
              <button class="text-xs text-neon-blue hover:underline mt-1" @click="loadTables">读取表清单</button>
            </div>
            <div>
              <label class="text-xs text-fg-sub">源码项目（魔法值/关联识别，可选）</label>
              <select v-model="gen.repoId" class="input-select w-full">
                <option :value="0">— 不使用源码分析 —</option>
                <option v-for="r in repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
              </select>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-fg-sub">行数</label><input v-model.number="gen.rowCount" type="number" class="input-text w-full" min="1" max="1000000" /></div>
            <div><label class="text-xs text-fg-sub">孤儿率 orphanRate（0~1，可选）</label><input v-model.number="gen.orphanRate" type="number" step="0.01" min="0" max="1" class="input-text w-full" /></div>
          </div>
          <div>
            <label class="text-xs text-fg-sub">字段规则(JSON) — 支持 enum+weights / range+overThreshold / ref / refSkew</label>
            <textarea v-model="gen.ruleJson" rows="3" class="input-text w-full font-mono text-xs"
              placeholder='{"fields":{"user_id":{"type":"refSkew","table":"user","column":"id","hotPercent":20,"hotShare":80},"status":{"type":"enum","values":["PENDING","PAID"],"weights":[20,80]}}}'></textarea>
            <div v-if="skewWarn" class="text-[11px] text-amber-400 mt-1">⚠ 检测到高热点倾斜（refSkew 热行承载过大）：建议降低批量或为子表分区，否则高并发下行锁冲突会抵消批量写入收益</div>
          </div>
          <div>
            <div class="flex items-center justify-between">
              <label class="text-xs text-fg-sub">联表表组(JSON，可选 — 填写后按表组拓扑排序灌数)</label>
              <button class="text-xs text-neon-blue hover:underline" @click="fillTableGroupExample">填入示例</button>
            </div>
            <textarea v-model="gen.tablesJson" rows="3" class="input-text w-full font-mono text-xs"
              placeholder='[{"tableName":"user","rowCount":10000},{"tableName":"order","rowCount":100000,"fields":{"user_id":{"type":"ref","table":"user","column":"id"}}}]'></textarea>
          </div>
          <div v-if="columns.length" class="text-xs text-fg-sub space-y-0.5">
            <div v-for="c in columns" :key="c.name">
              {{ c.name }}（{{ c.type }}）
              <span v-if="c.suggestedValues && c.suggestedValues.length" class="text-neon-green">建议值: {{ c.suggestedValues.join(' / ') }}</span>
              <span v-else-if="c.suspectEnum" class="text-amber-400">疑似枚举列，建议人工配置规则</span>
            </div>
          </div>
          <div v-if="suggestions.length" class="space-y-1.5">
            <div class="text-xs text-fg-sub font-medium">语义建议（低置信关联需确认采纳）</div>
            <div v-for="s in suggestions" :key="s.id" class="flex items-center justify-between px-3 py-2 rounded-lg bg-white/5">
              <div class="text-[11px] text-fg-sub">
                <template v-if="s.hintType === 'relation'">
                  {{ s.tableName }}.{{ s.columnName }} → {{ s.parentTable }}.{{ s.parentColumn }}
                  <span class="text-neon-blue ml-1">[{{ s.source }}] {{ (s.confidence * 100).toFixed(0) }}%</span>
                </template>
                <template v-else>
                  {{ s.tableName }}.{{ s.columnName }} 候选值 {{ s.valuesJson }}
                </template>
              </div>
              <div class="flex gap-2">
                <button class="text-[11px] text-neon-green hover:underline" @click="adoptHint(s.id, true)">采纳</button>
                <button class="text-[11px] text-fg-sub hover:underline" @click="adoptHint(s.id, false)">忽略</button>
              </div>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <button class="btn-secondary w-full justify-center" :disabled="running" @click="startDataGen(true)">试运行（校验预检）</button>
            <button class="btn-primary w-full justify-center" :disabled="running || !dryRunPassed" @click="startDataGen(false)">开始灌数据</button>
          </div>
          <div v-if="!dryRunPassed" class="text-[11px] text-fg-sub">建议先执行试运行：校验表存在/规则解析/关联识别/环检测与外键池预检，通过后开启正式执行</div>
        </div>
      </div>
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-3">灌数据任务</div>
        <div v-if="!genTasks.length" class="text-xs text-fg-sub py-8 text-center">暂无任务</div>
        <div v-else class="space-y-2">
          <div v-for="t in genTasks" :key="t.id" class="px-4 py-3 rounded-lg bg-white/5">
            <div class="flex items-center justify-between">
              <div class="text-sm text-fg-main">{{ t.taskName }}</div>
              <div class="flex items-center gap-2">
                <span v-if="t.tablesJson" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue">联表</span>
                <span class="text-[11px] px-2 py-0.5 rounded-full" :class="statusClass(t.status)">{{ t.status }}</span>
              </div>
            </div>
            <div class="text-[11px] text-fg-sub mt-1">
              {{ t.tableName }} · {{ t.inserted }}/{{ (t.totalRows || t.rowCount) }} 行 · {{ t.costMs }}ms
            </div>
            <div class="h-1.5 rounded bg-white/5 mt-2 overflow-hidden">
              <div class="h-full bg-neon-green rounded transition-all" :style="{ width: (t.inserted / Math.max(t.totalRows || t.rowCount, 1)) * 100 + '%' }"></div>
            </div>
            <div v-if="t.detailJson" class="text-[11px] mt-1.5 space-y-0.5">
              <div v-if="parseDetail(t.detailJson).dryRunReport" class="text-neon-green">✓ 试运行通过 · 预计 {{ parseDetail(t.detailJson).dryRunReport.totalRows }} 行 · 单表事务估算 {{ parseDetail(t.detailJson).dryRunReport.estimatedMaxTxnMs }}ms</div>
              <div v-for="(w, wi) in parseDetail(t.detailJson).warnings || []" :key="wi" class="text-amber-400">⚠ {{ w }}</div>
              <div v-if="parseDetail(t.detailJson).selfCheck" class="text-fg-sub">
                自检：非空率 {{ ((parseDetail(t.detailJson).selfCheck.nonEmptyRate || 0) * 100).toFixed(0) }}%
                <span v-if="parseDetail(t.detailJson).selfCheck.warning" class="text-amber-400">（{{ parseDetail(t.detailJson).selfCheck.warning }}）</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { Gauge } from 'lucide-vue-next'
import MetricChart from '../components/charts/MetricChart.vue'
import { taskStatusClass as statusClass, fmtTime } from '../utils/format'
import { stressApi, dataGenApi, capacityApi, sourceApi, appApi, dbConnApi } from '../api'
import { useRoute } from 'vue-router'

const route = useRoute()

const tabs = [
  { key: 'datagen', label: '数据准备' },
  { key: 'stress', label: '压测' },
  { key: 'capacity', label: '容量建议' }
]
const activeTab = ref(['stress', 'capacity', 'datagen'].includes(String(route.query.tab))
  ? String(route.query.tab) : 'stress')
// 支持外部（总览快速开始引导等）通过 /stress?tab=datagen 直达指定 Tab
watch(() => route.query.tab, (t) => {
  if (t && ['stress', 'capacity', 'datagen'].includes(String(t))) activeTab.value = String(t)
})
const running = ref(false)
const tasks = ref<Array<Record<string, any>>>([])
const report = ref<Record<string, any> | null>(null)
const samples = ref<Array<Record<string, any>>>([])
const capacity = ref<Record<string, any> | null>(null)
const advice = ref<Record<string, any> | null>(null)
const genTasks = ref<Array<Record<string, any>>>([])
const tables = ref<string[]>([])
const columns = ref<Array<{ name: string; type: string; suggestedValues?: any[]; suspectEnum?: boolean }>>([])

const form = ref({
  taskName: '',
  targetUrl: '',
  repoId: 0,
  endpointIdx: -1,
  baseUrl: '',
  method: 'GET',
  concurrency: 50,
  durationSec: 30,
  engineType: 'http',
  stageConcurrencies: '10,50,100'
})
// 项目/应用/接口扫描
const repos = ref<Array<Record<string, any>>>([])
const appInstances = ref<Array<Record<string, any>>>([])
const endpoints = ref<Array<Record<string, any>>>([])

// 当前项目归属应用（压测任务 appName 取值）
const currentRepoApp = computed(() => {
  const r = repos.value.find((x) => x.id === form.value.repoId)
  if (!r) return ''
  return r.appName || r.repoName || ''
})

// 当前项目类型：分支切换框仅对 git 项目展示（zip/local 无分支概念）
const currentRepoType = computed(() => {
  const r = repos.value.find((x) => x.id === form.value.repoId)
  return r ? (r.repoType || '') : ''
})

async function loadEndpoints() {
  endpoints.value = []
  form.value.endpointIdx = -1
  branchOptions.value = []
  if (!form.value.repoId) return
  try {
    const r = await sourceApi.endpoints(form.value.repoId)
    endpoints.value = r.data || []
  } catch { /* 扫描失败保持空列表 */ }
  // 选定 git 项目后自动同步分支列表（zip/local 项目无分支，不请求）
  if (currentRepoType.value === 'git') {
    loadBranchOptions()
  }
  // 选定项目后按 agent 上报的应用实例自动预填服务地址
  prefillBaseUrl()
}

// 服务地址自动预填：agent 上报注册的应用实例（host + 可选端口），
// 用户手动修改过后不再覆盖（仅空值或上次自动值时刷新）
let autoBaseUrl = ''
function prefillBaseUrl() {
  const app = currentRepoApp.value
  const inst = appInstances.value.find((x) => x.appName === app)
  if (!inst || !inst.appHost) return
  const url = 'http://' + inst.appHost + (inst.appPort ? ':' + inst.appPort : '')
  if (!form.value.baseUrl || form.value.baseUrl === autoBaseUrl) {
    form.value.baseUrl = url
    autoBaseUrl = url
    composeUrl()
  }
}

// 与源码重新同步：重新拉取源码 → 重建索引 → 重扫接口（源码更新后点击）
const resyncing = ref(false)
async function resyncAndScan() {
  if (!form.value.repoId || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.resync(form.value.repoId)
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

// 分支切换：当前仓库分支 + 分支列表缓存 + 切换（重同步 + 重扫接口）
const currentRepoBranch = computed(() => {
  const r = repos.value.find((x) => x.id === form.value.repoId)
  return r ? r.branch || '' : ''
})
const branchOptions = ref<string[]>([])
async function loadBranchOptions() {
  if (!form.value.repoId) return
  try {
    const r = await sourceApi.branches(form.value.repoId)
    branchOptions.value = r.data || []
  } catch { /* 拉取失败保持空 */ }
}
async function changeBranch(branch: string) {
  if (!branch || branch === currentRepoBranch.value || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.switchBranch(form.value.repoId, branch)
    await loadRepos()
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

// 选中接口：自动填 method 与 URL（baseUrl + path），用户只需提供 host:port
function applyEndpoint() {
  const e = endpoints.value[form.value.endpointIdx]
  if (!e) return
  form.value.method = e.httpMethod === 'POST' ? 'POST' : 'GET'
  composeUrl(e.path)
  if (!form.value.taskName) {
    form.value.taskName = `${e.className}#${e.methodName || e.path}`
  }
}

// 拼接完整目标 URL；显式传入 path（选接口时）优先于当前输入
function composeUrl(pathOverride?: string) {
  if (form.value.repoId <= 0) return
  const base = (form.value.baseUrl || '').replace(/\/+$/, '')
  const p = pathOverride !== undefined ? pathOverride : currentPath()
  if (base && p) {
    form.value.targetUrl = base + p
  }
}

function currentPath(): string {
  const e = endpoints.value[form.value.endpointIdx]
  return e ? e.path : ''
}
const gen = ref({
  connId: 0,
  dbUrl: '',
  dbUser: 'root',
  dbPassword: '',
  tableName: '',
  rowCount: 1000,
  ruleJson: '',
  tablesJson: '',
  orphanRate: 0,
  repoId: 0
})
const suggestions = ref<Array<Record<string, any>>>([])
const dryRunPassed = ref(false)

// refSkew 高倾斜警告：hotShare/hotPercent 比值 > 2.5 视为热行承载过大
const skewWarn = computed(() => {
  try {
    const root = JSON.parse(gen.value.ruleJson || '{}')
    const fields = root.fields || root || {}
    return Object.values(fields).some((r: any) =>
      r?.type === 'refSkew' && (r.hotShare || 80) / Math.max(r.hotPercent || 20, 1) > 2.5)
  } catch {
    return false
  }
})

function parseDetail(json: string): any {
  try {
    return typeof json === 'string' ? JSON.parse(json) : json
  } catch {
    return {}
  }
}

function fillTableGroupExample() {
  gen.value.tablesJson = JSON.stringify([
    { tableName: gen.value.tableName || 'user', rowCount: 10000 },
    { tableName: 'order', rowCount: 50000, fields: { user_id: { type: 'ref', table: 'user', column: 'id' } } }
  ], null, 0)
}

async function loadSuggestions() {
  try {
    const r = await dataGenApi.suggestions({
      repoId: gen.value.repoId || undefined,
      tableName: gen.value.tableName || undefined,
      status: 'pending'
    })
    suggestions.value = r.data || []
  } catch { /* 建议查询失败不阻断 */ }
}

async function adoptHint(id: number, adopt: boolean) {
  await dataGenApi.adopt(id, adopt)
  await loadSuggestions()
}

const sampleOption = computed(() => ({
  backgroundColor: 'transparent',
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 12, top: 12, bottom: 20 },
  xAxis: { type: 'category', data: samples.value.map((s) => `#${s.id}`), axisLabel: { color: '#8A94A6', fontSize: 10 }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } } },
  yAxis: { type: 'value', axisLabel: { color: '#8A94A6', fontSize: 10 }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } } },
  series: [
    { name: 'QPS', type: 'line', smooth: true, showSymbol: false, data: samples.value.map((s) => s.qps), lineStyle: { color: '#00E5A0', width: 2 }, areaStyle: { color: 'rgba(0,229,160,0.12)' } },
    { name: 'P95(ms)', type: 'line', smooth: true, showSymbol: false, data: samples.value.map((s) => s.p95Ms), lineStyle: { color: '#FAAD14', width: 1.5 }, yAxisIndex: 0 }
  ]
}))

const stageOption = computed(() => {
  const pts = report.value?.result?.stagePoints || []
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#8A94A6', fontSize: 11 }, top: 0 },
    grid: { left: 40, right: 40, top: 26, bottom: 20 },
    xAxis: { type: 'category', data: pts.map((p: any) => `${p.concurrency} 并发`), axisLabel: { color: '#8A94A6', fontSize: 10 }, axisLine: { lineStyle: { color: 'rgba(255,255,255,0.15)' } } },
    yAxis: [
      { type: 'value', name: 'QPS', axisLabel: { color: '#8A94A6', fontSize: 10 }, splitLine: { lineStyle: { color: 'rgba(255,255,255,0.06)' } } },
      { type: 'value', name: 'P95ms', axisLabel: { color: '#8A94A6', fontSize: 10 }, splitLine: { show: false } }
    ],
    series: [
      { name: 'QPS', type: 'bar', data: pts.map((p: any) => p.qps), itemStyle: { color: '#0EA5E9', borderRadius: [4, 4, 0, 0] } },
      { name: 'P95(ms)', type: 'line', yAxisIndex: 1, data: pts.map((p: any) => p.p95Ms), lineStyle: { color: '#00E5A0', width: 2 }, itemStyle: { color: '#00E5A0' } }
    ]
  }
})

function riskClass(r: string) {
  return r === 'high'
    ? 'border-red-400/30 bg-red-400/5 text-red-300'
    : r === 'medium'
      ? 'border-amber-400/30 bg-amber-400/5 text-amber-300'
      : 'border-neon-green/30 bg-neon-green/5 text-neon-green'
}

async function loadTasks() {
  const r = await stressApi.list({ limit: 20 })
  tasks.value = r.data
}

let pollTimer: ReturnType<typeof setInterval> | null = null
function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    loadTasks()
  }, 3000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function startStress() {
  if (!form.value.targetUrl) return
  const stageList = form.value.stageConcurrencies
    .split(',').map((s) => Number(s.trim())).filter((n) => n > 0)
  const body: Record<string, unknown> = {
    taskName: form.value.taskName || undefined,
    targetUrl: form.value.targetUrl,
    // 压测任务归属应用 = 项目的应用
    appName: currentRepoApp.value || undefined,
    method: form.value.method,
    concurrency: form.value.concurrency,
    durationSec: form.value.durationSec,
    engineType: form.value.engineType
  }
  if (stageList.length > 1) body.stageConcurrencies = stageList
  running.value = true
  try {
    await stressApi.create(body)
    await loadTasks()
    setTimeout(() => { running.value = false }, 3000)
  } catch {
    running.value = false
  }
}

async function showReport(id: number) {
  const r = await stressApi.report(id)
  report.value = r.data
  capacity.value = r.data.result?.capacity || null
  // 切换报告时重置版本对比状态
  compareTaskId.value = 0
  compareReport.value = null
  const s = await stressApi.samples(id)
  samples.value = s.data
}

// 报告任务是否已执行完（完成或失败均可"再来一次"）
const taskFinished = computed(() => {
  const s = report.value?.task?.status
  return s === 'finished' || s === 'failed'
})

// ---------- 版本对比 ----------
// 可对比候选：同应用（或双方均无应用）的其他已完成任务
const compareCandidates = computed(() => {
  const cur = report.value?.task
  if (!cur) return []
  return tasks.value.filter((t: any) =>
    t.id !== cur.id && t.status === 'finished' &&
    (!cur.appName || !t.appName || t.appName === cur.appName))
})

const compareTaskId = ref(0)
const compareReport = ref<any>(null)
const compareLoading = ref(false)

// 候选项文案：版本 + 时间 + 并发
function compareLabel(t: any) {
  const v = t.appVersion ? 'v' + t.appVersion : '未带版本'
  const time = t.finishTime ? fmtTime(t.finishTime) : fmtTime(t.createTime)
  return `${v} · ${time} · ${t.concurrency}并发（${t.taskName || t.targetUrl}）`
}

async function loadCompare(id: number) {
  compareReport.value = null
  if (!id) return
  compareLoading.value = true
  try {
    const r = await stressApi.report(id)
    compareReport.value = r.data
  } catch { /* 拉取失败保持空 */ } finally {
    compareLoading.value = false
  }
}

// 相对变化百分比（基线 → 当前）
function pct(cur: number | null | undefined, base: number | null | undefined): string {
  if (cur == null || base == null || base === 0) return ''
  const v = ((cur - base) / base) * 100
  return (v > 0 ? '+' : '') + v.toFixed(1) + '%'
}

// 对比行：QPS 越高越好，延迟/错误率越低越好
const compareRows = computed(() => {
  const cur = report.value?.result
  const base = compareReport.value?.result
  if (!cur || !base) return []
  const defs = [
    { label: 'QPS', cur: cur.qps, base: base.qps, higherBetter: true },
    { label: 'P50 (ms)', cur: cur.p50Ms, base: base.p50Ms, higherBetter: false },
    { label: 'P95 (ms)', cur: cur.p95Ms, base: base.p95Ms, higherBetter: false },
    { label: 'P99 (ms)', cur: cur.p99Ms, base: base.p99Ms, higherBetter: false },
    { label: '错误率 (%)', cur: cur.errorRate, base: base.errorRate, higherBetter: false },
  ]
  return defs.map((d) => {
    const delta = pct(d.cur, d.base)
    const improved = delta !== '' && (d.higherBetter === delta.startsWith('+'))
    const cls = delta === '' ? '' : (improved ? 'text-neon-green' : 'text-red-400')
    return { label: d.label, cur: d.cur ?? '—', base: d.base ?? '—', delta: improved ? delta + ' ↑' : (delta ? delta + ' ↓' : ''), cls }
  })
})

// "再来一次"回填提示（短暂展示）
const rerunFilled = ref(false)

// 再来一次：把已完成任务的配置回填到左侧表单，由用户点击「开始压测」重新发起
function rerunTask() {
  const t = report.value?.task
  if (!t) return
  // 基本配置回填
  form.value.taskName = t.taskName || ''
  form.value.targetUrl = t.targetUrl || ''
  form.value.method = t.method || 'GET'
  form.value.concurrency = t.concurrency ?? 50
  form.value.durationSec = t.durationSec ?? 30
  form.value.engineType = t.engineType || 'http'
  // 服务地址：取 URL origin（http://host:port），便于后续选接口重新拼接
  try {
    form.value.baseUrl = new URL(t.targetUrl).origin
  } catch { /* 非标准 URL 保持原样 */ }
  // 分档并发：从报告分档采样点还原
  const pts = report.value?.result?.stagePoints || []
  if (pts.length) {
    form.value.stageConcurrencies = pts.map((p: any) => p.concurrency).join(',')
  }
  // 项目联动：按任务应用匹配项目（恢复接口列表；无匹配则手动 URL 模式）
  const repo = repos.value.find((r) => t.appName && r.appName === t.appName)
  if (repo) {
    form.value.repoId = repo.id
    loadEndpoints()
  } else {
    // 无匹配项目：退回手动 URL 模式（targetUrl 已完整填入，可直接压测）
    form.value.repoId = 0
    form.value.endpointIdx = -1
    endpoints.value = []
    branchOptions.value = []
  }
  activeTab.value = 'stress'
  rerunFilled.value = true
  setTimeout(() => { rerunFilled.value = false }, 6000)
}

async function runCapacity() {
  if (!report.value?.task?.id) return
  const r = await capacityApi.analyze({ taskId: report.value.task.id })
  capacity.value = r.data
  // 重新读取报告使 capacity 落入报告
  await showReport(report.value.task.id)
}

async function loadAdvice() {
  const r = await capacityApi.advice()
  advice.value = r.data
}

async function loadTables() {
  const r = await dataGenApi.tables({ dbUrl: gen.value.dbUrl, dbUser: gen.value.dbUser, dbPassword: gen.value.dbPassword })
  tables.value = r.data
}

async function loadColumns() {
  if (!gen.value.tableName) return
  const r = await dataGenApi.columns({ dbUrl: gen.value.dbUrl, dbUser: gen.value.dbUser, dbPassword: gen.value.dbPassword, tableName: gen.value.tableName })
  columns.value = r.data
  loadSuggestions()
}

async function startDataGen(dryRun: boolean) {
  let tables
  if (gen.value.tablesJson && gen.value.tablesJson.trim()) {
    try {
      tables = JSON.parse(gen.value.tablesJson)
    } catch {
      alert('表组 JSON 解析失败，请检查格式')
      return
    }
  }
  await dataGenApi.create({
    taskName: `${dryRun ? '试运行' : '灌数据'}-${tables ? tables[0].tableName : gen.value.tableName}`,
    connId: gen.value.connId || undefined,
    dbUrl: gen.value.dbUrl,
    dbUser: gen.value.dbUser,
    dbPassword: gen.value.dbPassword,
    tableName: gen.value.tableName,
    rowCount: gen.value.rowCount,
    ruleJson: gen.value.ruleJson || undefined,
    tables,
    orphanRate: gen.value.orphanRate || undefined,
    repoId: gen.value.repoId || undefined,
    dryRun
  })
  if (dryRun) {
    dryRunPassed.value = true
  }
  const r = await dataGenApi.list()
  genTasks.value = r.data
}

onMounted(() => {
  loadTasks()
  startPolling()
  loadAdvice()
  loadRepos()
  loadApps()
  loadConns()
  dataGenApi.list().then((r) => { genTasks.value = r.data }).catch(() => {})
})

onUnmounted(stopPolling)

// 项目/应用/连接配置（数据准备 Tab 复用连接配置）
const conns = ref<Array<Record<string, any>>>([])

async function loadRepos() {
  const r = await sourceApi.repos()
  repos.value = r.data
}

async function loadApps() {
  try {
    const r = await appApi.list()
    appInstances.value = r.data || []
  } catch { /* 应用列表失败不阻断 */ }
}

async function loadConns() {
  try {
    const r = await dbConnApi.list()
    conns.value = r.data || []
  } catch { /* 连接配置失败不阻断 */ }
}

// 数据准备：从连接配置选择后自动填连接参数（仍可手改）
function applyConn() {
  const c = conns.value.find((x) => x.id === gen.value.connId)
  if (c) {
    gen.value.dbUrl = c.dbUrl
    gen.value.dbUser = c.username
    gen.value.dbPassword = c.password || ''
  }
}
</script>
