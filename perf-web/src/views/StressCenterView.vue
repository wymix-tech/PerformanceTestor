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
        class="px-5 py-2 rounded-lg text-sm transition-all cursor-pointer"
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
            <select v-model="repoId" class="input-select w-full" @change="loadEndpoints">
              <option :value="0">— 手动填写 URL —</option>
              <option v-for="r in repoStore.repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
            </select>
          </div>
          <div v-if="repoId > 0">
            <label class="text-xs text-fg-sub flex items-center justify-between">
              <span>{{ currentRepoType === 'git' ? '分支与接口' : '接口' }}</span>
              <button class="text-neon-blue hover:underline text-[11px] cursor-pointer" :disabled="resyncing" @click="resyncAndScan">
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
          </div>
          <!-- 压测参数公共组件（接口选择/URL 拼接/并发/时长/预热/QPS/分档） -->
          <StressForm
            ref="stressFormRef"
            :endpoints="endpoints"
            :auto-base-url="autoBaseUrl"
            :disabled="running"
            submit-label="开始压测"
            @submit="startStress"
            @endpoint-selected="onEndpointSelected"
          />
        </div>
      </div>

      <div class="glass-card p-5 xl:col-span-2">
        <div class="text-sm font-medium mb-3 flex items-center justify-between">
          <span>最近任务</span>
          <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="loadTasks">刷新</button>
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

    <!-- 压测报告（公共组件；版本对比经 #extra 插槽注入） -->
    <StressReport
      v-if="activeTab === 'stress' && report"
      :report="report"
      :samples="samples"
      :capacity="capacity"
      class="mt-4"
      @rerun="rerunTask"
      @run-capacity="runCapacity"
    >
      <template #extra>
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
      </template>
    </StressReport>

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
              <button class="text-xs text-neon-blue hover:underline mt-1 cursor-pointer" @click="loadTables">读取表清单</button>
            </div>
            <div>
              <label class="text-xs text-fg-sub">源码项目（魔法值/关联识别，可选）</label>
              <select v-model="gen.repoId" class="input-select w-full">
                <option :value="0">— 不使用源码分析 —</option>
                <option v-for="r in repoStore.repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
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
              <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="fillTableGroupExample">填入示例</button>
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
                <button class="text-[11px] text-neon-green hover:underline cursor-pointer" @click="adoptHint(s.id, true)">采纳</button>
                <button class="text-[11px] text-fg-sub hover:underline cursor-pointer" @click="adoptHint(s.id, false)">忽略</button>
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

    <!-- 定时压测 Tab：调度管理 + 版本基线对比（回归门禁） -->
    <div v-else-if="activeTab === 'schedule'" class="grid grid-cols-1 xl:grid-cols-3 gap-4">
      <div class="glass-card p-5">
        <div class="text-sm font-medium mb-4">新建定时压测</div>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-fg-sub">调度名称</label>
            <input v-model="sch.name" class="input-text w-full" placeholder="如：核心接口回归门禁" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">压测模板（基于当前表单参数）</label>
            <div class="text-[11px] text-fg-sub bg-white/5 rounded-lg px-3 py-2 font-mono break-all">
              {{ sch.templatePreview || '在「压测」Tab 配置参数后回到此处（或直接填下方模板 JSON）' }}
            </div>
            <textarea v-model="sch.taskJson" rows="4" class="input-text w-full font-mono text-xs mt-2"
              placeholder='{"targetUrl":"http://127.0.0.1:8081/api/user/list?limit=200","concurrency":20,"durationSec":30}'></textarea>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-fg-sub">间隔（分钟）</label>
              <input v-model.number="sch.intervalMinutes" type="number" class="input-text w-full" min="1" max="10080" />
            </div>
            <div>
              <label class="text-xs text-fg-sub">基线任务 ID（0=自动）</label>
              <input v-model.number="sch.baselineTaskId" type="number" class="input-text w-full" min="0" />
            </div>
          </div>
          <div>
            <label class="text-xs text-fg-sub">告警 Webhook（钉钉/飞书/自定义，可选）</label>
            <input v-model="sch.webhookUrl" class="input-text w-full" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" />
            <div class="text-[11px] text-fg-sub mt-1">回归阈值：QPS↓&gt;10% / P95↑&gt;20% / 错误率↑&gt;5pct 任一命中即告警</div>
          </div>
          <button class="btn-primary w-full justify-center" :disabled="creatingSchedule" @click="createSchedule">
            {{ creatingSchedule ? '创建中…' : '创建调度' }}
          </button>
        </div>
      </div>

      <div class="glass-card p-5 xl:col-span-2">
        <div class="text-sm font-medium mb-3 flex items-center justify-between">
          <span>调度列表</span>
          <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="loadSchedules">刷新</button>
        </div>
        <div v-if="!schedules.length" class="text-xs text-fg-sub py-8 text-center">暂无定时压测调度</div>
        <div v-else class="space-y-2 max-h-[430px] overflow-y-auto pr-1">
          <div v-for="s in schedules" :key="s.id" class="px-4 py-3 rounded-lg bg-white/5">
            <div class="flex items-center justify-between">
              <div class="text-sm text-fg-main">{{ s.scheduleName }}</div>
              <div class="flex items-center gap-2">
                <span class="text-[11px] px-2 py-0.5 rounded-full" :class="s.enabled ? 'bg-neon-green/10 text-neon-green' : 'bg-white/5 text-fg-sub'">
                  {{ s.enabled ? '运行中' : '已暂停' }}
                </span>
                <button class="text-[11px] text-neon-blue hover:underline cursor-pointer" @click="toggleSchedule(s)">
                  {{ s.enabled ? '暂停' : '启用' }}
                </button>
                <button class="text-[11px] text-neon-green hover:underline cursor-pointer" @click="loadDiff(s)">基线对比</button>
                <button class="text-[11px] text-red-400 hover:underline cursor-pointer" @click="removeSchedule(s)">删除</button>
              </div>
            </div>
            <div class="text-[11px] text-fg-sub mt-1.5 flex flex-wrap gap-3">
              <span>每 {{ s.intervalMinutes }} 分钟</span>
              <span v-if="s.lastRunTime">上次触发 {{ fmtTime(s.lastRunTime) }}</span>
              <span v-if="s.nextRunTime">下次 {{ fmtTime(s.nextRunTime) }}</span>
              <span v-if="s.lastTaskId">任务 #{{ s.lastTaskId }}</span>
            </div>
            <!-- 基线对比结果 -->
            <div v-if="diffOf[s.id]" class="mt-3">
              <div v-if="diffOf[s.id].noBaseline" class="text-[11px] text-amber-400">暂无可用基线（首次执行或历史任务缺失）</div>
              <template v-else>
                <div class="flex items-center gap-2 mb-1.5">
                  <span class="text-[11px] text-fg-sub">基线 #{{ diffOf[s.id].baseline?.id }}</span>
                  <span class="text-fg-sub">→</span>
                  <span class="text-[11px] text-fg-sub">当前 #{{ diffOf[s.id].current?.id }}</span>
                  <span v-if="diffOf[s.id].regressed" class="text-[11px] px-2 py-0.5 rounded-full bg-red-400/10 text-red-400">⚠ 检测到性能回归</span>
                  <span v-else class="text-[11px] px-2 py-0.5 rounded-full bg-neon-green/10 text-neon-green">✓ 指标正常</span>
                </div>
                <table class="w-full text-[11px]">
                  <tbody>
                    <tr v-for="r in diffOf[s.id].rows" :key="r.label" class="border-b border-white/5">
                      <td class="py-1 text-fg-dim">{{ r.label }}</td>
                      <td class="text-right text-fg-sub">{{ r.base }}</td>
                      <td class="text-right text-fg-main">{{ r.cur }}</td>
                      <td class="text-right font-medium" :class="r.regressed ? 'text-red-400' : r.improved ? 'text-neon-green' : 'text-fg-sub'">{{ r.deltaPct || '—' }}</td>
                    </tr>
                  </tbody>
                </table>
              </template>
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
import StressForm from '../components/stress/StressForm.vue'
import StressReport from '../components/stress/StressReport.vue'
import { taskStatusClass as statusClass, fmtTime } from '../utils/format'
import { stressApi, dataGenApi, capacityApi, sourceApi, appApi, dbConnApi, scheduleApi } from '../api'
import { useRepoStore } from '../stores/repoStore'
import { useRoute } from 'vue-router'

