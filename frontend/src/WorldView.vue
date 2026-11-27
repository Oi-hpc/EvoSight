<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ArrowUpRight, BookOpen, ChevronLeft, Folder, Globe2, Search } from 'lucide-vue-next'
import { api, knowledgeKind, stamp, type Card } from './data'

type Summary = {
  id: number
  category: string
  head_id: number
  subject: string
  text: string
  kind: string
  breadcrumbs: Crumb[]
}
type Crumb = { path: string; label: string }
type Directory = {
  path: string
  breadcrumbs: Crumb[]
  children: (Crumb & { count: number })[]
  direct_count: number
  count: number
}
type Catalog = {
  entries: Summary[]
  total: number
  counts: Record<string, number>
  categories: Record<string, string>
  directory: Directory
}
type Citation = {
  fragment_id: number
  document_id: number
  quote: string
  locator: string
  link: string
  title: string
}
type Detail = {
  id: number
  demo: number
  head_id: number
  version: number
  category_name: string
  path: string
  breadcrumbs: Crumb[]
  schema_id: string
  schema_version: number
  attributes: Record<string, string | number>
  field_labels: Record<string, string>
  claim: {
    id: number
    event_id: number
    subject: string
    text: string
    kind: string
    reason: string
    occurred_at: string | null
    precision: string
    citations: Citation[]
    refs: number[]
    facets: Record<string, string | number>
  }
  history: { id: number; version: number; created_at: string; origin_id: number; origin: string }[]
  events: { id: number; title: string; index_reason: string }[]
  entity: { name: string; identity: string; aliases: string[] } | null
  related_entries: { id: number; subject: string; text: string }[]
}
const props = defineProps<{
  entryId: number
  versionId: number
  target: number
  events: Card[]
  catalogPath: string
}>()
const catalog = ref<Catalog>(),
  item = ref<Detail>(),
  search = ref(''),
  offset = ref(0)
const error = ref(''),
  notice = ref(''),
  loading = ref(false),
  chosen = ref(0),
  reason = ref(''),
  section = ref(''),
  expected = ref<number | null>(null),
  saving = ref(false)
const pageSize = 24
const available = computed(() => props.events.filter((e) => !e.demo))
const existingLink = computed(() => item.value?.events.find((link) => link.id === chosen.value))
const route = (entry = 0, version = 0, path = props.catalogPath) =>
  '#/world/' +
  entry +
  '/' +
  version +
  (props.target || path ? '/' + props.target : '') +
  (path ? '/' + path.split('/').join('~') : '')
const displayFields = computed(() => {
  if (!item.value) return {}
  const record = item.value
  return {
    ...record.claim.facets,
    ...Object.fromEntries(
      Object.entries(record.attributes).map(([key, v]) => [record.field_labels[key] || key, v])
    ),
  }
})
const missingLabels = computed(() => {
  const record = item.value
  if (!record || !Object.keys(record.attributes).length) return []
  return Object.entries(record.field_labels)
    .filter(([key]) => !(key in record.attributes))
    .map(([, label]) => label)
})
function chooseCategory(e: Event) {
  location.hash = route(0, 0, (e.target as HTMLSelectElement).value)
}
let seq = 0,
  selected = 0,
  delay: ReturnType<typeof setTimeout>
async function load() {
  const token = ++seq
  loading.value = true
  error.value = ''
  try {
    if (props.entryId) {
      const data = await api<Detail>(
        '/world/' + props.entryId + (props.versionId ? '?revision=' + props.versionId : '')
      )
      if (token === seq) item.value = data
    } else {
      const params = new URLSearchParams({
        q: search.value,
        path: props.catalogPath,
        offset: String(offset.value),
        limit: String(pageSize),
      })
      const data = await api<Catalog>('/world?' + params)
      if (token === seq) catalog.value = data
    }
  } catch (e) {
    if (token === seq) error.value = (e as Error).message
  } finally {
    if (token === seq) loading.value = false
  }
}
watch(
  () => [props.entryId, props.versionId, props.target, props.catalogPath],
  () => {
    item.value = undefined
    notice.value = ''
    reason.value = ''
    section.value = ''
    chosen.value = props.target || 0
    offset.value = 0
    void load()
  },
  { immediate: true }
)
watch(search, () => {
  if (offset.value) offset.value = 0
  clearTimeout(delay)
  delay = setTimeout(load, 160)
})
watch(offset, () => {
  clearTimeout(delay)
  void load()
})
async function refreshExpected() {
  const token = ++selected
  expected.value = null
  if (!chosen.value || !props.entryId) return
  try {
    const data = await api<{ snapshot: number | null }>(
      '/events/' + chosen.value + '/knowledge?section=index'
    )
    if (token === selected) expected.value = data.snapshot || 0
  } catch (e) {
    if (token === selected) error.value = (e as Error).message
  }
}
watch([chosen, () => props.entryId], refreshExpected, { immediate: true })
async function link() {
  if (!item.value || !chosen.value || expected.value === null || saving.value) return
  saving.value = true
  error.value = ''
  try {
    await api('/events/' + chosen.value + '/knowledge/links', {
      entry_id: item.value.id,
      version_id: item.value.head_id,
      expected_snapshot: expected.value,
      reason: reason.value,
      section: section.value,
    })
    notice.value = existingLink.value ? '已固定本事件的引用' : '事件索引已引用此条目'
    reason.value = ''
    section.value = ''
    chosen.value = 0
    await load()
  } catch (e) {
    const message = (e as Error).message
    await load()
    await refreshExpected()
    error.value = message
  } finally {
    saving.value = false
  }
}
onBeforeUnmount(() => {
  ++seq
  ++selected
  clearTimeout(delay)
})
</script>

