<template>
  <div class="page-title">
    <Cpu class="w-5 h-5 text-neon-green" />
    JVM 监控
  </div>
  <div class="glass-card p-8 text-center text-fg-sub">
    <p class="mb-2">JVM 指标监控（堆/非堆内存 · GC · 线程 · CPU）</p>
    <p class="text-sm">时序图表与指标卡在 perf-web 阶段完成</p>
  </div>
</template>

<script setup lang="ts">
import { Cpu } from 'lucide-vue-next'
</script>
