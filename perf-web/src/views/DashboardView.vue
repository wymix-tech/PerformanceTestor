<template>
  <div class="page-title">
    <LayoutDashboard class="w-5 h-5 text-neon-green" />
    总览
  </div>
  <div class="glass-card p-8 text-center text-fg-sub">
    <div class="text-4xl mb-4 animate-float">🚀</div>
    <p class="mb-2">PerformanceTestor 总览仪表盘</p>
    <p class="text-sm">关键指标卡 · JVM 总览 · 最近压测任务 · 最近慢 SQL · 最近性能问题</p>
    <p class="text-xs mt-2 text-fg-sub/60">模块骨架已就绪，图表与数据联调在 perf-web 阶段完成</p>
  </div>
</template>

<script setup lang="ts">
import { LayoutDashboard } from 'lucide-vue-next'
</script>