<template>
  <header class="page-head">
    <div>
      <h1><Globe2 /> 世界知识库</h1>
      <p v-if="!entryId">按类别检索各事件共同积累的知识、出处与修订记录。</p>
    </div>
    <a v-if="target" class="btn" :href="'#/events/' + target + '/knowledge'">返回事件索引</a>
  </header>
  <p v-if="error" role="alert" class="notice warn">
    {{ error }} <button class="btn quiet" @click="load">重新读取</button>
  </p>
  <p v-if="notice" role="status" class="notice">{{ notice }}</p>
  <p v-if="loading && !item && !catalog" class="meta">正在读取世界知识库…</p>
  <template v-if="!entryId && catalog">
    <section class="panel world-panel">
      <nav class="directory-trail" aria-label="当前知识目录">
        <a :href="route(0, 0, '')">全部知识</a>
        <template v-for="p in catalog.directory.breadcrumbs" :key="p.path"
          ><span aria-hidden="true">/</span
          ><a :href="route(0, 0, p.path)">{{ p.label }}</a></template
        >
      </nav>
      <div class="world-filter">
        <label
          ><Search />
          <input v-model="search" aria-label="搜索世界知识" placeholder="搜索主体、别名或结论"
        /></label>
        <label
          >知识类别
          <select :value="catalogPath.split('/')[0]" @change="chooseCategory">
            <option value="">全部类别</option>
            <option v-for="(name, key) in catalog.categories" :key="key" :value="key">
              {{ name }}（{{ catalog.counts[key] || 0 }}）
            </option>
          </select></label
        >
        <span class="meta">{{ catalog.total }} 个条目</span>
      </div>
      <nav
        v-if="catalog.directory.children.some((p) => p.count)"
        class="directory-children"
        aria-label="下级内容目录"
      >
        <a
          v-for="p in catalog.directory.children.filter((p) => p.count)"
          :key="p.path"
          :href="route(0, 0, p.path)"
          ><Folder /> {{ p.label }} <small>{{ p.count }}</small></a
        >
      </nav>
      <p v-if="!catalog.entries.length" class="rail-item">
        {{
          search || catalogPath
            ? '没有符合条件的知识。'
            : '公开事件发布研究结果后，知识会按类别出现在这里。'
        }}
      </p>
      <article v-for="k in catalog.entries" :key="k.id" class="rail-item">
        <small
          >{{ k.breadcrumbs.map((p) => p.label).join(' / ') }} · {{ knowledgeKind(k.kind) }}</small
        >
        <h3>
          <a :href="route(k.id)">{{ k.subject }} <ArrowUpRight /></a>
        </h3>
        <p>{{ k.text }}</p>
      </article>
      <div v-if="catalog.total > pageSize" class="actions">
        <button class="btn" :disabled="!offset" @click="offset -= pageSize">上一页</button
        ><button
          class="btn"
          :disabled="offset + pageSize >= catalog.total"
          @click="offset += pageSize"
        >
          下一页
        </button>
      </div>
    </section>
  </template>
  <template v-else-if="entryId && item">
    <a :href="route(0, 0, catalogPath || item.path)" class="evidence-link"
      ><ChevronLeft /> 返回知识目录</a
    >
    <section class="panel world-panel">
      <nav class="directory-trail" aria-label="条目所属目录">
        <a :href="route(0, 0, '')">全部知识</a>
        <template v-for="p in item.breadcrumbs" :key="p.path"
          ><span aria-hidden="true">/</span
          ><a :href="route(0, 0, p.path)">{{ p.label }}</a></template
        >
      </nav>
      <small
        >{{ item.demo ? '演示资料 · ' : '' }}{{ item.category_name }} ·
        {{ knowledgeKind(item.claim.kind) }} · 版本 {{ item.version }}
        <template v-if="item.claim.occurred_at">
          · 发生于
          {{
            item.claim.precision === 'date'
              ? item.claim.occurred_at
              : stamp(item.claim.occurred_at, true)
          }}</template
        ></small
      >
      <h2>{{ item.claim.subject }}</h2>
      <p>{{ item.claim.text }}</p>
      <p v-if="item.claim.reason" class="meta">{{ item.claim.reason }}</p>
      <p v-if="item.entity" class="meta">
        {{ item.entity.name }} · {{ item.entity.identity
        }}<template v-if="item.entity.aliases.length">
          · {{ item.entity.aliases.join('、') }}</template
        >
      </p>
      <dl v-if="Object.keys(displayFields).length" class="world-facets">
        <template v-for="(v, k) in displayFields" :key="k"
          ><dt>{{ k }}</dt>
          <dd>{{ v }}</dd></template
        >
      </dl>
      <p v-if="missingLabels.length" class="meta">尚未收录{{ missingLabels.join('、') }}。</p>
      <a
        class="meta"
        :href="'/api/world/schemas/' + item.schema_id + '?version=' + item.schema_version"
        target="_blank"
        rel="noopener noreferrer"
        >查看字段定义 · {{ item.schema_id }} v{{ item.schema_version }}</a
      >
      <h3>
        证据
        <small v-if="item.claim.citations.length"
          >{{ item.claim.refs.length }} 份资料 · {{ item.claim.citations.length }} 处引文</small
        >
      </h3>
      <blockquote v-for="c in item.claim.citations" :key="c.fragment_id + ':' + c.quote">
        <p>{{ c.quote }}</p>
        <a
          :href="'#/events/' + item.claim.event_id + '/doc/' + c.document_id + '/' + c.fragment_id"
          >{{ c.title }}</a
        >
        · <a :href="c.link" target="_blank" rel="noopener noreferrer">{{ c.locator }}</a>
      </blockquote>
    </section>
    <section v-if="item.related_entries.length" class="panel world-panel">
      <h2>同一实体的其他知识</h2>
      <p v-for="r in item.related_entries" :key="r.id">
        <a :href="route(r.id)">{{ r.text }}</a>
      </p>
    </section>
    <section class="panel world-panel">
      <h2><BookOpen /> 引用此知识的事件</h2>
      <p v-for="e in item.events" :key="e.id">
        <a :href="'#/events/' + e.id + '/knowledge'">{{ e.title }}</a> · {{ e.index_reason }}
      </p>
      <form
        v-if="!item.demo && available.length && item.claim.id === item.head_id"
        class="world-filter"
        @submit.prevent="link"
      >
        <label
          >引用到事件
          <select v-model.number="chosen" required>
            <option :value="0">选择事件</option>
            <option v-for="e in available" :key="e.id" :value="e.id">{{ e.title }}</option>
          </select></label
        >
        <input
          v-model="reason"
          aria-label="引用理由"
          placeholder="这条知识能帮助核实什么？"
          required
          minlength="2"
          maxlength="400"
        />
        <label
          >事件内章节
          <select v-model="section">
            <option value="">沿用原章节</option>
            <option value="background">事件背景</option>
            <option value="people">人物与组织</option>
            <option value="opinion">社会舆情</option>
            <option value="news">新闻与多模态</option>
          </select></label
        >
        <button
          class="btn primary"
          :disabled="!chosen || reason.trim().length < 2 || saving || expected === null"
        >
          {{ existingLink ? '固定引用' : '加入事件索引' }}
        </button>
      </form>
    </section>
    <section class="panel world-panel">
      <h2>修订历史</h2>
      <p v-for="v in item.history" :key="v.id">
        <a :href="route(item.id, v.id)">版本 {{ v.version }}</a> · {{ stamp(v.created_at, true) }} ·
        由 <a :href="'#/events/' + v.origin_id">{{ v.origin }}</a> 贡献
      </p>
    </section>
  </template>
