<template>
  <div class="mt-4 p-4 rounded-lg bg-white/5 border border-white/10">
    <div class="flex items-center justify-between mb-2">
      <div class="text-xs text-fg-sub">
        历史趋势（同应用 + 同目标，最近 {{ points.length }} 次）
        <span v-if="points.length >= 2" class="ml-2 text-fg-dim">— 观察版本迭代间的性能漂移</span>
      </div>
    </div>

    <div v-if="points.length < 2" class="text-xs text-fg-sub py-6 text-center">
      该场景仅有 1 次执行记录，完成 2 次以上压测后自动显示趋势曲线
    </div>
    <div v-else class="grid grid-cols-1 xl:grid-cols-2 gap-4">
      <div>
        <div class="text-xs text-fg-sub mb-2">吞吐（QPS）</div>
        <MetricChart :option="qpsOption" height="200px" />
      </div>
      <div>
        <div class="text-xs text-fg-sub mb-2">响应时间（P95 / P99）与错误率</div>
        <MetricChart :option="rtOption" height="200px" />
      </div>
    </div>

    <!-- 明细表：便于按数值精确核对 -->
    <div v-if="points.length" class="mt-3 overflow-x-auto">
      <table class="w-full text-xs">
        <thead>
          <tr class="text-fg-sub text-[11px]">
            <th class="text-left py-1 pr-3 font-normal">#</th>
            <th class="text-left py-1 pr-3 font-normal">完成时间</th>
            <th class="text-right py-1 pr-3 font-normal">QPS</th>
            <th class="text-right py-1 pr-3 font-normal">P95(ms)</th>
            <th class="text-right py-1 pr-3 font-normal">P99(ms)</th>
            <th class="text-right py-1 font-normal">错误率</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="p in points" :key="p.taskId"
            class="border-t border-white/5"
            :class="p.taskId === currentTaskId ? 'text-neon-green' : ''"
          >
            <td class="py-1 pr-3 font-mono">
              {{ p.taskId }}
              <span v-if="p.taskId === currentTaskId" class="text-[10px] ml-1">（当前）</span>
            </td>
            <td class="py-1 pr-3 text-fg-sub">{{ shortTime(p.finishTime) }}</td>
            <td class="py-1 pr-3 text-right">{{ p.qps }}</td>
            <td class="py-1 pr-3 text-right">{{ p.p95Ms }}</td>
            <td class="py-1 pr-3 text-right">{{ p.p99Ms }}</td>
            <td class="py-1 text-right" :class="p.errorRate > 5 ? 'text-red-400' : ''">{{ p.errorRate }}%</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MetricChart from '../charts/MetricChart.vue'

/**
 * 报告历史趋势：同应用 + 同目标 URL 的多版本指标折线对比。
 *
 * <p>数据来自后端 {@code GET /api/stress/trend}，复用已有压测任务历史，
 * 与「版本对比」的差异在于：版本对比是人工挑两个任务做差值，
 * 趋势图是自动按时间轴铺开全部历史，用于观察长期漂移。
 */
const props = defineProps<{
  /** 趋势点（按完成时间正序） */
  points: Array<Record<string, any>>
  /** 当前查看的任务 ID（表格中高亮标记） */
  currentTaskId?: number
}>()

/** X 轴标签：日期 + 时分（过长会重叠，只取到分钟） */
const labels = computed(() =>
  props.points.map((p) => {
    const t = String(p.finishTime || '')
    // 后端返回 "2026-01-15T14:30:00" 或 "2026-01-15 14:30:00"
    return t.replace('T', ' ').slice(5, 16)
  })
)

const qpsOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 48, right: 16, top: 24, bottom: 28 },
  xAxis: { type: 'category', data: labels.value, axisLabel: { fontSize: 10 } },
  yAxis: { type: 'value', name: 'QPS', nameTextStyle: { fontSize: 10 } },
  series: [
    {
      name: 'QPS',
      type: 'line',
      smooth: true,
      data: props.points.map((p) => p.qps),
      itemStyle: { color: '#00f5d4' },
      areaStyle: { opacity: 0.12 }
    }
  ]
}))

const rtOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['P95', 'P99', '错误率'], textStyle: { fontSize: 10 }, top: 0 },
  grid: { left: 48, right: 48, top: 28, bottom: 28 },
  xAxis: { type: 'category', data: labels.value, axisLabel: { fontSize: 10 } },
  yAxis: [
    { type: 'value', name: 'ms', nameTextStyle: { fontSize: 10 } },
    { type: 'value', name: '%', nameTextStyle: { fontSize: 10 }, splitLine: { show: false } }
  ],
  series: [
    {
      name: 'P95',
      type: 'line',
      smooth: true,
      data: props.points.map((p) => p.p95Ms),
      itemStyle: { color: '#00bbf9' }
    },
    {
      name: 'P99',
      type: 'line',
      smooth: true,
      data: props.points.map((p) => p.p99Ms),
      itemStyle: { color: '#9b5de5' }
    },
    {
      name: '错误率',
      type: 'line',
      yAxisIndex: 1,
      smooth: true,
      data: props.points.map((p) => p.errorRate),
      itemStyle: { color: '#f15bb5' }
    }
  ]
}))

function shortTime(t: string | null) {
  if (!t) return '—'
  return String(t).replace('T', ' ').slice(5, 16)
}
</script>
