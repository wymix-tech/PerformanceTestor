/// <reference types="vitest" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: true,
    // 开发环境代理：前端请求统一转发到 perf-server。
    // 阶段 5 绞杀者切换：VITE_API_TARGET=http://localhost:8081 即指向 Python perf-py
    // server（契约测试已保证 /api 结构兼容），perf-web 代码零改动。
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8080',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500
  },
  // 单元测试（vitest + jsdom）：与构建共用同一套别名，测试文件为 **/__tests__/*.spec.ts
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/__tests__/**/*.spec.ts']
  }
})
