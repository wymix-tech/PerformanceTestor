<template>
  <div :class="mode === 'compact' ? 'space-y-4' : 'grid grid-cols-1 xl:grid-cols-2 gap-4'">
    <div class="glass-card p-5">
      <!-- 接口方案卡：由压测中心「按接口准备数据」分析生成，回显待造表/行数/证据 -->
      <div v-if="plan" ref="planCardRef" class="mb-4 rounded-lg border border-neon-blue/25 bg-neon-blue/5 overflow-hidden">
        <div class="flex items-center justify-between gap-2 px-3 py-2.5 border-b border-white/10">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="text-sm font-medium text-fg-main truncate">{{ plan.planName }}</span>
              <span v-if="planDirty" class="text-[10px] px-1.5 py-0.5 rounded bg-amber-400/10 text-amber-400 shrink-0">已手动调整</span>
            </div>
            <div class="text-[11px] text-fg-sub mt-0.5">
              来自接口分析 · {{ planRepoName }}
              <span v-if="planRtSource" class="ml-1">· RT 基线 {{ planRtSource }}</span>
            </div>
          </div>
          <button class="text-[11px] text-fg-sub hover:text-fg-main hover:underline cursor-pointer shrink-0" @click="clearPlan">清除方案</button>
        </div>

        <!-- 加载失败：保留已加载的方案数据，仅提示重试 -->
        <div v-if="planLoadError" class="px-3 py-2 border-b border-white/10 flex items-center gap-2">
          <span class="text-[11px] text-red-400">✖ {{ planLoadError }}</span>
          <button class="text-[11px] text-fg-sub hover:text-fg-main hover:underline cursor-pointer"
                  @click="retryLoadPlan">重试</button>
        </div>

        <!-- 未覆盖提示：静态分析无法解析的调用链/子查询，引导手工补表 -->
        <div v-if="planWarnings.length" class="px-3 py-2 border-b border-white/10 space-y-1">
          <div v-for="(w, wi) in planWarnings" :key="wi" class="text-[11px]" :class="warningClass(w)">
            {{ warningIcon(w) }}{{ w }}
          </div>
        </div>

        <!-- 表清单：表名 + 读写类型 + 可编辑行数 + 证据折叠 -->
        <div v-if="planTables.length" class="divide-y divide-white/5">
          <div v-for="(t, ti) in planTables" :key="ti">
            <div class="flex items-center gap-2 px-3 py-2">
              <button class="text-[10px] text-fg-sub hover:text-fg-main cursor-pointer shrink-0" @click="toggleEvidence(ti)">
                {{ expandedEvidence[ti] ? '▼' : '▶' }}
              </button>
              <span class="text-xs text-fg-main font-medium">{{ t.table }}</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded shrink-0"
                    :class="t.access === 'WRITE' ? 'bg-neon-green/15 text-neon-green' : 'bg-neon-blue/15 text-neon-blue'">
                {{ t.access === 'WRITE' ? '写' : '读' }}
              </span>
              <span v-if="t.predicateSummary" class="text-[10px] text-fg-sub truncate" :title="t.predicateSummary">
                {{ t.predicateSummary }}
              </span>
              <div class="ml-auto flex items-center gap-1 shrink-0">
                <input
                  v-model.number="t.rowCount" type="number" min="1" max="1000000"
                  class="input-text w-24 text-xs text-right" @change="markPlanDirty"
                />
                <span class="text-[10px] text-fg-sub">行</span>
              </div>
            </div>
            <div v-if="expandedEvidence[ti] && (t.evidence || []).length" class="px-3 pb-2 pl-8">
              <div class="text-[10px] text-fg-sub mb-1">推荐理由（源码位置）</div>
              <div v-for="(ev, ei) in t.evidence" :key="ei" class="text-[10px] text-fg-sub font-mono">{{ ev }}</div>
            </div>
          </div>
        </div>
        <div v-else class="px-3 py-4 text-[11px] text-center">
          <span v-if="planAnalyzing" class="text-fg-sub">正在分析源码调用链…</span>
          <span v-else-if="planFailed" class="text-red-400">
            分析失败，请查看上方告警后重试，或在下方手动选表造数
          </span>
          <span v-else class="text-fg-sub">
            该接口未分析出涉及的表，可在下方手动选表，或检查告警提示
          </span>
        </div>

        <div class="px-3 py-2 border-t border-white/10 flex items-center justify-between gap-2">
          <span class="text-[10px] text-fg-sub">确认表与行数后执行试运行，通过后即可正式灌数</span>
          <button class="btn-ghost px-3 py-1 text-[11px]" :disabled="planBusy || !planTables.length" @click="executePlan(true)">
            {{ planBusy ? '执行中…' : '试运行' }}
          </button>
          <button class="btn-secondary px-3 py-1 text-[11px]" :disabled="planBusy || !planDryRunPassed" @click="executePlan(false)">
            正式灌数
          </button>
        </div>
      </div>

      <!-- 顶部说明 + 步骤条：先把操作路径讲清楚，再展开具体表单 -->
      <div class="mb-4">
        <div class="text-sm font-medium">准备测试数据</div>
        <p class="text-[11px] text-fg-sub mt-1">
          四步完成：连接数据库 → 选择表 → 设置数量 → 校验执行。
          不填字段规则时，系统会按表结构自动生成常见类型的测试数据。
        </p>
      </div>

      <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
        <button
          v-for="(s, i) in wizardSteps" :key="s.title"
          class="rounded-lg border px-3 py-2 text-left transition-all cursor-pointer"
          :class="wizardStepClass(i + 1)"
          @click="goToStep(i + 1)"
        >
          <div class="flex items-center gap-1.5 text-xs font-medium">
            <span class="w-5 h-5 rounded-full bg-white/10 text-[10px] flex items-center justify-center">{{ i + 1 }}</span>
            {{ s.title }}
          </div>
          <div class="text-[10px] text-fg-sub mt-0.5 leading-4">{{ s.desc }}</div>
        </button>
      </div>

      <!-- Step 1：连接数据库 -->
      <section v-if="wizardStep === 1" class="space-y-2.5">
        <div>
          <label class="text-xs text-fg-sub">常用连接（可复用已保存配置，密码无需重复填写）</label>
          <select v-model="gen.connId" class="input-select w-full" @change="onConnChange">
            <option :value="0">— 手动填写连接 —</option>
            <option v-for="c in conns" :key="c.id" :value="c.id">{{ c.connName }}（{{ c.dbUrl }}）</option>
          </select>
          <p v-if="gen.connId" class="text-[11px] text-fg-sub mt-1">
            已选择保存的连接，密码由服务端解密使用
          </p>
        </div>

        <div v-if="!gen.connId" class="space-y-2.5">
          <div>
            <label class="text-xs text-fg-sub">数据库地址</label>
            <input v-model="gen.dbUrl" class="input-text w-full" placeholder="jdbc:mysql://host:3306/db" />
          </div>
          <div class="grid grid-cols-2 gap-2.5">
            <div>
              <label class="text-xs text-fg-sub">账号</label>
              <input v-model="gen.dbUser" class="input-text w-full" />
            </div>
            <div>
              <label class="text-xs text-fg-sub">密码</label>
              <input v-model="gen.dbPassword" type="password" class="input-text w-full" />
            </div>
          </div>
        </div>

        <div class="pt-1 flex items-center justify-between gap-2">
          <span class="text-[11px] text-fg-sub">下一步会自动读取这个库里的表</span>
          <button class="btn-primary px-4" :disabled="tablesLoading" @click="nextFromConn">
            {{ tablesLoading ? '读取表中…' : '下一步：选择表' }}
          </button>
        </div>
        <p v-if="tableMsg" class="text-[11px]" :class="tableMsg.ok ? 'text-neon-green' : 'text-red-400'">
          {{ tableMsg.text }}
        </p>
      </section>

      <!-- Step 2：选择表 -->
      <section v-else-if="wizardStep === 2" class="space-y-3">
        <div>
          <label class="text-xs text-fg-sub">要造数据的表（可多选，多选按表组灌数）</label>
          <div class="flex gap-2">
            <select v-model="gen.tableNames" multiple size="6" class="input-select flex-1" @change="onTablesChange">
              <option v-for="t in tables" :key="t" :value="t">{{ t }}</option>
            </select>
            <button class="btn-secondary shrink-0 px-3" :disabled="tablesLoading" @click="loadTables">
              {{ tablesLoading ? '刷新中…' : '刷新表' }}
            </button>
          </div>
          <p class="text-[10px] text-fg-sub mt-1">按住 Ctrl/Shift 可选多张表；多选时使用自动生成规则，行数按每表计算</p>
          <p v-if="tableMsg" class="text-[11px] mt-1" :class="tableMsg.ok ? 'text-neon-green' : 'text-red-400'">
            {{ tableMsg.text }}
          </p>
        </div>

        <div v-if="columnsLoading" class="text-[11px] text-fg-sub">正在读取表结构…</div>
        <div v-else-if="columnMsg" class="text-[11px]" :class="columnMsg.ok ? 'text-neon-green' : 'text-red-400'">
          {{ columnMsg.text }}
        </div>

        <div v-if="Object.keys(columnsByTable).length" class="rounded-lg border border-white/10 overflow-hidden">
          <div class="px-3 py-2 border-b border-white/10 text-[11px] text-fg-sub">
            表结构预览（系统会按这些字段生成数据；绿色代表已识别出候选值）
          </div>
          <div class="max-h-52 overflow-y-auto">
            <div v-for="(cols, tname) in columnsByTable" :key="tname">
              <div class="px-3 py-1.5 bg-white/5 border-b border-white/10 text-[11px] text-fg-main flex items-center gap-2 sticky top-0">
                {{ tname }}
                <span class="text-[10px] text-fg-sub">{{ cols.length }} 字段</span>
              </div>
              <div v-for="c in cols" :key="c.name" class="px-3 py-1.5 border-b border-white/5 text-xs text-fg-sub flex items-center gap-2">
                <span class="text-fg-main">{{ c.name }}</span>
                <span class="text-[10px]">{{ c.typeName || c.type }}</span>
                <span v-if="c.primaryKey" class="text-[10px] px-1 rounded bg-white/10">主键</span>
                <span v-if="c.autoIncrement" class="text-[10px] px-1 rounded bg-white/10">自增</span>
                <span v-if="c.suggestedValues && c.suggestedValues.length" class="text-[10px] text-neon-green">
                  候选值：{{ c.suggestedValues.join(' / ') }}
                </span>
                <span v-else-if="c.suspectEnum" class="text-[10px] text-amber-400">疑似枚举</span>
              </div>
            </div>
          </div>
        </div>

        <div v-if="suggestions.length" class="space-y-1.5">
          <div class="text-xs text-fg-sub font-medium">源码分析建议（可选采纳）</div>
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
              <button class="text-[11px] text-fg-sub hover:text-fg-main hover:underline cursor-pointer" @click="adoptHint(s.id, false)">忽略</button>
            </div>
          </div>
        </div>

        <div class="pt-1 flex items-center justify-between gap-2">
          <button class="btn-ghost px-3" @click="wizardStep = 1">上一步</button>
          <button class="btn-primary px-4" :disabled="!gen.tableNames.length && !gen.tableName" @click="wizardStep = 3">
            下一步：设置数量
          </button>
        </div>
      </section>

      <!-- Step 3：数量与规则 -->
      <section v-else-if="wizardStep === 3" class="space-y-3">
        <div class="grid grid-cols-2 gap-2.5">
          <div>
            <label class="text-xs text-fg-sub">{{ gen.tableNames.length > 1 ? '每表生成行数' : '生成行数' }}</label>
            <input v-model.number="gen.rowCount" type="number" class="input-text w-full" min="1" max="1000000" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">字段规则</label>
            <div class="input-text w-full flex items-center justify-between text-xs">
              <span>{{ gen.ruleJson ? '已自定义' : '自动生成（推荐）' }}</span>
              <button class="text-[11px] text-neon-blue hover:underline cursor-pointer" @click="fillRuleJsonExample">
                {{ gen.tableNames.length > 1 ? '填入按表示例' : '填入示例' }}
              </button>
              <button class="text-[11px] text-neon-blue hover:underline cursor-pointer" @click="advancedOpen = !advancedOpen">
                {{ advancedOpen ? '收起' : '修改' }}
              </button>
            </div>
          </div>
        </div>

        <div class="rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-[11px] text-fg-sub">
          不确定怎么填就保持「自动生成」。多选表时字段规则支持按表指定（"tables" → 表名 → "fields"，见下方「填入按表示例」）——同名同名列在不同表可配不同规则；未覆盖的列自动生成。
        </div>

        <div v-show="advancedOpen" class="space-y-2.5 border border-white/10 rounded-lg p-3">
          <div class="text-xs font-medium text-fg-sub">高级配置</div>
          <div>
            <label class="text-xs text-fg-sub">源码项目（用于识别枚举与关联，可不选）</label>
            <select v-model="gen.repoId" class="input-select w-full">
              <option :value="0">— 不使用源码分析 —</option>
              <option v-for="r in repoStore.repos" :key="r.id" :value="r.id">{{ r.repoName }}</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-fg-sub">孤儿率（0~1，通常填 0）</label>
            <input v-model.number="gen.orphanRate" type="number" step="0.01" min="0" max="1" class="input-text w-full" />
          </div>
          <div>
            <label class="text-xs text-fg-sub">字段规则(JSON)</label>
            <textarea v-model="gen.ruleJson" rows="4" class="input-text w-full font-mono text-xs"
              placeholder='{"fields":{"status":{"type":"enum","values":["PENDING","PAID"],"weights":[20,80]}}}  多表按表指定：{"tables":{"perf_user":{"fields":{"status":{...}}},"perf_order":{"fields":{"status":{...}}}}}'></textarea>
            <div v-if="skewWarn" class="text-[11px] text-amber-400 mt-1">
              检测到热点倾斜过高，建议降低批量或为子表分区
            </div>
          </div>
          <div>
            <div class="flex items-center justify-between">
              <label class="text-xs text-fg-sub">多表表组(JSON)</label>
              <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="fillTableGroupExample">填入示例</button>
            </div>
            <textarea v-model="gen.tablesJson" rows="3" class="input-text w-full font-mono text-xs"
              placeholder='[{"tableName":"user","rowCount":10000},{"tableName":"order","rowCount":50000,"fields":{"user_id":{"type":"ref","table":"user","column":"id"}}}]'></textarea>
          </div>
        </div>

        <div class="pt-1 flex items-center justify-between gap-2">
          <button class="btn-ghost px-3" @click="wizardStep = 2">上一步</button>
          <button class="btn-primary px-4" @click="wizardStep = 4">下一步：校验执行</button>
        </div>
      </section>

      <!-- Step 4：校验执行 -->
      <section v-else class="space-y-3">
        <div class="rounded-lg border border-white/10 divide-y divide-white/5">
          <div class="px-3 py-2 text-xs flex items-center justify-between">
            <span class="text-fg-sub">数据库</span>
            <span class="text-fg-main truncate max-w-[65%]" :title="gen.dbUrl">{{ gen.dbUrl || '未填写' }}</span>
          </div>
          <div class="px-3 py-2 text-xs flex items-center justify-between">
            <span class="text-fg-sub">目标表</span>
            <span class="text-fg-main truncate max-w-[65%]" :title="gen.tablesJson ? '多表表组(JSON)' : gen.tableNames.join('、')">
              {{ gen.tablesJson ? '多表表组' : (gen.tableNames.length ? gen.tableNames.join('、') : (gen.tableName || '未选择')) }}
            </span>
          </div>
          <div class="px-3 py-2 text-xs flex items-center justify-between">
            <span class="text-fg-sub">生成行数</span>
            <span class="text-fg-main">{{ gen.tablesJson ? '按表组配置' : (gen.tableNames.length > 1 ? `每表 ${gen.rowCount} 行` : gen.rowCount) }}</span>
          </div>
          <div class="px-3 py-2 text-xs flex items-center justify-between">
            <span class="text-fg-sub">字段规则</span>
            <span class="text-fg-main">{{ gen.ruleJson ? '自定义' : '自动生成' }}</span>
          </div>
          <div class="px-3 py-2 text-xs flex items-center justify-between">
            <span class="text-fg-sub">试运行</span>
            <span :class="dryRunPassed ? 'text-neon-green' : 'text-fg-sub'">
              {{ dryRunPassed ? '已通过' : '未执行' }}
            </span>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-2.5">
          <button class="btn-secondary w-full justify-center" :disabled="busy" @click="startDataGen(true)">
            {{ dryRunPassed ? '重新试运行' : '先试运行' }}
          </button>
          <button class="btn-primary w-full justify-center" :disabled="busy || !dryRunPassed" @click="startDataGen(false)">
            开始灌数据
          </button>
        </div>

        <div class="text-[11px] text-fg-sub">
          试运行只做校验，不会写库。通过后再点「开始灌数据」。若失败，右侧任务会显示具体原因。
        </div>

        <div class="flex items-center justify-between">
          <button class="btn-ghost px-3" @click="wizardStep = 3">上一步</button>
        </div>
      </section>
    </div>

    <div v-if="mode !== 'compact'" class="glass-card p-5">
      <div class="text-sm font-medium mb-3">任务状态</div>
      <div v-if="!tasks.length" class="text-xs text-fg-sub py-8 text-center">暂无任务</div>
      <div v-else class="space-y-2">
        <div v-for="t in tasks" :key="t.id" class="px-4 py-3 rounded-lg bg-white/5">
          <div class="flex items-center justify-between">
            <div class="text-sm text-fg-main">{{ t.taskName }}</div>
            <div class="flex items-center gap-2">
              <span v-if="t.tablesJson" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue">联表</span>
              <span class="text-[11px] px-2 py-0.5 rounded-full" :class="taskStatusClass(t.status)">{{ t.status }}</span>
            </div>
          </div>
          <div class="text-[11px] text-fg-sub mt-1">
            {{ taskTableLabel(t) }} · {{ t.inserted }}/{{ (t.totalRows || t.rowCount) }} 行{{ t.tablesJson ? '（表组合计）' : '' }} · {{ t.costMs }}ms
          </div>
          <div class="h-1.5 rounded bg-white/5 mt-2 overflow-hidden">
            <div class="h-full bg-neon-green rounded transition-all" :style="{ width: (t.inserted / Math.max(t.totalRows || t.rowCount, 1)) * 100 + '%' }"></div>
          </div>
          <div v-if="taskError(t)" class="text-[11px] mt-1.5 text-red-400">
            {{ taskError(t) }}
          </div>
          <div v-else-if="verifyFailedReason(t)" class="text-[11px] mt-1.5 text-amber-400">
            校验未通过：{{ verifyFailedReason(t) }}
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
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { dataGenApi, dataGenPlanApi, dbConnApi } from '../../api'
import { useRepoStore } from '../../stores/repoStore'
import { taskStatusClass } from '../../utils/format'

