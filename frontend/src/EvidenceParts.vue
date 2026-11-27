<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { api } from './data'
type Asset = {
  id: number
  url: string
  kind: string
  state: string
  detail: string
  digest: string | null
  parts: { id: number; locator: string; text: string; link: string }[]
}
const props = defineProps<{ eventId: number; documentId: number; fragmentId: number }>()
const assets = ref<Asset[]>([]),
  error = ref('')
const hasStructuredRecord = computed(() =>
  assets.value.some((a) => ['daily_prices', 'sec_filings'].includes(a.kind))
)
let seq = 0
watch(
  () => [props.eventId, props.documentId, props.fragmentId],
  async () => {
    const n = ++seq
    try {
      const result = await api<Asset[]>(
        `/events/${props.eventId}/documents/${props.documentId}/fragments`
      )
      if (n === seq) {
        assets.value = result
        error.value = ''
        if (props.fragmentId) {
          await nextTick()
          if (n === seq)
            document
              .getElementById('evidence-fragment-' + props.fragmentId)
              ?.scrollIntoView({ block: 'nearest' })
        }
      }
    } catch (e) {
      if (n === seq) error.value = (e as Error).message
    }
  },
  { immediate: true }
)
</script>
<template>
  <p v-if="error" class="error">{{ error }}</p>
  <details
    v-for="a in assets"
    :key="a.id"
    class="rail-item"
    :open="a.parts.some((p) => p.id === fragmentId)"
  >
    <summary>
      {{
        {
          feed: hasStructuredRecord ? '归档摘要' : '订阅原文',
          daily_prices: '交易日行情原表',
          sec_filings: 'SEC 申报索引',
          page: '发布方原文',
          html: '网页正文',
          pdf: 'PDF',
          image: '图片',
          media: '音视频',
          captions: '字幕',
        }[a.kind] || a.kind
      }}
      · {{ a.parts.length }} 个片段
    </summary>
    <p v-if="a.detail" class="meta">{{ a.detail }}</p>
    <a v-if="!a.parts.length" :href="a.url" target="_blank" rel="noopener noreferrer"
      >查看发布方材料</a
    >
    <a v-if="a.digest" :href="`/api/events/${eventId}/assets/${a.id}/original`" download
      >下载当时留存的原件</a
    >
    <blockquote
      v-for="part in a.parts"
      :key="part.id"
      :id="'evidence-fragment-' + part.id"
      :class="{ cited: part.id === fragmentId }"
    >
      <a :href="part.link" target="_blank" rel="noopener noreferrer">{{ part.locator }}</a>
      <small>片段 #{{ part.id }}</small>
      <p>{{ part.text }}</p>
    </blockquote>
  </details>
</template>
<style scoped>
.cited {
  border-inline-start: 3px solid var(--accent, #287365);
  padding-inline-start: 0.8rem;
  background: var(--accent-soft, #e8f4f1);
}
</style>
