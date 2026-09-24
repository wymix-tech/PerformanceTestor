<template>
  <div class="min-h-screen flex flex-col items-center justify-center bg-bg-base px-4 relative overflow-hidden">
    <!-- 背景光晕（纯装饰，aria-hidden 避免读屏播报） -->
    <div class="pointer-events-none absolute inset-0" aria-hidden="true">
      <div class="absolute top-1/4 left-1/2 -translate-x-1/2 w-[40rem] h-[24rem] rounded-full opacity-15 blur-3xl"
        style="background: radial-gradient(ellipse, rgba(0,229,160,0.5) 0%, transparent 65%)"></div>
    </div>

    <!-- 霓虹大数字（装饰） -->
    <div class="relative select-none animate-float" aria-hidden="true">
      <div class="text-[10rem] md:text-[14rem] font-bold leading-none tracking-tighter text-transparent"
        style="background-image: linear-gradient(135deg, #00E5A0 0%, #0EA5E9 50%, #A78BFA 100%);
               -webkit-background-clip: text; background-clip: text;
               filter: drop-shadow(0 0 24px rgba(0,229,160,0.35))">
        404
      </div>
    </div>

    <div class="relative text-center mt-2">
      <h1 class="text-lg text-fg-main font-medium">{{ t('notFound.title') }}</h1>
      <p class="text-sm text-fg-sub mt-2">{{ t('notFound.desc') }}</p>
    </div>

    <button
      class="mt-8 px-6 py-2.5 rounded-lg text-sm font-medium text-on-neon bg-gradient-to-r from-neon-green to-neon-teal
             hover:brightness-110 transition-all cursor-pointer animate-pulseGlow flex items-center gap-2"
      @click="goHome"
    >
      <LayoutDashboard class="w-4 h-4" aria-hidden="true" />
      {{ t('notFound.backHome') }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { LayoutDashboard } from 'lucide-vue-next'

const router = useRouter()
const { t } = useI18n()

function goHome() {
  router.push('/dashboard')
}
</script>
