<template>
  <div class="page-title">
    <Gauge class="w-5 h-5 text-neon-green" />
    压测中心
  </div>
  <div class="glass-card p-8 text-center text-fg-sub">
    <p class="mb-2">压测配置与实时结果 · 数据准备 · 容量评估与扩容建议</p>
    <p class="text-sm">实时 QPS/RT 图表与容量曲线在 perf-web 阶段完成</p>
  </div>
</template>

<script setup lang="ts">
import { Gauge } from 'lucide-vue-next'
</script>