/**
 * 自动灌数据表单（数据准备）。
 *
 * <p>压测中心「数据准备」Tab 与快速开始向导「灌注数据」步骤共用同一份实现。
 * 前端交互改为四步向导：连接数据库 → 选择表 → 设置数量 → 校验执行；
 * 高级配置（源码项目/孤儿率/字段规则/多表表组）默认收起，避免一屏塞满技术字段。
 *
 * <p>调用方可通过 initial* 属性预填（如向导把上游步骤选择的项目与部署后
 * agent 上报探测到的数据库连接自动带入）；这些值后续变化会同步到表单，
 * 用户仍可随时手动修改。
 */
const props = withDefaults(defineProps<{
  /** 预填的源码项目 ID（0 = 不使用源码分析） */
  initialRepoId?: number
  /** 预填的数据库连接配置 ID（0 = 手动填写） */
  initialConnId?: number
  /** 预填的 JDBC URL（通常由 agent 上报探测得到） */
  initialDbUrl?: string
  initialDbUser?: string
  initialDbPassword?: string
  /**
   * 布局模式：
   * - full（默认）：两栏，左表单右任务列表（压测中心「数据准备」Tab 用）
   * - compact：单栏，仅表单，隐藏任务列表（快速开始向导内不需要，灌数结果回到压测中心查看）
   */
  mode?: 'full' | 'compact'
  /** 是否轮询刷新任务列表（压测中心需要，向导按需关闭） */
  poll?: boolean
  /** 接口驱动数据方案 ID（压测中心「按接口准备数据」生成后传入，回显待造表与行数） */
  initialPlanId?: number
}>(), {
  initialRepoId: 0,
  initialConnId: 0,
  initialDbUrl: '',
  initialDbUser: '',
  initialDbPassword: '',
  mode: 'full',
  poll: false,
  initialPlanId: 0
})