</template>

<style scoped>
.directory-trail,
.directory-children {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
  margin: 0 0 0.9rem;
}
.directory-trail {
  font-size: 0.86rem;
}
.directory-trail span {
  color: #84938c;
}
.directory-children {
  margin: 0.9rem 0;
}
.directory-children a {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  border: 1px solid var(--line, #d9dfdd);
  padding: 0.35rem 0.65rem;
  border-radius: 6px;
  text-decoration: none;
}
.directory-children svg {
  height: 16px;
  width: 16px;
}
.directory-children small {
  font-variant-numeric: tabular-nums;
}
.world-panel {
  padding: 1.1rem 1.3rem;
  margin: 1rem 0;
  overflow-wrap: anywhere;
}
.world-panel h2,
.page-head h1 {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.world-panel h3 {
  margin: 0.65rem 0;
}
.world-panel h3 svg {
  width: 15px;
  height: 15px;
}
.world-filter {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  flex-wrap: wrap;
}
.world-filter label {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.world-filter input,
.world-filter select {
  max-width: 100%;
  padding: 0.45rem;
  border: 1px solid var(--line, #d9dfdd);
  border-radius: 6px;
  background: transparent;
  color: inherit;
}
.world-filter svg,
.evidence-link svg,
.world-panel h2 svg {
  width: 18px;
  height: 18px;
}
.world-facets {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.35rem 0.8rem;
}
.world-facets dd {
  margin: 0;
}
.world-panel blockquote {
  border-left: 3px solid var(--line, #d9dfdd);
  padding-left: 0.8rem;
  margin: 0.7rem 0;
}
</style>
