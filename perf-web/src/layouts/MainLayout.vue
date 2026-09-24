<template>
  <div class="flex h-full bg-bg-base text-fg-main">
    <!-- 无障碍：键盘用户第一个 Tab 即可跳过侧边导航直达主内容 -->
    <a href="#main-content" class="skip-link">{{ t('a11y.skipToContent') }}</a>

    <!-- 左侧导航栏 -->
    <aside
      class="w-60 shrink-0 flex flex-col border-r border-white/10 bg-bg-panel/80 backdrop-blur-md"
    >
      <div class="h-16 flex items-center gap-3 px-5 border-b border-white/10">
        <div
          class="w-9 h-9 rounded-lg bg-gradient-to-br from-neon-green to-neon-blue flex items-center justify-center shadow-neon"
          aria-hidden="true"
        >
          <Activity class="w-5 h-5 text-on-neon" />
        </div>
        <div>
          <div class="text-sm font-semibold tracking-wide">PerformanceTestor</div>
          <div class="text-[11px] text-fg-sub">{{ t('nav.brandSub') }}</div>
        </div>
      </div>

      <nav class="flex-1 py-4 px-3 space-y-1 overflow-y-auto" :aria-label="t('a11y.mainNav')">
        <router-link
          v-for="item in menus"
          :key="item.path"
          :to="item.path"
          class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm text-fg-sub hover:bg-white/5 hover:text-fg-main transition-colors"
          :class="{ '!bg-neon-green/10 !text-neon-green font-medium': route.path === item.path }"
          :aria-current="route.path === item.path ? 'page' : undefined"
        >
          <component :is="item.icon" class="w-4.5 h-4.5" aria-hidden="true" />
          {{ item.title }}
        </router-link>
      </nav>

      <div class="px-5 py-4 border-t border-white/10 text-xs text-fg-sub">
        <div class="flex items-center gap-2 mb-1">
          <span class="w-2 h-2 rounded-full bg-neon-green animate-pulseGlow" aria-hidden="true"></span>
          {{ t('nav.online') }}
        </div>
        <div class="text-[11px]">{{ t('nav.deployMode') }}</div>
      </div>
    </aside>

    <!-- 右侧主区域 -->
    <div class="flex-1 flex flex-col min-w-0">
      <!-- 顶栏 -->
      <header
        class="h-16 shrink-0 flex items-center justify-between px-6 border-b border-white/10 bg-bg-card/60 backdrop-blur-md"
      >
        <div class="flex items-center gap-3">
          <h1 class="text-lg font-semibold">{{ currentTitle }}</h1>
          <span class="text-xs text-fg-sub" aria-hidden="true">/ {{ routeTitle }}</span>
          <!-- 快速开始向导没有侧边栏入口，用户在向导内跳到本页后将无路可回；
               只要会话内还有未结束的向导，就在顶栏提供一步返回 -->
          <button
            v-if="showBackToQuickStart"
            class="flex items-center gap-1 px-2.5 py-1 rounded-md text-xs text-neon-green border border-neon-green/30 hover:bg-neon-green/10 transition-colors cursor-pointer"
            :title="t('nav.backToQuickStartTitle')"
            @click="backToQuickStart"
          >
            <CornerUpLeft class="w-3.5 h-3.5" aria-hidden="true" />
            {{ t('nav.backToQuickStart') }}{{ quickStartStepTitle ? `（${quickStartStepTitle}）` : '' }}
          </button>
        </div>
        <div class="flex items-center gap-2 text-fg-sub">
          <span class="text-xs">{{ t('nav.currentTime', { time: now }) }}</span>

          <!-- 语言切换 -->
          <label class="flex items-center gap-1.5 text-xs">
            <span class="sr-only">{{ t('language.switch') }}</span>
            <Languages class="w-4 h-4" aria-hidden="true" />
            <select
              class="bg-transparent border border-white/10 rounded-md px-1.5 py-1 text-xs text-fg-sub hover:text-fg-main transition-colors cursor-pointer outline-none"
              :value="locale"
              :aria-label="t('language.switch')"
              @change="onLocaleChange(($event.target as HTMLSelectElement).value)"
            >
              <option v-for="opt in localeOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
            </select>
          </label>

          <!-- 明暗主题切换：三态（深色 / 浅色 / 跟随系统） -->
          <button
            class="p-2 rounded-lg hover:bg-white/5 transition-colors cursor-pointer"
            :title="t('theme.toggle')"
            :aria-label="t('theme.current', { theme: themeLabel })"
            @click="themeStore.toggle()"
          >
            <Sun v-if="themeStore.isDark" class="w-4.5 h-4.5" aria-hidden="true" />
            <Moon v-else class="w-4.5 h-4.5" aria-hidden="true" />
          </button>

          <button class="p-2 rounded-lg hover:bg-white/5 transition-colors cursor-pointer" :title="t('nav.notifications')" :aria-label="t('nav.notifications')">
            <Bell class="w-4.5 h-4.5" aria-hidden="true" />
          </button>
          <button class="p-2 rounded-lg hover:bg-white/5 transition-colors cursor-pointer" :title="t('nav.settings')" :aria-label="t('nav.settings')">
            <Settings class="w-4.5 h-4.5" aria-hidden="true" />
          </button>
          <div class="flex items-center gap-2 text-sm ml-2">
            <div
              class="w-7 h-7 rounded-full bg-gradient-to-br from-neon-blue to-neon-green flex items-center justify-center text-xs text-on-neon font-bold"
              aria-hidden="true"
            >
              {{ avatarChar }}
            </div>
            <span>{{ authStore.username || t('nav.defaultUser') }}</span>
            <button
              class="flex items-center gap-1 px-2 py-1 rounded-md text-xs text-fg-sub hover:text-red-300 hover:bg-red-400/10 transition-colors cursor-pointer"
              :title="t('nav.logout')"
              :aria-label="t('nav.logout')"
              @click="handleLogout"
            >
              <LogOut class="w-3.5 h-3.5" aria-hidden="true" />
              {{ t('nav.logoutShort') }}
            </button>
          </div>
        </div>
      </header>

      <!-- 内容区 -->
      <main id="main-content" class="flex-1 overflow-y-auto p-6" tabindex="-1">
        <router-view />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  Activity, Bell, Settings, LayoutDashboard, Cpu, GitBranch, Gauge, Target, FolderGit, Bot, Server,
  LogOut, CornerUpLeft, Sun, Moon, Languages
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import { setLocale, type AppLocale } from '@/i18n'
import { hasQuickStart, quickStartStepTitle, syncQuickStartFlag } from '@/stores/quickStartNav'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const themeStore = useThemeStore()
const { t, locale } = useI18n()