const emit = defineEmits(['created', 'plan-cleared'])

const repoStore = useRepoStore()
const conns = ref<Array<Record<string, any>>>([])
const tables = ref<string[]>([])
/** 多选逐表列结构：表名 → 列清单（单选时仅一项） */
const columnsByTable = ref<Record<string, Array<Record<string, any>>>>({})
/** 表清单/列结构的读取中与结果反馈（后端业务错误以 HTTP 200 返回，需显式呈现） */
const tablesLoading = ref(false)
const columnsLoading = ref(false)
const tableMsg = ref<{ ok: boolean; text: string } | null>(null)
const columnMsg = ref<{ ok: boolean; text: string } | null>(null)
const suggestions = ref<Array<Record<string, any>>>([])
const tasks = ref<Array<Record<string, any>>>([])
const busy = ref(false)
const dryRunPassed = ref(false)

const gen = ref({
  connId: props.initialConnId || 0,
  dbUrl: props.initialDbUrl || '',
  dbUser: props.initialDbUser || 'root',
  dbPassword: props.initialDbPassword || '',
  tableName: '',
  tableNames: [] as string[],
  rowCount: props.initialDbUrl ? 10000 : 1000,
  ruleJson: '',
  tablesJson: '',
  orphanRate: 0,
  repoId: props.initialRepoId || 0
})

