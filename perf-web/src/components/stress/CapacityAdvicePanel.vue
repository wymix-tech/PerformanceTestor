<template>
  <div class="grid grid-cols-1 xl:grid-cols-2 gap-4">
    <div class="glass-card p-5">
      <div class="text-sm font-medium mb-3 flex items-center justify-between">
        <span>扩容建议（基于监控数据）</span>
        <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="loadAdvice">刷新</button>
      </div>
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
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { capacityApi } from '../../api'

/**
 * 容量建议面板：扩容建议 + JVM 配置建议。
 * 从 StressCenterView 拆出，行为与视觉保持不变。
 */
const advice = ref<Record<string, any> | null>(null)

async function loadAdvice() {
  const r = await capacityApi.advice()
  advice.value = r.data
}

function riskClass(r: string) {
  return r === 'high'
    ? 'border-red-400/30 bg-red-400/5 text-red-300'
    : r === 'medium'
      ? 'border-amber-400/30 bg-amber-400/5 text-amber-300'
      : 'border-neon-green/30 bg-neon-green/5 text-neon-green'
}

onMounted(loadAdvice)
defineExpose({ loadAdvice })
</script>
