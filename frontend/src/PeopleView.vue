<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  FileText,
  Link2,
  RefreshCw,
  UsersRound,
} from 'lucide-vue-next'
import { api, label, stamp } from './data'
interface Person {
  requested?: boolean
  id: number
  entity_id: number
  name: string
  identity: string
  role: string
  relevance: string
  quote: string
  fragment_id: number
  document_id: number
  goal_version: number
  aliases: string[]
  materials: number
  updated_at: string
}
interface Account {
  id: number
  platform: string
  account_key: string
  url: string
  state: string
  proof_doc: number
  proof_event: number
  proof_text: string
}
interface Material {
  document_id: number
  fragment_id: number
  source_url: string
  channel: string
  attribution: string
  source_at: string | null
  period: string
  title: string
  excerpt: string
}
interface Coverage {
  round_id: number
  channel: string
  status: string
  detail: string
  checked_at: string
  window_start: string
  window_end: string
}
interface Claim {
  attribution_review?: boolean
  id: number
  world_id: number
  world_version: number
  subject: string
  text: string
  kind: string
  created_at: string
  facets: Record<string, unknown>
  citations: { fragment_id: number; document_id: number; quote: string }[]
}
interface Result {
  people: Person[]
  pending_people?: {
    id: number
    target: string
    angle: string
    first_round_id: number
    first_state: string
    detail: string | null
  }[]
  person?: Person
  claims?: Claim[]
  accounts?: Account[]
  documents?: Material[]
  checks?: Coverage[]
  seed_status: string
  goal_version: number
}
const props = defineProps<{ eventId: number; selected: number; canRefresh: boolean }>()
const emit = defineEmits<{ message: [s: string] }>()
const state = ref<Result | null>(null),
  error = ref(''),
  busy = ref(false),
  filter = ref('')
const base = '/events/' + props.eventId + '/people'
const link = (p?: number) => '#/events/' + props.eventId + '/people' + (p ? '/' + p : '')
const doc = (d: number, f?: number, event = props.eventId) =>
  '#/events/' + event + '/doc/' + d + (f ? '/' + f : '')