/** 表选择变化：单选时同步 tableName（列预览/单表形态提交），多选清空单表名 */
function onTablesChange() {
  gen.value.tableName = gen.value.tableNames.length === 1 ? gen.value.tableNames[0] : ''
  loadColumns()
}

/** 高级选项是否展开（仅源码项目/孤儿率/JSON 规则：默认收起，避免向导窄空间内表单过高） */
const advancedOpen = ref(false)

// ---------- 四步向导状态：用户能明确知道当前在哪一步、下一步是什么 ----------
const wizardStep = ref(1)
const wizardSteps = [
  { title: '连接数据库', desc: '选择常用连接或手动填写' },
  { title: '选择表', desc: '读取目标库并选择表' },
  { title: '设置数量', desc: '行数与可选规则' },
  { title: '校验执行', desc: '试运行通过后灌数' }
]

function wizardStepClass(step: number) {
  return step === wizardStep.value
    ? 'border-neon-green/40 bg-neon-green/10 text-neon-green'
    : 'border-white/10 text-fg-main hover:border-white/20'
}

/** 返回步骤随时可点；前进步骤要求前置条件已完成 */
function goToStep(step: number) {
  if (step === 1) {
    wizardStep.value = 1
    return
  }
  if (step >= 2 && !gen.value.connId && !gen.value.dbUrl) return
  if (step >= 3 && !gen.value.tableNames.length && !gen.value.tableName && !gen.value.tablesJson) return
  wizardStep.value = step
}

