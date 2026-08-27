import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'Dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '总览' }
      },
      {
        path: 'jvm',
        name: 'JvmMonitor',
        component: () => import('@/views/JvmMonitorView.vue'),
        meta: { title: 'JVM 监控' }
      },
      {
        path: 'trace',
        name: 'TraceAnalyzer',
        component: () => import('@/views/TraceAnalyzerView.vue'),
        meta: { title: '调用链分析' }
      },
      {
        path: 'stress',
        name: 'StressCenter',
        component: () => import('@/views/StressCenterView.vue'),
        meta: { title: '压测中心' }
      },
      {
        path: 'source',
        name: 'SourceOptimize',
        component: () => import('@/views/SourceOptimizeView.vue'),
        meta: { title: '源码与优化' }
      },
      {
        path: 'ai',
        name: 'AiAssistant',
        component: () => import('@/views/AiAssistantView.vue'),
        meta: { title: 'AI 助手' }
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router
