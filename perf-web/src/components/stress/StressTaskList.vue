<template>
  <div class="glass-card p-5 xl:col-span-2">
    <div class="text-sm font-medium mb-3 flex items-center justify-between">
      <span>最近任务</span>
      <button class="text-xs text-neon-blue hover:underline cursor-pointer" @click="loadTasks">刷新</button>
    </div>
    <div v-if="!tasks.length" class="text-xs text-fg-sub py-8 text-center">暂无压测任务</div>
    <div v-else class="space-y-2 max-h-[430px] overflow-y-auto pr-1">
      <div
        v-for="t in tasks" :key="t.id"
        data-test="task-row"
        class="px-4 py-3 rounded-lg bg-white/5 hover:bg-white/10 transition-all cursor-pointer"
        @click="emit('select', t.id)"
      >
        <div class="flex items-center justify-between">
          <div class="text-sm text-fg-main truncate">{{ t.taskName || t.targetUrl }}</div>
          <span class="flex items-center gap-2 shrink-0">
            <span v-if="t.appName" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-blue/10 text-neon-blue">{{ t.appName }}</span>
            <span v-if="t.appVersion" class="text-[10px] px-1.5 py-0.5 rounded bg-neon-purple/10 text-neon-purple">v{{ t.appVersion }}</span>
            <span class="text-[11px] px-2 py-0.5 rounded-full" :class="taskStatusClass(t.status)">{{ t.status }}</span>
          </span>
        </div>
        <div class="flex items-center justify-between mt-2 text-[11px] text-fg-sub">
          <span>{{ t.concurrency }} 并发 · {{ t.durationSec }}s · {{ t.engineType }}</span>
          <span v-if="t.finishTime">{{ fmtTime(t.finishTime) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { stressApi } from '../../api'
import { taskStatusClass, fmtTime } from '../../utils/format'

/**
 * 压测任务列表：轮询刷新 + 点击查看报告。
 * 从 StressCenterView 拆出，行为与视觉保持不变。
 */
const emit = defineEmits<{ (e: 'select', taskId: number): void }>()

const tasks = ref<Array<Record<string, any>>>([])
let pollTimer: ReturnType<typeof setInterval> | null = null

/**
 * 拉取任务列表。
 *
 * <p>轮询场景下请求失败必须就地吞掉：否则会产生未处理的 Promise 拒绝，
 * 在网络抖动或平台重启期间刷屏控制台，且中断后续轮询。
 */
async function loadTasks() {
  try {
    const r = await stressApi.list({ limit: 20 })
    tasks.value = r.data || []
  } catch {
    // 保留上一次数据，下个周期自动重试
  }
}

/** 供父组件在发起压测后立即刷新列表 */
function refresh() {
  return loadTasks()
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => { loadTasks() }, 3000)
}
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(() => {
  loadTasks()
  startPolling()
})
onUnmounted(stopPolling)

// 暴露任务数据：父组件的版本对比候选列表依赖它
defineExpose({ loadTasks, refresh, tasks })
</script>