/** Step 1 → Step 2：先读表，成功后再进入下一步 */
async function nextFromConn() {
  if (!gen.value.connId && !gen.value.dbUrl) {
    tableMsg.value = { ok: false, text: '请先选择常用连接或填写数据库地址' }
    return
  }
  await loadTables()
  if (tables.value.length) wizardStep.value = 2
}

/** 选中常用连接后自动补连接参数，并顺手刷新表清单 */
async function onConnChange() {
  applyConn()
  tableMsg.value = null
  if (gen.value.connId || gen.value.dbUrl) await loadTables()
}

// ---------- 接口驱动数据方案（压测中心「按接口准备数据」分析产出） ----------
const plan = ref<Record<string, any> | null>(null)
const planCardRef = ref<HTMLElement | null>(null)
const planBusy = ref(false)
const planDirty = ref(false)
const planDryRunPassed = ref(false)
/** 证据折叠展开状态：表下标 → 是否展开 */
const expandedEvidence = ref<Record<number, boolean>>({})
/** 分析异步完成前的轮询定时器 */
let planPollTimer: ReturnType<typeof setInterval> | null = null
/** 方案加载中标志：防止轮询与用户操作并发导致请求叠加 */
const planLoading = ref(false)
/** 方案详情加载失败提示（轮询抖动时保留已加载数据，仅提示重试） */
const planLoadError = ref('')

/** 方案涉及的表（含行数与证据；行数可编辑） */
const planTables = ref<Array<Record<string, any>>>([])

/** 方案告警（降级/未覆盖/超限等） */
const planWarnings = computed<string[]>(() => {
  const raw = plan.value?.warningsJson
  if (!raw) return []
  try {
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.map(String) : []
  } catch {
    return []
  }
})

/** 方案所属项目名（卡片副标题展示） */
const planRepoName = computed(() => {
  const rid = Number(plan.value?.repoId || 0)
  const r = repoStore.repos.find((x) => x.id === rid)
  return r ? r.repoName : '未关联项目'
})

/** RT 基线取值来源（历史压测/采样点/默认），让用户知道行数是不是精算值 */
const planRtSource = computed(() => {
  const hit = planWarnings.value.find((w) => w.includes('响应时间基线取值来源'))
  if (!hit) return ''
  const m = hit.match(/来源:\s*([^（]+)/)
  return m ? m[1].trim() : ''
})

/**
 * 方案仍在分析中：以服务端状态为准（pending = 分析中/待采纳）。
 *
 * <p>此前只靠「表清单为空且无告警」推断，与「分析失败」无法区分——分析抛异常时
 * 后端现已置 failed，这里按状态判定即可立即给出结论，不必干等到轮询超时。</p>
 */
const planAnalyzing = computed(() =>
  !!plan.value && plan.value.status === 'pending'
  && !planTables.value.length && planWarnings.value.length === 0)

/** 分析失败（后端异步分析抛异常后置 failed） */
const planFailed = computed(() => !!plan.value && plan.value.status === 'failed')

/**
 * 告警分级着色：分析失败（红色，阻断）/ RT 基线来源（弱化，纯说明）/ 其余降级告警（黄色）。
 *
 * <p>不加区分时，「分析失败」混在一堆黄色降级提示里，用户会当成可忽略的告警。</p>
 */
function warningClass(w: string): string {
  if (w.startsWith('分析失败')) return 'text-red-400'
  if (w.includes('响应时间基线')) return 'text-fg-sub'
  return 'text-amber-400'
}

function warningIcon(w: string): string {
  if (w.startsWith('分析失败')) return '✖ '
  if (w.includes('响应时间基线')) return 'ⓘ '
  return '⚠ '
}

/** 谓词摘要：把字段规则里的枚举/范围/seq 规则压缩成一行可读文本 */
function predicateSummaryOf(fields: any): string {
  try {
    const f = typeof fields === 'string' ? JSON.parse(fields) : fields
    if (!f || typeof f !== 'object') return ''
    const parts: string[] = []
    for (const [col, rule] of Object.entries<any>(f || {})) {
      if (!rule || typeof rule !== 'object') continue
      if (rule.type === 'enum' && Array.isArray(rule.values)) {
        parts.push(`${col}=${rule.values.slice(0, 3).join(',')}${rule.values.length > 3 ? '…' : ''}`)
      } else if (rule.type === 'range') {
        parts.push(`${col}∈[${rule.min},${rule.max}]`)
      } else if (rule.type === 'seq') {
        parts.push(`${col}=序号`)
      } else if (rule.type === 'ref' || rule.type === 'refSkew') {
        parts.push(`${col}→${rule.table}`)
      }
    }
    return parts.slice(0, 3).join(' ')
  } catch {
    return ''
  }
}

