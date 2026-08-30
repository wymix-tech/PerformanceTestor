<template>
  <div class="relative min-h-screen flex items-center justify-center overflow-hidden bg-bg-base px-4">
    <!-- 径向渐变光晕背景 -->
    <div class="pointer-events-none absolute inset-0">
      <div class="absolute -top-32 -left-32 w-[36rem] h-[36rem] rounded-full opacity-20 blur-3xl"
        style="background: radial-gradient(circle, rgba(0,229,160,0.6) 0%, transparent 70%)"></div>
      <div class="absolute -bottom-40 -right-24 w-[32rem] h-[32rem] rounded-full opacity-15 blur-3xl"
        style="background: radial-gradient(circle, rgba(14,165,233,0.6) 0%, transparent 70%)"></div>
      <div class="absolute top-1/3 right-1/4 w-64 h-64 rounded-full opacity-10 blur-3xl"
        style="background: radial-gradient(circle, rgba(167,139,250,0.5) 0%, transparent 70%)"></div>
      <!-- 网格纹理 -->
      <div class="absolute inset-0 opacity-[0.04]"
        style="background-image: linear-gradient(rgba(230,237,247,1) 1px, transparent 1px), linear-gradient(90deg, rgba(230,237,247,1) 1px, transparent 1px); background-size: 48px 48px;"></div>
    </div>

    <!-- 登录卡片 -->
    <div class="relative z-10 w-full max-w-md" :class="{ 'animate-shake': shake }">
      <div class="glass-card p-8 animate-float" style="animation-duration: 6s">
        <!-- 平台标识 -->
        <div class="flex flex-col items-center mb-8">
          <div class="w-14 h-14 rounded-2xl bg-gradient-to-br from-neon-green to-neon-blue flex items-center justify-center shadow-neon mb-4">
            <Activity class="w-7 h-7 text-bg-base" />
          </div>
          <h1 class="text-2xl font-semibold tracking-wide">PerformanceTestor</h1>
          <p class="text-sm text-fg-sub mt-1.5">Java 性能检测智能体平台</p>
        </div>

        <!-- 登录表单 -->
        <form class="space-y-5" @submit.prevent="handleLogin">
          <div>
            <label class="block text-xs text-fg-sub mb-2">账号</label>
            <div class="relative">
              <User class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-fg-sub/60" />
              <input v-model.trim="username" type="text" autocomplete="username" placeholder="请输入管理员账号"
                class="input-text w-full pl-9" :class="{ '!border-red-400/60': !!error }" />
            </div>
          </div>
          <div>
            <label class="block text-xs text-fg-sub mb-2">密码</label>
            <div class="relative">
              <Lock class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-fg-sub/60" />
              <input v-model="password" :type="showPassword ? 'text' : 'password'"
                autocomplete="current-password" placeholder="请输入密码"
                class="input-text w-full pl-9 pr-10" :class="{ '!border-red-400/60': !!error }" />
              <button type="button" class="absolute right-3 top-1/2 -translate-y-1/2 text-fg-sub/60 hover:text-fg-main transition-colors"
                @click="showPassword = !showPassword">
                <Eye v-if="showPassword" class="w-4 h-4" />
                <EyeOff v-else class="w-4 h-4" />
              </button>
            </div>
          </div>

          <!-- 错误提示 -->
          <transition name="fade">
            <div v-if="error" class="flex items-center gap-2 px-3 py-2.5 rounded-lg border border-red-400/30 bg-red-400/10 text-sm text-red-300">
              <AlertCircle class="w-4 h-4 shrink-0" />
              <span>{{ error }}</span>
            </div>
          </transition>

          <button type="submit" :disabled="loading || !username || !password"
            class="w-full py-2.5 rounded-lg text-sm font-semibold text-bg-base bg-gradient-to-r from-neon-green to-neon-teal transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed animate-pulseGlow hover:brightness-110 flex items-center justify-center gap-2">
            <Loader2 v-if="loading" class="w-4 h-4 animate-spin" />
            <LogIn v-else class="w-4 h-4" />
            {{ loading ? '正在登录…' : '登 录' }}
          </button>
        </form>

        <p class="mt-6 text-center text-xs text-fg-sub/70">
          默认账号 admin / admin123，可通过环境变量 PERF_ADMIN_USER / PERF_ADMIN_PASSWORD 覆盖
        </p>
      </div>

      <p class="mt-6 text-center text-xs text-fg-sub/50">
        平台与被测应用分离部署 · 探针上报通道免鉴权
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Activity, User, Lock, Eye, EyeOff, AlertCircle, LogIn, Loader2 } from 'lucide-vue-next'
import { authApi } from '@/api'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const username = ref('')
const password = ref('')
const showPassword = ref(false)
const loading = ref(false)
const error = ref('')
const shake = ref(false)

async function handleLogin() {
  if (loading.value || !username.value || !password.value) return
  loading.value = true
  error.value = ''
  try {
    // http 拦截器已解包为 { code, message, data }（axios 类型无法感知，需断言）
    const res = (await authApi.login({ username: username.value, password: password.value })) as unknown as {
      code: number
      message: string
      data?: { token?: string; username?: string }
    }
    if (res.code === 0 && res.data?.token) {
      authStore.setCredentials(res.data.token, res.data.username || username.value)
      const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
      router.push(redirect)
    } else {
      throw new Error(res.message || '登录失败')
    }
  } catch (e) {
    const err = e as { response?: { data?: { message?: string } }; message?: string }
    error.value = err.response?.data?.message || err.message || '登录失败，请检查网络连接'
    shake.value = true
    setTimeout(() => { shake.value = false }, 500)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.25s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
