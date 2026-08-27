<template>
  <div class="w-full overflow-x-auto" :style="{ height: height }">
    <div class="min-w-[800px]">
      <!-- 调用链火焰图：按耗时占比渲染色块（矩形树图），支持点击下钻 -->
      <div class="flex items-end gap-px" style="height: 100%">
        <div
          v-for="(node, i) in flatNodes"
          :key="i"
          class="flex flex-col justify-center items-center text-center text-[11px] cursor-pointer transition-all hover:brightness-125 border border-white/10"
          :style="{
            width: node.ratio + '%',
            height: depthFactor(node) + '%',
            background: colorOf(node)
          }"
          :title="`${labelOf(node)} · ${node.costMs}ms (${node.ratio}%)`"
          @click="$emit('node-click', node)"
        >
          <span class="truncate w-full px-1">{{ labelOf(node) }}</span>
        </div>
      </div>
      <div class="text-[11px] text-fg-sub mt-1">耗时占比火焰图：宽度=耗时占比，颜色深度=调用层级，点击色块下钻</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export interface FlameNode {
  method: string
  costMs: number
  ratio: number
  depth: number
  children?: FlameNode[]
}

const props = defineProps<{
  tree: FlameNode | null
  height?: string
}>()
defineEmits<{ (e: 'node-click', node: FlameNode): void }>()

// 扁平化节点用于渲染
const flatNodes = computed<FlameNode[]>(() => {
  if (!props.tree) return []
  return flatten(props.tree, 0)
})

function flatten(node: FlameNode, depth: number): FlameNode[] {
  const self = { ...node, depth }
  if (!node.children?.length) return [self]
  return [self, ...node.children.flatMap((c) => flatten(c, depth + 1))]
}

function labelOf(node: FlameNode): string {
  const parts = node.method.split('#')
  return parts.length > 1 ? parts[parts.length - 1] : node.method
}

function depthFactor(node: FlameNode): number {
  // 层级越深色块越矮，突出根节点
  return Math.max(20, 100 - node.depth * 18)
}

function colorOf(node: FlameNode): string {
  // 耗时越长越偏红，层级越深越偏蓝
  const hot = Math.min(node.ratio / 100, 1)
  const r = Math.round(20 + hot * 200)
  const g = Math.round(30 + (1 - hot) * 180)
  const b = Math.round(60 + node.depth * 30)
  return `rgba(${r}, ${g}, ${b}, 0.85)`
}
</script>