/**
 * 拉取并应用方案详情。
 *
 * <p><b>不触碰轮询定时器</b>——轮询回调必须走这里而不是 {@code loadPlan}：
 * 后者入口会先 {@code stopPlanPoll()}，把正在执行回调的定时器自身清掉，
 * 而回调内 poll=false 又不会重建，结果是只额外拉一次（2s 处）后轮询静默停止，
 * 分析耗时 10s+ 时用户一直卡在空态。</p>
 */
async function fetchPlan(id: number) {
  // 防重入：轮询的下一拍在上一次请求未返回时不得再发，
  // 否则慢响应会让请求叠加、后到的旧响应覆盖新数据
  if (planLoading.value) return
  planLoading.value = true
  try {
    const r = await dataGenPlanApi.detail(id)
    plan.value = r.data || null
    const raw = plan.value?.tablesJson
    let tables: Array<Record<string, any>> = []
    if (raw) {
      try {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed)) tables = parsed
      } catch { /* 非法 JSON 视作无表 */ }
    }
    planTables.value = tables.map((t) => ({
      table: t.table,
      rowCount: Number(t.rowCount) || 1000,
      access: t.access || 'READ',
      evidence: Array.isArray(t.evidence) ? t.evidence : [],
      fields: t.fields,
      predicateSummary: predicateSummaryOf(t.fields)
    }))
    // 方案带项目时同步到表单的源码项目（保证后续灌数走同一份源码分析）
    if (plan.value?.repoId) gen.value.repoId = plan.value.repoId
    expandedEvidence.value = {}
    planLoadError.value = ''
  } catch {
    // 已加载成功过就保留现有数据：轮询期间的瞬时网络抖动/超时若把整个方案卡清空，
    // 用户会被迫回压测中心重新进入——损失的远比一次拉取失败大
    if (!plan.value) {
      plan.value = null
      planTables.value = []
    }
    planLoadError.value = '方案加载失败，请重试'
  } finally {
    planLoading.value = false
  }
}

/** 加载方案详情；分析为异步，表清单为空时轮询直到产出或超时 */
async function loadPlan(id: number, poll = false) {
  stopPlanPoll()
  await fetchPlan(id)

  // 已终结（failed）就不再轮询：分析失败不会自行恢复，继续轮询纯属空转
  if (poll && plan.value?.status === 'pending'
      && !planTables.value.length && planWarnings.value.length === 0) {
    // 分析未完成：每 2s 拉一次，最多 30s，超时后停止（此时会显示告警或空态提示）
    let waited = 0
    planPollTimer = setInterval(async () => {
      waited += 2000
      if (waited >= 30000) return stopPlanPoll()
      await fetchPlan(id)
      if (plan.value?.status !== 'pending'
          || planTables.value.length || planWarnings.value.length) stopPlanPoll()
    }, 2000)
  }
}

function stopPlanPoll() {
  if (planPollTimer) {
    clearInterval(planPollTimer)
    planPollTimer = null
  }
}

/** 手动重试加载（失败后保留着方案卡，用户可就地重试） */
function retryLoadPlan() {
  if (!plan.value?.id) return
  loadPlan(Number(plan.value.id), plan.value.status === 'pending')
}

function toggleEvidence(idx: number) {
  expandedEvidence.value[idx] = !expandedEvidence.value[idx]
}

/**
 * 标记方案被手动调整。
 *
 * <p>必须同时作废试运行结果：试运行校验的是<b>当时的</b>参数，改完行数后
 * 「正式灌数」按钮仍可点，等于用旧参数的校验结论放行新参数——而行数正是
 * 影响灌数规模与执行时长的核心参数。</p>
 */
function markPlanDirty() {
  planDirty.value = true
  planDryRunPassed.value = false
}

/** 清除方案：退回纯手动模式，并通知父组件清掉 activePlanId */
function clearPlan() {
  stopPlanPoll()
  plan.value = null
  planTables.value = []
  planDirty.value = false
  planDryRunPassed.value = false
  planLoadError.value = ''
  emit('plan-cleared')
}

/**
 * 按方案执行灌数。
 *
 * <p>方案若未指定连接（分析阶段允许留空），则先落库回填当前表单选中的连接——
 * 后端在真正执行时才做强校验，这样用户在数据准备页选定连接即可直接跑。</p>
 */
async function executePlan(dryRun: boolean) {
  if (!plan.value?.id || !planTables.value.length) return
  if (!gen.value.dbUrl) {
    alert('请先在下面选择数据库连接，再执行方案')
    return
  }
  planBusy.value = true
  try {
    // connId 与 dbUrl 二选一：分析阶段允许留空，执行时按当前表单选定值传入
    const conn: Record<string, unknown> = {}
    if (gen.value.connId) {
      conn.connId = gen.value.connId
    } else {
      conn.dbUrl = gen.value.dbUrl
    }
    // 用户在方案卡上改过的行数必须回传，否则执行的是落库的旧行数，
    // 「已手动调整」角标就成了误导
    if (planDirty.value) {
      conn.rowOverrides = planTables.value.map((t) => ({ table: t.table, rowCount: t.rowCount }))
    }
    await dataGenPlanApi.execute(plan.value.id, dryRun, conn)
    if (dryRun) planDryRunPassed.value = true
    await loadTasks()
  } catch (e: any) {
    // 执行失败必须显式反馈：连接校验不过、方案状态异常等场景下
    // 若吞掉异常，用户点击后毫无反应，会误以为正在执行
    alert(e?.response?.data?.message || e?.message || (dryRun ? '方案试运行失败' : '方案正式灌数失败'))
  } finally {
    planBusy.value = false
  }
}

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

/** 任务失败原因：errorMsg 优先，回退 detailJson.errorMsg（避免失败任务只显示红条没原因） */
function taskError(t: Record<string, any>): string {
  if (t.errorMsg) return String(t.errorMsg)
  const detail = parseDetail(t.detailJson || '')
  return detail?.errorMsg ? String(detail.errorMsg) : ''
}

