<template>
  <div class="page-title">
    <GitBranch class="w-5 h-5 text-neon-green" />
    调用链分析
  </div>
  <div class="glass-card p-8 text-center text-fg-sub">
    <p class="mb-2">业务方法调用链耗时与占比 · 慢 SQL 列表</p>
    <p class="text-sm">调用树嵌套表格与火焰图在 perf-web 阶段完成</p>
  </div>
</template>

<script setup lang="ts">
import { GitBranch } from 'lucide-vue-next'
</script>
