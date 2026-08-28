<template>
  <div>
    <div class="flex items-center gap-2 py-1">
      <!-- 左侧标签区：固定宽度，层级用缩进体现，保证右侧时间轴所有行严格对齐 -->
      <div class="w-[46%] shrink-0 flex items-center gap-2 min-w-0">
        <span
          class="text-xs font-mono text-fg-main truncate"
          :style="{ color: depthColor(depth), paddingLeft: depth * 14 + 'px' }"
        >{{ node.method }}</span>
        <span class="text-[11px] text-amber-400 shrink-0">{{ fmtMs(node.costMs) }}ms</span>
      </div>
      <!-- 右侧时间轴：以根节点总耗时为 100%，条 left=开始偏移、width=耗时占比（甘特图），体现调用顺序 -->
      <div class="flex-1 flex items-center gap-2 min-w-0">
        <div class="relative flex-1 h-2 rounded bg-white/5 min-w-[60px]" :title="tooltip">
          <div
            class="absolute top-0 h-full rounded transition-all"
            :style="{ left: leftPct + '%', width: barWidth + '%', background: depthColor(depth) }"
          ></div>
        </div>
        <span class="text-[10px] text-fg-sub w-12 text-right shrink-0">{{ pctText }}</span>
        <!-- 所有行保留等宽的按钮位（无子节点时占位），保证时间轴轨道物理宽度一致、右端严格对齐 -->
        <span class="w-7 shrink-0 inline-flex justify-end">
          <button
            v-if="hasChildren"
            class="text-fg-sub hover:text-neon-green text-[11px]"
            @click="expanded = !expanded"
          >{{ expanded ? '收起' : '展开' }}</button>
        </span>
      </div>
    </div>
    <template v-if="expanded && node.children">
      <TraceTree
        v-for="(c, idx) in node.children" :key="idx"
        :node="c" :depth="depth + 1" :root-cost="rootCost"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  node: Record<string, any>
  depth: number
  /** 根节点总耗时 ms：整棵树统一时间轴的 100% 基准 */
  rootCost?: number
}>()

const expanded = ref(true)
const hasChildren = computed(() => !!props.node.children?.length)

// 时间轴基准：根节点耗时（根节点自身未传时取自身 costMs）
const total = computed(() => {
  const rc = Number(props.rootCost)
  if (rc > 0) return rc
  return Number(props.node.costMs) || 0
})

// 开始偏移：相对根节点开始时刻（旧数据无 offsetMs 字段时回退为 0，即与父节点起点对齐）
const offsetMs = computed(() => {
  const o = Number(props.node.offsetMs)
  return Number.isFinite(o) && o > 0 ? o : 0
})

const costMs = computed(() => Number(props.node.costMs) || 0)

// 条起点 = 开始偏移占比；条长度 = 耗时 / 根总耗时
const leftPct = computed(() => {
  if (total.value <= 0) return 0
  return Math.min((offsetMs.value / total.value) * 100, 99.5)
})

const barWidth = computed(() => {
  if (total.value <= 0) return 100
  // 最小可见宽度，避免极短调用画不出来
  return Math.max(Math.min((costMs.value / total.value) * 100, 100 - leftPct.value), 0.5)
})

// 占根节点总耗时百分比
const pctText = computed(() => {
  if (total.value <= 0) return '100%'
  return Math.round((costMs.value / total.value) * 100) + '%'
})

const tooltip = computed(() =>
  `开始于 +${offsetMs.value.toFixed(2)}ms · 耗时 ${costMs.value.toFixed(3)}ms · 占根节点 ${pctText.value}`
)

function fmtMs(v: number) {
  const n = Number(v) || 0
  return n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)
}

const colors = ['#00E5A0', '#0EA5E9', '#34D399', '#FAAD14', '#1677FF', '#52C41A', '#F5222D']

function depthColor(d: number) {
  return colors[d % colors.length]
}
</script>
