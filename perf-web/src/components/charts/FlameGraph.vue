<template>
  <div class="w-full">
    <div class="flex items-center gap-2 mb-2 text-xs">
      <input
        v-model="keyword"
        class="input-text w-52 !py-1"
        placeholder="搜索帧名高亮（如 com.demo.Svc）"
      />
      <button v-if="viewRoot" class="px-2 py-1 rounded border border-white/10 text-fg-sub hover:text-neon-green" @click="resetZoom">
        返回全景
      </button>
      <span class="text-fg-sub">点击帧放大 · 悬停查看占比 · {{ totalText }}</span>
    </div>
    <div v-if="!rows.length" class="text-xs text-fg-sub py-10 text-center">暂无折叠栈数据</div>
    <canvas
      v-else
      ref="canvasRef"
      class="w-full rounded-lg tree-bg cursor-pointer"
      :style="{ height: canvasHeight + 'px' }"
      @mousemove="onHover"
      @mouseleave="hover = null"
      @click="onClick"
    ></canvas>
    <div
      v-if="hover"
      class="fixed z-50 pointer-events-none px-3 py-2 rounded-lg text-xs glass-card border border-neon-green/30 max-w-[460px]"
      :style="{ left: hover.x + 12 + 'px', top: hover.y + 12 + 'px' }"
    >
      <div class="font-mono break-all mb-1">{{ hover.node.name }}</div>
      <div class="text-fg-sub">
        总 {{ hover.node.value }} · 自身 {{ hover.node.self }} ·
        占比 {{ pct(hover.node.value) }}%
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

/**
 * 轻量火焰图（P2-1）：纯 Canvas 渲染标准折叠栈（每行 "frame1;frame2;... count"），
 * 支持点击放大 / 关键字搜索高亮 / 悬停占比提示，零第三方依赖。
 */
interface FoldedNode {
  name: string
  value: number
  self: number
  children: Map<string, FoldedNode>
}

const props = defineProps<{
  folded: string
  /** 采样总量口径文案（cpu=样本数 / alloc=字节 / lock=毫秒） */
  totalLabel?: string
}>()

const ROW_H = 22
const SCALE = 1000
const PALETTE = ['#00E5A0', '#0EA5E9', '#34D399', '#FAAD14', '#1677FF', '#52C41A', '#F5222D']
const HIGHLIGHT = '#F59E0B'

const keyword = ref('')
const viewRoot = ref<FoldedNode | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const hover = ref<{ x: number; y: number; node: FoldedNode } | null>(null)

/** 折叠栈文本 → 聚合树（根为 "root"，value 为包含值，self 为叶帧自留值） */
function buildTree(folded: string): FoldedNode {
  const root: FoldedNode = { name: 'root', value: 0, self: 0, children: new Map() }
  for (const line of folded.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const idx = trimmed.lastIndexOf(' ')
    if (idx <= 0) continue
    const path = trimmed.slice(0, idx).trim()
    const count = Number(trimmed.slice(idx + 1))
    if (!path || !Number.isFinite(count) || count <= 0) continue
    root.value += count
    let node = root
    const frames = path.split(';')
    for (let i = 0; i < frames.length; i++) {
      let child = node.children.get(frames[i])
      if (!child) {
        child = { name: frames[i], value: 0, self: 0, children: new Map() }
        node.children.set(frames[i], child)
      }
      child.value += count
      if (i === frames.length - 1) child.self += count
      node = child
    }
    if (frames.length === 0) root.self += count
  }
  return root
}

/** 渲染行：按栈深度逐层布局（占比坐标系 0~SCALE，支持点击帧放大） */
interface FrameRect {
  node: FoldedNode
  depth: number
  x: number
  w: number
}

const tree = computed(() => buildTree(props.folded))

const layout = computed((): { rows: FrameRect[][]; total: number } => {
  const rootNode = viewRoot.value ?? tree.value
  const total = rootNode.value || tree.value.value
  if (total <= 0) return { rows: [], total: 0 }
  const rows: FrameRect[][] = []
  let level: FrameRect[] = [{ node: rootNode, depth: 0, x: 0, w: SCALE }]
  while (level.length) {
    rows.push(level)
    const next: FrameRect[] = []
    for (const r of level) {
      let childX = r.x
      for (const child of r.node.children.values()) {
        const w = Math.round((child.value / (r.node.value || 1)) * r.w)
        if (w <= 0) continue
        next.push({ node: child, depth: r.depth + 1, x: childX, w })
        childX += w
      }
    }
    level = next
  }
  return { rows, total }
})

const rows = computed(() => layout.value.rows)
const total = computed(() => layout.value.total)

const totalText = computed(() => {
  const t = total.value
  const label = props.totalLabel ?? '样本'
  return t >= 1000000 ? `${(t / 1000000).toFixed(1)}M ${label}` : `${t} ${label}`
})

const canvasHeight = computed(() => rows.value.length * ROW_H + 8)

function pct(value: number): string {
  return total.value ? ((value * 100) / total.value).toFixed(1) : '0.0'
}

function isHighlighted(name: string): boolean {
  const k = keyword.value.trim()
  return !!k && name.toLowerCase().includes(k.toLowerCase())
}

function draw() {
  const canvas = canvasRef.value
  if (!canvas || !rows.value.length) return
  const dpr = window.devicePixelRatio || 1
  const cssWidth = canvas.clientWidth
  const cssHeight = canvasHeight.value
  canvas.width = Math.round(cssWidth * dpr)
  canvas.height = Math.round(cssHeight * dpr)
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, cssWidth, cssHeight)
  ctx.font = '10px Menlo, Consolas, monospace'
  ctx.textBaseline = 'middle'
  for (const row of rows.value) {
    for (const r of row) {
      const x = (r.x / SCALE) * cssWidth
      const w = Math.max((r.w / SCALE) * cssWidth - 1, 1)
      const y = r.depth * ROW_H + 4
      const h = ROW_H - 4
      ctx.fillStyle = isHighlighted(r.node.name) ? HIGHLIGHT : PALETTE[r.depth % PALETTE.length]
      ctx.fillRect(x, y, w, h)
      const maxChars = Math.floor((w - 8) / 6)
      if (maxChars >= 4) {
        const label = r.node.name.length > maxChars
          ? r.node.name.slice(0, maxChars - 1) + '…' : r.node.name
        ctx.fillStyle = '#0B1220'
        ctx.fillText(label, x + 4, y + h / 2)
      }
    }
  }
}

function pick(clientX: number, clientY: number): FrameRect | null {
  const canvas = canvasRef.value
  if (!canvas || !rows.value.length) return null
  const rect = canvas.getBoundingClientRect()
  const cssWidth = rect.width
  const rx = ((clientX - rect.left) / cssWidth) * SCALE
  const depth = Math.floor((clientY - rect.top - 4) / ROW_H)
  if (depth < 0 || depth >= rows.value.length) return null
  for (const r of rows.value[depth]) {
    if (rx >= r.x && rx < r.x + r.w) return r
  }
  return null
}

function onHover(e: MouseEvent) {
  const picked = pick(e.clientX, e.clientY)
  hover.value = picked ? { x: e.clientX, y: e.clientY, node: picked.node } : null
}

function onClick(e: MouseEvent) {
  const picked = pick(e.clientX, e.clientY)
  if (picked) viewRoot.value = picked.node
}

function resetZoom() {
  viewRoot.value = null
}

watch([rows, keyword], () => draw(), { flush: 'post' })

onMounted(() => draw())
</script>