const now = ref('')
const localeOptions: { value: AppLocale; label: string }[] = [
  { value: 'zh-CN', label: '简体中文' },
  { value: 'en-US', label: 'English' }
]

const avatarChar = computed(() => (authStore.username || t('nav.defaultUser')).charAt(0).toUpperCase())
const themeLabel = computed(() => (themeStore.isDark ? t('theme.dark') : t('theme.light')))
const routeTitle = computed(() => String(route.meta.title ?? ''))
// 标题跟随语言：优先取路由 i18n key，缺失时回落 meta.title（业务页尚未全量抽取）
const currentTitle = computed(() => (route.meta.titleKey ? t(String(route.meta.titleKey)) : routeTitle.value))

function onLocaleChange(value: string) {
  setLocale(value as AppLocale)
}
function tickClock() {
  now.value = new Date().toLocaleString(locale.value, { hour12: false })
}
function handleLogout() {
  authStore.logout()
  router.push({ name: 'Login' })
}

const menus = computed(() => [
  { path: '/dashboard', title: t('nav.dashboard'), icon: LayoutDashboard },
  { path: '/projects', title: t('nav.projects'), icon: FolderGit },
  { path: '/deploy', title: t('nav.deploy'), icon: Server },
  { path: '/stress', title: t('nav.stress'), icon: Gauge },
  { path: '/jvm', title: t('nav.jvm'), icon: Cpu },
  { path: '/trace', title: t('nav.trace'), icon: GitBranch },
  { path: '/optimize', title: t('nav.optimize'), icon: Target },
  { path: '/ai', title: t('nav.ai'), icon: Bot }
])

/** 在向导外的页面且会话内仍有未结束的向导 → 显示返回按钮 */
const showBackToQuickStart = computed(() => hasQuickStart.value && route.path !== '/quick-start')

function backToQuickStart() {
  router.push('/quick-start')
}

let timer: number | undefined
let disposeTheme: (() => void) | undefined
onMounted(() => {
  syncQuickStartFlag()
  disposeTheme = themeStore.init()
  tickClock()
  timer = window.setInterval(tickClock, 1000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
  disposeTheme?.()
})
</script>

<style scoped>
/* 跳转链接默认移出视口，聚焦时才出现在左上角（键盘可达、鼠标用户无感） */
.skip-link {
  position: absolute;
  left: 0.75rem;
  top: 0.75rem;
  z-index: 100;
  padding: 0.5rem 0.75rem;
  border-radius: 0.5rem;
  font-size: 0.75rem;
  color: rgb(var(--bg-base));
  background: rgb(var(--neon-green));
  transform: translateY(-150%);
  transition: transform 0.15s ease;
}
.skip-link:focus {
  transform: translateY(0);
}
</style>
