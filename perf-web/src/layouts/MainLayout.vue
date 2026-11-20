<template>
  <div class="flex h-full bg-bg-base text-fg-main">
    <!-- 左侧导航栏 -->
    <aside
      class="w-60 shrink-0 flex flex-col border-r border-white/10 bg-bg-panel/80 backdrop-blur-md"
    >
      <div class="h-16 flex items-center gap-3 px-5 border-b border-white/10">
        <div
          class="w-9 h-9 rounded-lg bg-gradient-to-br from-neon-green to-neon-blue flex items-center justify-center shadow-neon"
        >
          <Activity class="w-5 h-5 text-bg-base" />
        </div>
        <div>
          <div class="text-sm font-semibold tracking-wide">PerformanceTestor</div>
          <div class="text-[11px] text-fg-sub">Java 性能检测智能体</div>
        </div>
      </div>

      <nav class="flex-1 py-4 px-3 space-y-1 overflow-y-auto">
        <router-link
          v-for="item in menus"
          :key="item.path"
          :to="item.path"
          class="flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm text-fg-sub hover:bg-white/5 hover:text-fg-main transition-colors"
          :class="{ '!bg-neon-green/10 !text-neon-green font-medium': route.path === item.path }"
        >
          <component :is="item.icon" class="w-4.5 h-4.5" />
          {{ item.title }}
        </router-link>
      </nav>

      <div class="px-5 py-4 border-t border-white/10 text-xs text-fg-sub">
        <div class="flex items-center gap-2 mb-1">
          <span class="w-2 h-2 rounded-full bg-neon-green animate-pulseGlow"></span>
          平台在线
        </div>
        <div class="text-[11px]">分离部署模式 · v1.0.0</div>
      </div>
    </aside>

    <!-- 右侧主区域 -->
    <div class="flex-1 flex flex-col min-w-0">
      <!-- 顶栏 -->
      <header
        class="h-16 shrink-0 flex items-center justify-between px-6 border-b border-white/10 bg-bg-card/60 backdrop-blur-md"
      >
        <div class="flex items-center gap-3">
          <span class="text-lg font-semibold">{{ currentTitle }}</span>
          <span class="text-xs text-fg-sub">/ {{ route.meta.title }}</span>
          <!-- 快速开始向导没有侧边栏入口，用户在向导内跳到本页后将无路可回；
               只要会话内还有未结束的向导，就在顶栏提供一步返回 -->
          <button
            v-if="showBackToQuickStart"
            class="flex items-center gap-1 px-2.5 py-1 rounded-md text-xs text-neon-green border border-neon-green/30 hover:bg-neon-green/10 transition-colors cursor-pointer"
            title="返回快速开始向导的当前步骤"
            @click="backToQuickStart"
          >
            <CornerUpLeft class="w-3.5 h-3.5" />
            返回快速开始{{ quickStartStepTitle ? `（${quickStartStepTitle}）` : '' }}
          </button>
        </div>
        <div class="flex items-center gap-4 text-fg-sub">
          <span class="text-xs">当前时间 {{ now }}</span>
          <button class="p-2 rounded-lg hover:bg-white/5 transition-colors" title="通知">
            <Bell class="w-4.5 h-4.5" />
          </button>
          <button class="p-2 rounded-lg hover:bg-white/5 transition-colors" title="设置">
            <Settings class="w-4.5 h-4.5" />
          </button>
          <div class="flex items-center gap-2 text-sm">
            <div
              class="w-7 h-7 rounded-full bg-gradient-to-br from-neon-blue to-neon-green flex items-center justify-center text-xs text-bg-base font-bold"
            >
              {{ avatarChar }}
            </div>
            <span>{{ authStore.username || '管理员' }}</span>
            <button
              class="flex items-center gap-1 px-2 py-1 rounded-md text-xs text-fg-sub hover:text-red-300 hover:bg-red-400/10 transition-colors cursor-pointer"
              title="退出登录"
              @click="handleLogout"
            >
              <LogOut class="w-3.5 h-3.5" />
              退出
            </button>
          </div>
        </div>
      </header>

      <!-- 内容区 -->
      <main class="flex-1 overflow-y-auto p-6">
        <router-view />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity, Bell, Settings, LayoutDashboard, Cpu, GitBranch, Gauge, Target, FolderGit, Database, Bot, Server, LogOut, CornerUpLeft
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { hasQuickStart, quickStartStepTitle, syncQuickStartFlag } from '@/stores/quickStartNav'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const now = ref(new Date().toLocaleString('zh-CN'))

const avatarChar = computed(() => (authStore.username || '管').charAt(0).toUpperCase())

function handleLogout() {
  authStore.logout()
  router.push({ name: 'Login' })
}

const menus = [
  { path: '/dashboard', title: '总览', icon: LayoutDashboard },
  { path: '/projects', title: '项目管理', icon: FolderGit },
  { path: '/deploy', title: '部署管理', icon: Server },
  { path: '/stress', title: '压测中心', icon: Gauge },
  { path: '/datasets', title: '数据管理', icon: Database },
  { path: '/jvm', title: '项目监控', icon: Cpu },
  { path: '/trace', title: '调用链分析', icon: GitBranch },
  { path: '/optimize', title: '性能优化', icon: Target },
  { path: '/ai', title: 'AI 助手', icon: Bot }
]

const currentTitle = computed(() => String(route.meta.title ?? ''))

/** 在向导外的页面且会话内仍有未结束的向导 → 显示返回按钮 */
const showBackToQuickStart = computed(() => hasQuickStart.value && route.path !== '/quick-start')

function backToQuickStart() {
  router.push('/quick-start')
}

let timer: number | undefined
onMounted(() => {
  // 从既有缓存同步标记：刷新页面后返回按钮不丢失
  syncQuickStartFlag()
  timer = window.setInterval(() => {
    now.value = new Date().toLocaleString('zh-CN')
  }, 1000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>
