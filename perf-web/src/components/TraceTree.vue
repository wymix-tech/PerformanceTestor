<template>
  <div>
    <div class="flex items-center gap-2 py-1" :style="{ marginLeft: depth * 16 + 'px' }">
      <div class="flex-1 flex items-center gap-2 min-w-0">
        <span class="text-xs font-mono text-fg-main truncate" :style="{ color: depthColor(depth) }">
          {{ node.method }}
        </span>
        <span class="text-[11px] text-amber-400 shrink-0">{{ node.costMs }}ms</span>
        <div class="flex-1 h-1.5 rounded bg-white/5 overflow-hidden min-w-[40px]">
          <div
            class="h-full rounded transition-all"
            :style="{ width: pct + '%', background: depthColor(depth) }"
          ></div>
        </div>
        <span class="text-[10px] text-fg-sub w-9 text-right shrink-0">{{ pct }}%</span>
      </div>
      <button v-if="hasChildren" class="text-fg-sub hover:text-neon-green text-[11px] shrink-0" @click="expanded = !expanded">
        {{ expanded ? '收起' : '展开' }}
      </button>
    </div>
    <template v-if="expanded && node.children">
      <TraceTree
        v-for="(c, idx) in node.children" :key="idx"
        :node="c" :depth="depth + 1" :parent-cost="Number(node.costMs) || 0"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  node: Record<string, any>
  depth: number
  parentCost?: number
}>()

const expanded = ref(true)
const hasChildren = computed(() => !!props.node.children?.length)

// 相对父节点耗时占比；根节点（无 parentCost）显示 100%
const pct = computed(() => {
  const self = Number(props.node.costMs) || 0
  const parent = props.parentCost || self
  if (parent <= 0) return 100
  return Math.min(Math.round((self / parent) * 100), 100)
})

const colors = ['#00E5A0', '#0EA5E9', '#34D399', '#FAAD14', '#1677FF', '#52C41A', '#F5222D']

function depthColor(d: number) {
  return colors[d % colors.length]
}
</script>