const route = useRoute()
const repoStore = useRepoStore()

const tabs = [
  { key: 'datagen', label: '数据准备' },
  { key: 'stress', label: '压测' },
  { key: 'capacity', label: '容量建议' },
  { key: 'schedule', label: '定时压测' }
]
const activeTab = ref(['stress', 'capacity', 'datagen'].includes(String(route.query.tab))
  ? String(route.query.tab) : 'stress')
// 支持外部（总览快速开始引导等）通过 /stress?tab=datagen 直达指定 Tab
watch(() => route.query.tab, (t) => {
  if (t && ['stress', 'capacity', 'datagen', 'schedule'].includes(String(t))) activeTab.value = String(t)
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

// ---------- 项目选择与接口扫描（视图特有逻辑，表单细节在 StressForm 公共组件） ----------
const repoId = ref(0)
const endpoints = ref<Array<Record<string, any>>>([])
const appInstances = ref<Array<Record<string, any>>>([])
const stressFormRef = ref<InstanceType<typeof StressForm> | null>(null)
// 任务名（选中接口时自动派生）
const taskName = ref('')

/** 当前项目归属应用（压测任务 appName 取值） */
const currentRepoApp = computed(() => {
  const r = repoStore.repos.find((x) => x.id === repoId.value)
  if (!r) return ''
  return r.appName || r.repoName || ''
})

/** 当前项目类型：分支切换框仅对 git 项目展示（zip/local 无分支概念） */
const currentRepoType = computed(() => {
  const r = repoStore.repos.find((x) => x.id === repoId.value)
  return r ? (r.repoType || '') : ''
})

/** agent 上报的服务地址（StressForm 自动预填） */
const autoBaseUrl = computed(() => {
  const inst = appInstances.value.find((x) => x.appName === currentRepoApp.value)
  if (!inst || !inst.appHost) return ''
  return 'http://' + inst.appHost + (inst.appPort ? ':' + inst.appPort : '')
})

async function loadEndpoints() {
  endpoints.value = []
  branchOptions.value = []
  if (!repoId.value) return
  try {
    const r = await sourceApi.endpoints(repoId.value)
    endpoints.value = r.data || []
  } catch { /* 扫描失败保持空列表 */ }
  // 选定 git 项目后自动同步分支列表（zip/local 项目无分支，不请求）
  if (currentRepoType.value === 'git') {
    loadBranchOptions()
  }
}

// 与源码重新同步：重新拉取源码 → 重建索引 → 重扫接口（源码更新后点击）
const resyncing = ref(false)
async function resyncAndScan() {
  if (!repoId.value || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.resync(repoId.value)
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

// 分支切换：当前仓库分支 + 分支列表缓存 + 切换（重同步 + 重扫接口）
const currentRepoBranch = computed(() => {
  const r = repoStore.repos.find((x) => x.id === repoId.value)
  return r ? r.branch || '' : ''
})
const branchOptions = ref<string[]>([])
async function loadBranchOptions() {
  if (!repoId.value) return
  try {
    const r = await sourceApi.branches(repoId.value)
    branchOptions.value = r.data || []
  } catch { /* 拉取失败保持空 */ }
}
async function changeBranch(branch: string) {
  if (!branch || branch === currentRepoBranch.value || resyncing.value) return
  resyncing.value = true
  try {
    await sourceApi.switchBranch(repoId.value, branch)
    await repoStore.reload()
    await loadEndpoints()
  } finally {
    resyncing.value = false
  }
}

/** 选中接口回调：自动派生任务名 */
function onEndpointSelected(e: Record<string, any>) {
  if (!taskName.value) {
    taskName.value = `${e.className}#${e.methodName || e.path}`
  }
}

// ---------- 灌数（数据准备 Tab） ----------
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

/** 发起压测：StressForm 提交标准请求体，本视图补充任务名与应用归属 */
async function startStress(payload: Record<string, unknown>) {
  const body = {
    ...payload,
    taskName: taskName.value || undefined,
    // 压测任务归属应用 = 项目的应用
    appName: currentRepoApp.value || undefined
  }
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

// 再来一次：把已完成任务的配置回填到表单，由用户点击「开始压测」重新发起
function rerunTask() {
  const t = report.value?.task
  if (!t || !stressFormRef.value) return
  stressFormRef.value.fillFrom(t, report.value?.result?.stagePoints || [])
  // 项目联动：按任务应用匹配项目（恢复接口列表；无匹配则手动 URL 模式）
  const repo = repoStore.repos.find((r) => t.appName && r.appName === t.appName)
  if (repo) {
    repoId.value = repo.id
    loadEndpoints()
  } else {
    // 无匹配项目：退回手动 URL 模式（targetUrl 已完整填入，可直接压测）
    repoId.value = 0
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
  repoStore.load()
  loadApps()
  loadConns()
  dataGenApi.list().then((r) => { genTasks.value = r.data }).catch(() => {})
  loadSchedules()
})

onUnmounted(stopPolling)

// 项目/应用/连接配置（数据准备 Tab 复用连接配置）
const conns = ref<Array<Record<string, any>>>([])

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

// ---------- 定时压测（调度管理 + 基线对比） ----------
const schedules = ref<Array<Record<string, any>>>([])
const diffOf = ref<Record<number, any>>({})
const creatingSchedule = ref(false)
const sch = ref({
  name: '',
  intervalMinutes: 60,
  baselineTaskId: 0,
  webhookUrl: '',
  taskJson: '',
  /** 表单模板预览（当前压测表单快照派生） */
  templatePreview: ''
})

// 切到定时 Tab 时，用当前表单快照预填模板
watch(activeTab, (tab) => {
  if (tab === 'schedule' && !sch.value.taskJson) {
    const snap = stressFormRef.value?.snapshot()
    if (snap?.targetUrl) {
      sch.value.templatePreview = `${snap.method} ${snap.targetUrl} · ${snap.concurrency}并发 × ${snap.durationSec}s`
      sch.value.taskJson = JSON.stringify(snap)
    }
  }
})

async function loadSchedules() {
  try {
    const r = await scheduleApi.list()
    schedules.value = r.data || []
  } catch { /* 列表加载失败静默 */ }
}

async function createSchedule() {
  if (!sch.value.name.trim() || !sch.value.taskJson.trim()) return
  let task: Record<string, unknown>
  try {
    task = JSON.parse(sch.value.taskJson)
  } catch {
    alert('压测模板 JSON 解析失败，请检查格式')
    return
  }
  creatingSchedule.value = true
  try {
    const r = await scheduleApi.create({
      scheduleName: sch.value.name.trim(),
      intervalMinutes: sch.value.intervalMinutes,
      baselineTaskId: sch.value.baselineTaskId,
      webhookUrl: sch.value.webhookUrl.trim(),
      task
    })
    // http 拦截器已解包为 { code, message, data }（类型断言与现有视图模式一致）
    const res = r as unknown as { code: number; message: string; data: unknown }
    if (res.code === 0) {
      sch.value.name = ''
      await loadSchedules()
    } else {
      alert(res.message || '创建失败')
    }
  } finally {
    creatingSchedule.value = false
  }
}

async function toggleSchedule(s: Record<string, any>) {
  await scheduleApi.toggle(s.id, !s.enabled)
  await loadSchedules()
}

async function removeSchedule(s: Record<string, any>) {
  if (!confirm(`确认删除调度「${s.scheduleName}」？`)) return
  await scheduleApi.remove(s.id)
  await loadSchedules()
}

async function loadDiff(s: Record<string, any>) {
  try {
    const r = await scheduleApi.diff(s.id)
    diffOf.value = { ...diffOf.value, [s.id]: r.data }
  } catch { /* 对比失败静默 */ }
}
</script>
