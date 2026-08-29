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
        path: 'quick-start',
        name: 'QuickStart',
        component: () => import('@/views/QuickStartView.vue'),
        meta: { title: '快速开始' }
      },
      {
        path: 'projects',
        name: 'ProjectManage',
        component: () => import('@/views/ProjectManageView.vue'),
        meta: { title: '项目管理' }
      },
      {
        path: 'jvm',
        name: 'JvmMonitor',
        component: () => import('@/views/JvmMonitorView.vue'),
        meta: { title: '项目监控' }
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
        path: 'optimize',
        name: 'Optimize',
        component: () => import('@/views/OptimizeView.vue'),
        meta: { title: '性能优化' }
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