/** 任务卡片表名：表组从 tablesJson 展开全部表名（后端 tableName 只保留首表），
 * 避免多表任务被误读成单表行数（如「perf_order · 0/2000 行」）。 */
function taskTableLabel(t: Record<string, any>): string {
  if (t.tablesJson) {
    try {
      const arr = JSON.parse(t.tablesJson)
      if (Array.isArray(arr)) {
        const names = arr.map((x: any) => x?.tableName).filter(Boolean)
        if (names.length > 2) return `${names.slice(0, 2).join('、')} 等 ${names.length} 表`
        if (names.length) return names.join('、')
      }
    } catch { /* 解析失败退回后端首表名 */ }
  }
  return t.tableName || '未命名表'
}

/**
 * 灌后校验失败原因：数据已写入，但 verifyMin 任一检查未通过时
 * 把具体检查项压成一行可读文本，避免只看到状态徽章却不知道差在哪里。
 */
function verifyFailedReason(t: Record<string, any>): string {
  if (t.status !== 'finished_verify_failed') return ''
  const detail = parseDetail(t.detailJson || '')
  const checks: Array<Record<string, any>> = detail?.verifyMin?.checks || []
  const parts: string[] = []
  for (const c of checks) {
    if (c?.ok !== false) continue
    if (c.kind === 'row_count') {
      if (c.mode === 'increment') {
        parts.push(`本次新增行数核对：期望 ${c.expected} 行，实际 ${c.actual} 行`)
      } else {
        parts.push(`行数核对：期望 ${c.expected} 行，实际 ${c.actual} 行`)
      }
    } else if (c.kind === 'fk_orphan') {
      parts.push(`外键孤儿：${c.column} → ${c.parent}，抽检 ${c.sampled} 行发现 ${c.orphans} 条孤儿`)
    } else if (c.kind === 'enum_legal') {
      parts.push(`枚举合法率：${c.column} 仅 ${(Number(c.legalRate) * 100).toFixed(0)}% 合法`)
    } else if (c.kind) {
      parts.push(String(c.kind))
    }
  }
  return parts.join('；')
}

/** 字段规则示例：单表全局形态；多选时按当前选中表生成 tables 按表骨架
 * （同名列在不同表可配不同规则——后端按表名路由，显式规则优先）。 */
function fillRuleJsonExample() {
  const enumRule = { type: 'enum', values: ['PENDING', 'PAID'], weights: [20, 80] }
  const names = gen.value.tableNames.length
    ? gen.value.tableNames
    : (gen.value.tableName ? [gen.value.tableName] : [])
  if (names.length > 1) {
    gen.value.ruleJson = JSON.stringify({
      tables: Object.fromEntries(names.map((t) => [t, { fields: { status: { ...enumRule } } }]))
    }, null, 0)
  } else {
    gen.value.ruleJson = JSON.stringify({ fields: { status: { ...enumRule } } })
  }
}

function fillTableGroupExample() {
  gen.value.tablesJson = JSON.stringify([
    { tableName: gen.value.tableName || 'user', rowCount: 10000 },
    { tableName: 'order', rowCount: 50000, fields: { user_id: { type: 'ref', table: 'user', column: 'id' } } }
  ], null, 0)
}

/** 连接参数：选中已保存连接时带 connId，由服务端解析加密凭据 */
function connParams() {
  return {
    connId: gen.value.connId || undefined,
    dbUrl: gen.value.dbUrl,
    dbUser: gen.value.dbUser,
    dbPassword: gen.value.dbPassword
  }
}

/**
 * 解包后端统一响应 { code, message, data }。
 *
 * <p>关键点：后端业务异常（JDBC URL 非法、连库失败等）经 GlobalExceptionHandler
 * 处理后仍以 HTTP 200 返回，仅 code != 0、data 为 null。axios 拦截器只识别 HTTP
 * 状态码，不会 reject，故必须在此显式判定 code，否则前端表现为「点击无响应」。
 */
function unwrap<T>(res: any): { ok: boolean; data: T | null; message: string } {
  const code = res?.code
  const message = typeof res?.message === 'string' ? res.message : ''
  if (typeof code === 'number' && code !== 0) {
    return { ok: false, data: null, message: message || '请求失败' }
  }
  return { ok: true, data: (res?.data ?? null) as T | null, message }
}

async function loadTables() {
  // 连接参数缺失时前端直接拦截，避免发出必然失败且无反馈的请求
  if (!gen.value.connId && !gen.value.dbUrl) {
    tables.value = []
    tableMsg.value = { ok: false, text: '请先选择常用连接或填写数据库地址' }
    return
  }
  tablesLoading.value = true
  tableMsg.value = null
  try {
    const { ok, data, message } = unwrap<string[]>(await dataGenApi.tables(connParams()))
    if (!ok) {
      tables.value = []
      tableMsg.value = { ok: false, text: message }
      return
    }
    tables.value = data || []
    tableMsg.value = tables.value.length
      ? { ok: true, text: `已读取 ${tables.value.length} 张表` }
      : { ok: false, text: '目标库中未读取到表，请确认库名与账号权限' }
  } catch (e) {
    tables.value = []
    tableMsg.value = { ok: false, text: (e as { message?: string })?.message || '读取表清单失败' }
  } finally {
    tablesLoading.value = false
  }
}

