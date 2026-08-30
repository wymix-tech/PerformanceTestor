import { defineStore } from 'pinia'
import { sourceApi } from '@/api'

/**
 * 项目列表全局状态：消除各视图重复拉取 sourceApi.repos()。
 *
 * <p>首次访问任一视图时加载并缓存；项目管理页等需要强刷的场景调用 reload()。
 */
export const useRepoStore = defineStore('repo', {
  state: () => ({
    repos: [] as Array<Record<string, any>>,
    loaded: false,
    loading: false
  }),
  getters: {
    byId: (state) => (id: number) => state.repos.find((r) => r.id === id)
  },
  actions: {
    /** 加载项目列表（已加载且未强制时直接复用缓存） */
    async load(force = false) {
      if (!force && (this.loaded || this.loading)) return
      this.loading = true
      try {
        const r = await sourceApi.repos()
        this.repos = r.data || []
        this.loaded = true
      } finally {
        this.loading = false
      }
    },
    /** 强制刷新（导入/删除项目后） */
    async reload() {
      this.loaded = false
      await this.load(true)
    }
  }
})