const sources: Record<string, string> = {
  x: 'X',
  weibo: '微博',
  xiaohongshu: '小红书',
  news: '新闻与公告',
  history: '历史资料',
}
const checkStates: Record<string, string> = {
  unavailable: '未能核查',
  partial: '部分可读',
  found: '取得新材料',
  checked: '已检索',
}
const current = computed(() => state.value?.person)
const docs = computed(
  () => state.value?.documents?.filter((m) => !filter.value || m.channel === filter.value) || []
)
const coverage = computed(
  () =>
    state.value?.checks?.filter((c, i, a) => a.findIndex((x) => x.channel === c.channel) === i) ||
    []
)
async function load() {
  try {
    state.value = await api<Result>(base + (props.selected ? '?target=' + props.selected : ''))
    error.value = ''
  } catch (e) {
    error.value = (e as Error).message
  }
}
async function refresh() {
  busy.value = true
  try {
    const x = await api<{ round: number }>(base + '/refresh', {})
    emit('message', '已安排人物专项补查 · 轮次 #' + x.round)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
async function review(a: Account, confirmed: boolean) {
  busy.value = true
  try {
    await api(base + '/' + props.selected + '/accounts/' + a.id, { confirmed })
    await load()
    emit('message', confirmed ? '已确认公开账号归属' : '已撤下该账号归属')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
onMounted(load)
</script>
<template>
  <p v-if="error" class="error" role="alert">
    {{ error }} <button class="btn quiet" @click="load">重试</button>
  </p>
  <p v-if="!state && !error" class="loading">正在读取相关人物…</p>
  <div v-if="state" class="stack people-workspace">
    <div class="section-head">
      <h2>
        <UsersRound />{{ current ? '人物档案' : '相关人物' }}
        <small v-if="!current">{{
          state.people.length + (state.pending_people?.length || 0)
        }}</small>
      </h2>
      <button v-if="canRefresh" class="btn" :disabled="busy" @click="refresh">
        <RefreshCw />补查人物与往事
      </button>
    </div>
    <template v-if="!current">
      <div v-for="f in state.pending_people" :key="'requested-' + f.id" class="panel">
        <p class="meta">每轮核查 · 身份待核实</p>
        <h3>{{ f.target }}</h3>
        <p v-if="f.angle">{{ f.angle }}</p>
        <p>{{ f.detail || '首次核查已安排，等待具名来源。' }}</p>
        <a :href="'#/events/' + eventId + '/research'"
          >查看收集范围及轮次 #{{ f.first_round_id }}</a
        >
      </div>
      <div v-if="!state.people.length && !state.pending_people?.length" class="empty">
        <UsersRound /><strong>尚未核实与主题相关的人物</strong
        ><span>{{ state.seed_status || '追踪轮次会从具名报道、官方名单和采访中辨认人物。' }}</span>
      </div>
      <a v-for="p in state.people" :key="p.id" :href="link(p.id)" class="panel doc-link">
        <span class="eyebrow"
          >{{ p.role }} ·
          {{
            p.requested
              ? '用户指定 · 每轮核查'
              : p.goal_version === state.goal_version
                ? '当前目标'
                : '历史目标'
          }}</span
        >
        <h3>{{ p.name }} <ArrowUpRight /></h3>
        <p>{{ p.identity }}</p>
        <p class="scope">{{ p.relevance }}</p>
        <small class="meta">{{ p.materials }} 份关联材料</small>
      </a>
    </template>
    <template v-else>
      <a :href="link()" class="backlink"><ArrowLeft />所有相关人物</a>
      <section class="panel">
        <div class="eyebrow">{{ current.role }} · 世界人物实体 #{{ current.entity_id }}</div>
        <h2>{{ current.name }}</h2>
        <p>
          <strong>{{ current.identity }}</strong>
        </p>
        <p>{{ current.relevance }}</p>
        <blockquote>{{ current.quote }}</blockquote>
        <a :href="doc(current.document_id, current.fragment_id)"><Link2 />核对入选依据</a>
      </section>
      <div class="split">
        <section class="stack">
          <div class="panel">
            <header class="panel-head"><h2>人物分析</h2></header>
            <p v-if="!state.claims?.length" class="rail-item">
              等待带引文的分析进入本事件知识索引。
            </p>
            <article v-for="c in state.claims" :key="c.id" class="rail-item">
              <span class="eyebrow">{{ label(c.kind) }} · {{ stamp(c.created_at, true) }}</span>
              <p v-if="c.attribution_review" class="warning">
                引用帖的账号归属已转为待核，请核查原文和当前账号记录。
              </p>
              <p>{{ c.text }}</p>
              <dl v-if="Object.keys(c.facets).length">
                <template v-for="(s, k) in c.facets" :key="k"
                  ><dt>{{ k }}</dt>
                  <dd>{{ s }}</dd></template
                >
              </dl>
              <div class="evidence">
                <a :href="'#/world/' + c.world_id + '/' + c.id">知识版本 v{{ c.world_version }}</a>
                <a
                  v-for="q in c.citations"
                  :key="q.fragment_id"
                  :href="doc(q.document_id, q.fragment_id)"
                  >引文 #{{ q.fragment_id }}</a
                >
              </div>
            </article>
          </div>
          <div class="panel">
            <header class="panel-head">
              <h2><FileText />相关动态与往事</h2>
              <select v-model="filter" aria-label="人物资料来源">
                <option value="">全部来源</option>
                <option v-for="(name, id) in sources" :value="id" :key="id">{{ name }}</option>
              </select>
            </header>
            <p v-if="!docs.length" class="rail-item">该范围内尚无归档材料。</p>
            <article v-for="m in docs" :key="m.source_url + '-' + m.document_id" class="rail-item">
              <span class="category">{{ sources[m.channel] }} · {{ m.attribution }}</span>
              <small
                >{{ m.period === 'history' ? '历史补查' : '本轮窗口' }} ·
                {{ m.source_at ? stamp(m.source_at, true) : '来源时间待核' }}</small
              >
              <a :href="doc(m.document_id, m.fragment_id)">{{ m.title }}</a>
              <p v-if="m.excerpt" class="scope clamp">{{ m.excerpt }}</p>
            </article>
          </div>
        </section>
        <aside class="stack">
          <section class="panel">
            <header class="panel-head"><h2>公开账号与归属</h2></header>
            <p v-if="!state.accounts?.length" class="rail-item">
              目前缺少能将平台账号与此人对应的公开资料。
            </p>
            <article v-for="a in state.accounts" :key="a.id" class="rail-item">
              <strong>{{ sources[a.platform] }} · {{ a.account_key }}</strong
              ><small>{{
                { confirmed: '归属已核实', candidate: '归属待核', rejected: '已排除' }[a.state]
              }}</small>
              <p class="scope">{{ a.proof_text }}</p>
              <div class="evidence">
                <a :href="doc(a.proof_doc, undefined, a.proof_event)">身份出处</a
                ><a :href="a.url" target="_blank" rel="noopener noreferrer">公开主页</a>
              </div>
              <div class="actions spaced">
                <button
                  v-if="a.state !== 'confirmed'"
                  class="btn quiet"
                  :disabled="busy"
                  @click="review(a, true)"
                >
                  <Check />确认归属</button
                ><button
                  v-if="a.state !== 'rejected'"
                  class="btn quiet"
                  :disabled="busy"
                  @click="review(a, false)"
                >
                  排除该账号
                </button>
              </div>
            </article>
          </section>
          <section class="panel">
            <header class="panel-head"><h2>各来源最近核查</h2></header>
            <p v-if="!coverage.length" class="rail-item">等待下一次人物巡检。</p>
            <div v-for="c in coverage" :key="c.channel" class="rail-item">
              <strong>{{ sources[c.channel] }} · {{ checkStates[c.status] }}</strong
              ><small>{{ stamp(c.checked_at, true) }} · 轮次 #{{ c.round_id }}</small>
              <p>{{ c.detail }}</p>
            </div>
          </section>
        </aside>
      </div>
    </template>
  </div>
</template>
<style scoped>
.people-workspace h2 svg,
.people-workspace h3 svg,
.people-workspace a svg {
  width: 16px;
  height: 16px;
  vertical-align: middle;
}
.people-workspace blockquote {
  border-left: 3px solid var(--teal);
  background: var(--canvas);
  padding: 9px 14px;
  margin: 12px 0;
  color: var(--sub);
}
.people-workspace dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 5px 11px;
  font-size: 0.9rem;
}
.people-workspace dt {
  font-weight: 650;
  color: var(--sub);
}
.people-workspace dd {
  margin: 0;
}
.people-workspace .rail-item small {
  display: block;
}
.people-workspace .rail-item p {
  overflow-wrap: anywhere;
}
</style>