async function loadColumns() {
  // 多选逐表读取（顺序请求，目标库压力最小）；单选/表组 JSON 时仅当前表
  const targets = gen.value.tableNames.length
    ? [...gen.value.tableNames]
    : (gen.value.tableName ? [gen.value.tableName] : [])
  if (!targets.length) return
  columnsLoading.value = true
  columnMsg.value = null
  const result: Record<string, Array<Record<string, any>>> = {}
  let firstError = ''
  for (const t of targets) {
    try {
      const { ok, data, message } = unwrap<Array<Record<string, any>>>(
        await dataGenApi.columns({ ...connParams(), tableName: t }))
      if (!ok) {
        firstError = firstError || `${t}: ${message}`
        continue
      }
      result[t] = data || []
    } catch (e) {
      firstError = firstError || `${t}: ${(e as { message?: string })?.message || '读取列结构失败'}`
    }
  }
  columnsByTable.value = result
  const total = Object.values(result).reduce((n, cs) => n + cs.length, 0)
  columnMsg.value = total
    ? { ok: true, text: `已读取 ${Object.keys(result).length} 张表共 ${total} 个字段` }
    : { ok: false, text: firstError || '未读取到列结构，请确认表名与账号权限' }
  columnsLoading.value = false
  loadSuggestions()
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
  try {
    await dataGenApi.adopt(id, adopt)
    await loadSuggestions()
  } catch { /* 采纳失败不阻断 */ }
}

async function loadTasks() {
  try {
    const r = await dataGenApi.list()
    tasks.value = r.data || []
  } catch { /* 加载失败不阻断 */ }
}

async function loadConns() {
  try {
    const r = await dbConnApi.list()
    conns.value = r.data || []
  } catch { /* 连接配置失败不阻断 */ }
}

/**
 * 选择连接配置后自动填连接参数（仍可手改）。
 * 密码字段刻意留空：凭据已加密存储、列表仅返回脱敏值，
 * 真实密码由服务端按 connId 解析（见 DataGenController.applyConn）。
 */
function applyConn() {
  const c = conns.value.find((x) => x.id === gen.value.connId)
  if (c) {
    gen.value.dbUrl = c.dbUrl
    gen.value.dbUser = c.username
    gen.value.dbPassword = ''
  }
}

async function startDataGen(dryRun: boolean) {
  let tables
  if (gen.value.tablesJson && gen.value.tablesJson.trim()) {
    try {
      tables = JSON.parse(gen.value.tablesJson)
    } catch {
      alert('多表表组 JSON 解析失败，请检查格式')
      return
    }
  } else if (gen.value.tableNames.length > 1) {
    // 多选表 → 每表统一行数的表组形态（后端 tables 数组）
    tables = gen.value.tableNames.map((t) => ({ tableName: t, rowCount: gen.value.rowCount }))
  }
  if (!gen.value.connId && !gen.value.dbUrl) {
    alert('请先选择常用连接或填写数据库地址')
    return
  }
  if (!tables && !gen.value.tableName && !gen.value.tableNames.length) {
    alert('请先选择要造数据的表')
    return
  }
  busy.value = true
  try {
    const r = await dataGenApi.create({
      taskName: `${dryRun ? '试运行' : '灌数据'}-${tables ? tables.map((t: any) => t.tableName).filter(Boolean).join('、') : (gen.value.tableName || gen.value.tableNames[0])}`,
      connId: gen.value.connId || undefined,
      dbUrl: gen.value.dbUrl,
      dbUser: gen.value.dbUser,
      dbPassword: gen.value.dbPassword,
      tableName: gen.value.tableNames[0] || gen.value.tableName,
      rowCount: gen.value.rowCount,
      ruleJson: gen.value.ruleJson || undefined,
      tables,
      orphanRate: gen.value.orphanRate || undefined,
      repoId: gen.value.repoId || undefined,
      dryRun
    })
    if (dryRun) dryRunPassed.value = true
    await loadTasks()
    emit('created', { task: r.data, dryRun })
  } finally {
    busy.value = false
  }
}

// 关键参数变化后，旧的试运行结论不能再作为放行依据
watch(() => [
  gen.value.connId, gen.value.dbUrl, gen.value.tableName, gen.value.rowCount,
  gen.value.ruleJson, gen.value.tablesJson, gen.value.orphanRate, gen.value.repoId
], () => {
  dryRunPassed.value = false
})

// 上游预填值变化（如向导中用户回退改选项目、或部署后探测到数据库）→ 同步到表单
watch(() => [props.initialRepoId, props.initialConnId, props.initialDbUrl], () => {
  if (props.initialRepoId) gen.value.repoId = props.initialRepoId
  if (props.initialConnId) {
    gen.value.connId = props.initialConnId
    applyConn() // 连接列表可能刚加载完，此时补填 URL/用户/密码
  } else if (props.initialDbUrl) {
    gen.value.dbUrl = props.initialDbUrl
  }
}, { immediate: true })

// 连接列表加载完成后，若已预选连接则自动填充连接参数
watch(conns, () => {
  if (gen.value.connId && !gen.value.dbUrl) applyConn()
})

// 方案 ID 变化（压测中心分析完成跳入本页）→ 加载方案详情并轮询至分析产出
watch(() => props.initialPlanId, (id) => {
  stopPlanPoll()
  planDirty.value = false
  planDryRunPassed.value = false
  if (id && id > 0) {
    loadPlan(id, true)
  } else {
    plan.value = null
    planTables.value = []
  }
}, { immediate: true })

let pollTimer: ReturnType<typeof setInterval> | null = null
function startPolling() {
  stopPolling()
  if (!props.poll) return
  pollTimer = setInterval(loadTasks, 3000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(async () => {
  await Promise.all([loadConns(), repoStore.load().catch(() => {}), loadTasks()])
  if (gen.value.connId && !gen.value.dbUrl) applyConn()
  startPolling()
})

onUnmounted(() => {
  stopPolling()
  stopPlanPoll()
})

defineExpose({ loadTasks, gen, loadPlan })
</script>
