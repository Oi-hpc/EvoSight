<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { BookOpen, FileText, History, Search, Settings2 } from 'lucide-vue-next'
import { api, host, knowledgeKind, label, stamp, type CollectionFocus, type Home } from './data'
import ResearchInbox from './ResearchInbox.vue'
import ResearchSkills from './ResearchSkills.vue'
type Cite = {
  fragment_id: number
  document_id: number
  quote: string
  locator: string
  link: string
  title: string
}
type Entry = {
  world_id?: number
  world_version?: number
  index_reason?: string
  facets?: Record<string, string | number>
  role?: string
  id: number
  subject?: string
  text?: string
  title?: string
  body?: string
  kind?: string
  reason?: string
  section?: string
  occurred_at?: string
  created_at?: string
  precision?: string
  citations?: Cite[]
  refs?: number[]
  prediction_id?: number
  aliases?: string[]
  predictions?: { id: number; question: string }[]
}
type Source = {
  url: string
  name: string
  reason: string
  watching: number
  error: string
  success_at: string
}
type Goal = {
  version: number
  title: string
  scope: string
  priority: number
  related: number[]
}
type Knowledge = {
  delivery_snapshot?: number | null
  delivery?: {
    position: number
    question: string
    state: 'confirmed' | 'unknown' | 'missing' | 'rejected'
    answer: string
    reason: string
    citations: Pick<Cite, 'fragment_id' | 'document_id' | 'quote'>[]
  }[]
  rejections?: { task_id: number; subject: string; reason: string }[]
  board?: {
    guides: { id: number; question: string; reason: string; wake: string; query: { path?: string; query?: string; entity_id?: number; entry_id?: number }; candidates: { entry_id: number; version_id: number; subject: string }[] }[]
    notes: { id: number; subject: string; body: string; reason: string; citations: { document_id: number; fragment_id: number; quote: string }[] }[]
    lines?: { id: number; question: string; role: string; state: string; done_when: string; searches?: string[]; findings?: { subject: string; text: string }[] }[]
  }
  goal: Goal
  event_status: string
  snapshot: number | null
  summary: string
  gaps: string[]
  changes: Entry[]
  diff: { added: number[]; removed: number[] } | null
  sections: Record<string, Entry[]>
  section_names: Record<string, string>
  versions: { id: number; created_at: string; summary: string }[]
  sources: Source[]
  lessons?: {
    id: number
    event_id: number
    title: string
    question: string
    state: string
    reviews: {
      event_id: number
      title: string
      outcome: string
      reason: string
      document_id: number
    }[]
  }[]
  attempts: { url: string; state: string; reason: string }[]
  observations: {
    id: number
    origin: string
    question: string
    condition: string
    state: string
    answer: string
  }[]
  closure: { id: number; reason: string } | null
}
type Task = {
  line_key?: string | null
  rejections?: { subject: string; reason: string }[]
  id: number
  role_name: string
  question: string
  state: string
  time_scope: string
  done_when: string
  detail: string
}
type Round = {
  id: number
  goal_version: number
  reasons: string[]
  window_start: string
  window_end: string
  state: string
  detail: string
  evidence_preview?: {
    published_at: string
    excerpts: {
      line_key?: string | null
      task_id?: number | null
      question: string
      quote: string
      fragment_id: number
      document_id: number
      title: string
      locator: string
      in_event: boolean
      url: string
    }[]
    pending: string[]
  } | null
  plan: { rationale: string; postponed: string[] } | null
  tasks: Task[]
  coverage: { url: string; state: string; checked_at: string; recovery_round: number | null }[]
  handoffs?: {
    id: number; recipient: string; body: string; reason: string; documents: number[]
    received_run: number | null; job_status: string | null; info: string | null; snapshot_id: number | null
    forwarded_round: number | null; stopped_reason: string | null
  }[]
  runs: {
    id: number
    role: string
    model: string
    effort: string
    status: string
    elapsed: number | null
  }[]
}
const props = defineProps<{
  eventId: number
  mode: string
  selected: number
  roundId: number
  deliveryItem: number
  demo: boolean
}>()
const emit = defineEmits<{ changed: [] }>()
const inbox = ref<InstanceType<typeof ResearchInbox>>()
const skillPanel = ref<InstanceType<typeof ResearchSkills>>()
const data = ref<Knowledge>(),
  rounds = ref<Round[]>([]),
  collection = ref<CollectionFocus[]>([]),
  section = ref('background'),
  revision = ref(0),
  compare = ref(0),
  search = ref('')
const editing = ref(false),
  draft = ref<Goal>(),
  choices = ref<{ id: number; title: string }[]>([]),
  question = ref(''),
  condition = ref('')
const status = ref(''),
  error = ref(''),
  busy = ref(false)
const correction = ref<{
  id: number
  expected_snapshot: number
  text: string
  reason: string
  fragment_id: number
  quote: string
}>()
const removal = ref<{ entry: number; expected: number; reason: string }>()
async function removeLink() {
  if (!removal.value) return
  const { entry, expected, reason } = removal.value
  if (
    await action(
      '/knowledge/links/' + entry + '/remove',
      { expected_snapshot: expected, reason },
      '已从本事件的索引移出'
    )
  )
    removal.value = undefined
}
const lessonReview = ref<{
  id: number
  outcome: string
  reason: string
  fragment_id: number | null
}>()
const outcomeLabel: Record<string, string> = {
  support: '独立案例支持',
  counter: '发现反例',
  withdraw: '已撤回',
}
async function saveLesson() {
  if (!lessonReview.value) return
  const { id, ...body } = lessonReview.value
  if (await action('/lessons/' + id + '/reviews', body, '经验复核已保存'))
    lessonReview.value = undefined
}
function beginCorrection(v: Entry) {
  const cite = v.citations?.[0]
  if (!cite || !data.value?.snapshot) return
  correction.value = {
    id: v.id,
    expected_snapshot: data.value.snapshot,
    text: v.text || '',
    reason: '',
    fragment_id: cite.fragment_id,
    quote: cite.quote,
  }
}
async function saveCorrection() {
  if (!correction.value) return
  const { id, ...payload } = correction.value
  if (await action('/knowledge/claims/' + id + '/corrections', payload, '共享条目的订正已保存'))
    correction.value = undefined
}
let sequence = 0,
  timer: ReturnType<typeof setInterval> | undefined
const base = computed(() => `/events/${props.eventId}`)
async function load() {
  const n = ++sequence
  const params = new URLSearchParams()
  if (revision.value) params.set('revision', String(revision.value))
  if (compare.value) params.set('compare', String(compare.value))
  if (search.value.trim()) params.set('q', search.value.trim())
  try {
    const [k, r, f] = await Promise.all([
      api<Knowledge>(base.value + '/knowledge?' + params),
      props.mode === 'research'
        ? api<Round[]>(base.value + '/rounds' + (props.roundId ? '?rid=' + props.roundId : ''))
        : Promise.resolve([]),
      props.mode === 'research'
        ? api<CollectionFocus[]>(base.value + '/focus')
        : Promise.resolve([]),
    ])
    if (n === sequence) {
      data.value = k
      rounds.value = r
      collection.value = f
      error.value = ''
    }
  } catch (e) {
    if (n === sequence) error.value = (e as Error).message
  }
}
watch(
  () => [props.eventId, props.selected],
  () => {
    revision.value = props.selected || 0
    compare.value = 0
    editing.value = false
    correction.value = undefined
    lessonReview.value = undefined
  },
  { immediate: true }
)
watch(
  () => [props.mode, props.eventId, props.roundId, revision.value, compare.value, search.value],
  load,
  {
    immediate: true,
  }
)
watch(
  () => [props.mode, props.deliveryItem, props.selected, data.value?.snapshot, data.value?.delivery_snapshot],
  async () => {
    if (props.mode !== 'knowledge' || !props.deliveryItem || data.value?.snapshot !== props.selected ||
        !data.value?.delivery?.some(i => i.position === props.deliveryItem)) return
    section.value = 'background'
    await nextTick()
    document.getElementById('delivery-' + props.deliveryItem)?.scrollIntoView({ block: 'center' })
  }
)
onMounted(() => {
  timer = setInterval(() => {
    if (
      document.visibilityState === 'visible' &&
      !editing.value &&
      !correction.value &&
      !busy.value &&
      !revision.value
    )
      void load()
  }, 10000)
})
onBeforeUnmount(() => {
  clearInterval(timer)
  sequence++
})
async function action(path: string, body: unknown, message: string, method = 'POST') {
  if (busy.value) return false
  busy.value = true
  error.value = ''
  try {
    await api(base.value + path, body, method)
    await load()
    status.value = message
    emit('changed')
    return true
  } catch (e) {
    const msg = (e as Error).message
    await load()
    error.value = msg
    return false
  } finally {
    busy.value = false
  }
}
async function edit() {
  if (!data.value) return
  draft.value = { ...data.value.goal, related: [...data.value.goal.related] }
  editing.value = true
  try {
    choices.value = (await api<Home>('/home')).cards.filter((c) => c.id !== props.eventId)
  } catch (e) {
    error.value = (e as Error).message
  }
}
async function save() {
  const d = draft.value
  if (!d) return
  if (
    await action(
      '',
      {
        title: d.title,
        scope: d.scope,
        priority: d.priority,
        related: d.related,
        expected_version: d.version,
      },
      '已保存目标并安排重规划',
      'PATCH'
    )
  )
    editing.value = false
}
async function research() {
  if (
    await action(
      '/questions',
      { question: question.value, condition: condition.value },
      '专项研究已排队'
    )
  ) {
    question.value = ''
    condition.value = ''
  }
}
const documents = (id: number, fragment?: number) =>
  `#/events/${props.eventId}/doc/${id}` + (fragment ? '/' + fragment : '')
const entries = computed(() => data.value?.sections[section.value] || [])
const deliveryLabels = { confirmed: '已写入', unknown: '已写明证据缺口', missing: '漏写', rejected: '答复被拒' }
</script>
<template>
  <p v-if="error" class="error" role="alert">
    {{ error }} <button class="btn quiet" @click="load">重新读取</button>
  </p>
  <p v-if="status" class="notice" role="status">{{ status }}</p>
  <div v-if="!data && !error" class="loading">正在读取研究档案…</div>
  <template v-if="data">
    <div v-if="mode === 'knowledge'" class="kb-layout">
      <nav class="panel kb-nav" aria-label="知识章节">
        <a
          v-for="(name, key) in data.section_names"
          :key="key"
          href="#"
          :class="{ chosen: section === key }"
          @click.prevent="section = String(key)"
          >{{ name }} <small>{{ data.sections[key]?.length || 0 }}</small></a
        >
      </nav>
      <div class="stack">
        <section class="panel kb-content">
          <header class="panel-head">
            <h2><BookOpen />{{ data.section_names[section] }}</h2>
            <a :href="'#/world/0/0/' + eventId">{{ demo ? '世界知识库' : '从世界知识库引用' }}</a>
          </header>
          <div class="toolbar">
            <label
              ><History />索引版本
              <select v-model.number="revision">
                <option :value="0">当前版本</option>
                <option v-for="v in data.versions" :value="v.id" :key="v.id">
                  #{{ v.id }} · {{ stamp(v.created_at, true) }}
                </option>
              </select></label
            >
            <label v-if="data.versions.length > 1"
              >与
              <select v-model.number="compare">
                <option :value="0">选择版本比较</option>
                <option
                  v-for="v in data.versions.filter((x) => x.id !== (revision || data?.snapshot))"
                  :key="v.id"
                  :value="v.id"
                >
                  #{{ v.id }} · {{ stamp(v.created_at, true) }}
                </option>
              </select></label
            >
            <label
              ><Search /><input
                v-model="search"
                placeholder="搜索条目、引文或别名"
                aria-label="搜索事件知识索引"
            /></label>
          </div>
          <p v-if="data.diff" class="notice">
            相比 #{{ compare }} 新增 {{ data.diff.added.length }} 条，移出
            {{ data.diff.removed.length }} 条。
          </p>
          <p v-if="section === 'background' && data.summary" class="kb-summary">
            {{ data.summary }}
          </p>
          <section v-if="section === 'background' && data.delivery?.length" aria-label="交付核对">
            <h3>交付核对</h3>
            <p v-if="data.delivery_snapshot !== data.snapshot" class="meta">最近一次研究报告来自版本 #{{ data.delivery_snapshot }}。</p>
            <article v-for="item in data.delivery" :key="item.position" :id="'delivery-' + item.position"
              class="rail-item" :class="{ 'delivery-focus': deliveryItem === item.position && selected === data.snapshot }">
              <strong>{{ item.question }}</strong>
              <small> · {{ deliveryLabels[item.state] }}</small>
              <p v-if="item.answer || item.reason">{{ item.answer || item.reason }}</p>
              <details v-if="item.citations.length">
                <summary>查看原文依据（{{ item.citations.length }}）</summary>
                <blockquote v-for="cite in item.citations" :key="cite.fragment_id + cite.quote">
                  {{ cite.quote }} <a :href="documents(cite.document_id, cite.fragment_id)">查看片段 #{{ cite.fragment_id }}</a>
                </blockquote>
              </details>
            </article>
          </section>
          <details v-if="section === 'background' && data.rejections?.length">
            <summary>调查中曾被退回的提议（{{ data.rejections.length }}）</summary>
            <p v-for="r in data.rejections" :key="r.task_id + r.subject + r.reason">调查 #{{ r.task_id }} · {{ r.subject }} · {{ r.reason }}</p>
          </details>
          <p v-if="!entries.length" class="rail-item">
            {{ data.snapshot ? '这个索引章节暂无符合条件的条目。' : '等待建立事件知识索引。' }}
            <a :href="`#/events/${eventId}/research`">查看研究进度</a>
          </p>
          <article v-for="v in entries" :key="v.id" class="rail-item kb-entry">
            <small v-if="v.kind"
              >{{ v.world_id ? knowledgeKind(v.kind) : label(v.kind) }}
              <span v-if="v.occurred_at">
                · 发生于
                {{ v.precision === 'date' ? v.occurred_at : stamp(v.occurred_at, true) }}</span
              ></small
            >
            <h3>{{ v.subject || v.title }}</h3>
            <p>{{ v.text || v.body }}</p>
            <p v-if="v.world_id" class="meta">
              <a :href="'#/world/' + v.world_id + '/' + (revision ? v.id : 0)"
                >世界条目 #{{ v.world_id }} · 版本 {{ v.world_version }}</a
              >
              · {{ v.index_reason }}
            </p>
            <small v-if="section === 'index' && v.created_at"
              >收录于 {{ stamp(v.created_at, true)
              }}<template v-if="v.aliases?.length"> · {{ v.aliases.join('、') }}</template></small
            >
            <a v-for="p in v.predictions" :key="p.id" :href="'#/events/' + eventId + '/p/' + p.id"
              >使用同一证据的预测 · {{ p.question }}</a
            >
            <p v-if="v.reason" class="meta">{{ v.reason }}</p>
            <small v-if="v.role === 'user'">本机用户订正</small>
            <dl v-if="v.facets && Object.keys(v.facets).length" class="kb-facets">
              <template v-for="(value, key) in v.facets" :key="key"
                ><dt>{{ key }}</dt>
                <dd>{{ value }}</dd></template
              >
            </dl>
            <div v-if="v.citations?.length">
              <blockquote v-for="c in v.citations" :key="c.fragment_id">
                <p>{{ c.quote }}</p>
                <a :href="documents(c.document_id, c.fragment_id)">{{ c.title }}</a> ·
                <a :href="c.link" target="_blank" rel="noopener noreferrer">{{ c.locator }}</a>
              </blockquote>
            </div>
            <template v-else
              ><a
                v-for="refId in v.refs"
                :key="refId"
                :href="documents(refId)"
                class="evidence-link"
                >证据 #{{ refId }}
              </a></template
            >
            <button
              v-if="section === 'index' && v.section"
              class="btn quiet"
              @click="section = v.section"
            >
              打开{{ data.section_names[v.section] }}
            </button>
            <button
              v-if="
                section === 'index' &&
                !demo &&
                !revision &&
                v.world_id &&
                data.snapshot &&
                removal?.entry !== v.world_id
              "
              class="btn quiet"
              @click="removal = { entry: v.world_id, expected: data.snapshot, reason: '' }"
            >
              移出本事件索引
            </button>
            <form
              v-if="removal && removal.entry === v.world_id && section === 'index' && !revision"
              class="kb-form"
              @submit.prevent="removeLink"
            >
              <label
                >移出理由<input v-model="removal.reason" required minlength="2" maxlength="400"
              /></label>
              <div class="actions">
                <button class="btn" :disabled="busy">保存索引变更</button>
                <button type="button" class="btn quiet" @click="removal = undefined">取消</button>
              </div>
            </form>
            <button
              v-if="!demo && !revision && v.citations?.length && correction?.id !== v.id"
              class="btn quiet"
              @click="beginCorrection(v)"
            >
              订正共享条目
            </button>
            <form
              v-if="correction?.id === v.id && correction"
              class="kb-form"
              @submit.prevent="saveCorrection"
            >
              <p class="meta">订正会更新引用这条知识的事件。历史索引和已经登记的预测保留原版本。</p>
              <label
                >订正后的结论<textarea v-model="correction.text" required maxlength="1600" />
              </label>
              <label>订正理由<input v-model="correction.reason" required maxlength="800" /></label>
              <label
                >依据的证据片段编号<input
                  v-model.number="correction.fragment_id"
                  type="number"
                  min="1"
                  required
              /></label>
              <label
                >原文引文<textarea
                  v-model="correction.quote"
                  required
                  minlength="2"
                  maxlength="800"
                />
              </label>
              <div class="actions">
                <button class="btn primary" :disabled="busy">保存订正</button
                ><button type="button" class="btn" @click="correction = undefined">取消</button>
              </div>
            </form>
            <a v-if="v.prediction_id" :href="`#/events/${eventId}/p/${v.prediction_id}`"
              >查看预测</a
            >
          </article>
        </section>
        <section v-if="data.board?.guides.length" class="panel">
          <header class="panel-head"><h2>世界知识查阅指南</h2></header>
          <details v-for="g in data.board.guides" :key="g.id" class="rail-item">
            <summary>{{ g.question }}</summary>
            <p>{{ g.reason }} · {{ g.wake }}</p>
            <p>{{ g.query.path }} {{ g.query.query }} {{ g.query.entry_id ? '条目 #' + g.query.entry_id : '' }}</p>
            <p v-if="!g.candidates.length">等待匹配材料</p>
            <p v-for="h in g.candidates" :key="h.version_id"><a :href="`#/world/${h.entry_id}/${h.version_id}/${eventId}`">候选 #{{ h.entry_id }} · {{ h.subject }}</a></p>
          </details>
        </section>
        <section v-if="data.board?.lines?.length" class="panel">
          <header class="panel-head"><h2>调查线</h2></header>
          <details v-for="line in data.board.lines" :key="line.id" class="rail-item">
            <summary>{{ line.question }} · {{ label(line.state) }}</summary>
            <p>{{ label(line.role) }} · {{ line.done_when }}</p>
            <p v-for="(query, i) in line.searches" :key="i">已查 {{ query }}</p>
            <p v-for="(finding, i) in line.findings" :key="i"><strong>{{ finding.subject }}</strong> · {{ finding.text }}</p>
          </details>
        </section>
        <section v-if="data.board?.notes.length" class="panel">
          <header class="panel-head"><h2>事件专属材料</h2></header>
          <details v-for="n in data.board.notes" :key="n.id" class="rail-item">
            <summary>{{ n.subject }}</summary><p>{{ n.body }}</p><small>{{ n.reason }}</small>
            <p v-for="(ref, i) in n.citations" :key="i"><a :href="documents(ref.document_id, ref.fragment_id)">材料 #{{ ref.document_id }}</a> · {{ ref.quote }}</p>
          </details>
        </section>
        <section v-if="data.gaps.length" class="panel">
          <header class="panel-head"><h2>尚待核实</h2></header>
          <p v-for="g in data.gaps" :key="g" class="rail-item">{{ g }}</p>
        </section>
      </div>
    </div>
    <div v-else class="split">
      <div class="stack">
        <ResearchInbox v-if="!demo" ref="inbox" :event-id="eventId" :read-only="data.event_status !== 'active'" />
        <section v-if="collection.length" class="panel">
          <header class="panel-head">
            <h2>持续收集范围</h2>
            <a :href="'#/events/' + eventId + '/discussion'">在讨论中添加</a>
          </header>
          <div v-for="f in collection" :key="f.id" class="rail-item">
            <strong>{{ f.target }}</strong> · {{ f.kind === 'person' ? '人物' : '事项' }} ·
            {{ f.active ? '每轮核查' : '已暂停' }}
            <p v-if="f.angle">{{ f.angle }}</p>
            <small v-if="f.checked_at"
              >最近核查 {{ stamp(f.checked_at, true) }} · 轮次 #{{ f.last_round }}</small
            >
            <small v-else>首次核查 #{{ f.first_round_id }} · {{ label(f.first_state) }}</small>
            <p v-if="f.detail">{{ f.detail }}</p>
            <ul v-if="f.documents?.length">
              <li v-for="d in f.documents" :key="d.id">
                <a :href="documents(d.id)">{{ d.title }}</a>
              </li>
            </ul>
            <a
              v-if="f.person_target_id"
              :href="'#/events/' + eventId + '/people/' + f.person_target_id"
              >查看人物档案</a
            >
            <button
              v-if="f.active && !demo"
              class="btn quiet"
              :disabled="busy"
              @click="action('/focus/' + f.id + '/pause', {}, '已暂停此项的每轮核查')"
            >
              暂停此项
            </button>
          </div>
        </section>
        <section class="panel">
          <header class="panel-head">
            <h2><History />研究轮次</h2>
          </header>
          <a v-if="roundId" class="backlink" :href="'#/events/' + eventId + '/research'"
            >所有研究轮次</a
          >
          <p v-if="!rounds.length" class="rail-item">暂无研究轮次。</p>
          <details v-for="(r, i) in rounds" :key="r.id" class="rail-item" :open="i === 0">
            <summary>#{{ r.id }} · {{ r.reasons.join('、') }} · {{ label(r.state) }}</summary>
            <p class="meta">
              {{ stamp(r.window_start, true) }} 至 {{ stamp(r.window_end, true) }} · 目标 V{{
                r.goal_version
              }}
            </p>
            <p>{{ r.detail }}</p>
            <button v-if="!demo && data.event_status === 'active' && data.goal.version === r.goal_version" class="btn quiet"
              @click="inbox?.pointTo(r.id, r.goal_version)">就此轮补充要求</button>
            <details v-if="r.evidence_preview" :open="r.state === 'researching'" class="kb-task">
              <summary>阶段性材料 · {{ stamp(r.evidence_preview.published_at, true) }}</summary>
              <p v-for="(excerpt, n) in r.evidence_preview.excerpts" :key="n">
                <strong>{{ excerpt.question }}</strong><br />
                <q>{{ excerpt.quote }}</q>
                <a v-if="excerpt.in_event" :href="documents(excerpt.document_id, excerpt.fragment_id)">
                  {{ excerpt.title }} · {{ excerpt.locator || '查看原文' }}
                </a>
                <a v-else-if="excerpt.url" :href="excerpt.url" target="_blank" rel="noopener noreferrer">
                  {{ excerpt.title }} · 发布方原文
                </a>
                <button v-if="!demo && data.event_status === 'active' && data.goal.version === r.goal_version" class="btn quiet"
                  @click="inbox?.pointTo(r.id, r.goal_version, excerpt.line_key || r.tasks.find(t => t.id === excerpt.task_id)?.line_key || '', `请复核材料 #${excerpt.document_id} 的片段 #${excerpt.fragment_id}（${excerpt.title}）。`)">就这条材料补充</button>
              </p>
              <p class="meta">发布时待查</p>
              <ul>
                <li v-for="(q, n) in r.evidence_preview.pending" :key="n">{{ q }}</li>
              </ul>
            </details>
            <details v-if="r.handoffs?.length" class="kb-task">
              <summary>交接与退回（{{ r.handoffs.length }}）</summary>
              <div v-for="h in r.handoffs" :key="h.id">
                <strong>{{ h.recipient }}</strong> · {{ h.reason }}
                <p>{{ h.body }}</p>
                <p class="meta">
                  <template v-if="h.stopped_reason">{{ h.stopped_reason }}</template>
                  <a v-else-if="h.forwarded_round" :href="'#/events/' + eventId + '/research/0/' + h.forwarded_round">
                    已转交轮次 #{{ h.forwarded_round }}
                  </a>
                  <template v-else>
                    {{ h.job_status ? label(h.job_status) : h.received_run ? '已收到' : '待接收' }}
                    <span v-if="h.snapshot_id"> · 已纳入知识版本 #{{ h.snapshot_id }}</span>
                    <span v-else-if="h.info"> · {{ h.info }}</span>
                  </template>
                </p>
                <a v-for="id in h.documents" :key="id" :href="documents(id)">材料 #{{ id }} </a>
              </div>
            </details>
            <details v-if="r.runs?.length" class="kb-runs">
              <summary>{{ r.runs.length }} 次模型调用</summary>
              <p v-for="run in r.runs" :key="run.id">
                {{ label(run.role) }} · {{ run.model }} / {{ run.effort }} · {{ label(run.status)
                }}<span v-if="run.elapsed != null"> · {{ Math.round(run.elapsed) }} 秒</span>
              </p>
            </details>
            <p v-if="r.plan">{{ r.plan.rationale }}</p>
            <ul v-if="r.coverage?.length" class="kb-cover">
              <li v-for="s in r.coverage" :key="s.url">
                <a :href="s.url" target="_blank" rel="noopener noreferrer">{{ host(s.url) }}</a> ·
                {{
                  { covered: '已核查', missing: '此窗口待补查', recovered: '已补齐' }[s.state] ||
                  s.state
                }}
                <span v-if="s.recovery_round"> · 补采轮次 #{{ s.recovery_round }}</span>
              </li>
            </ul>
            <div v-for="t in r.tasks" :key="t.id" class="kb-task">
              <strong>{{ t.role_name }}</strong> · {{ label(t.state) }}
              <p>{{ t.question }}</p>
              <small
                >{{ t.time_scope === 'history' ? '含历史补查' : '本轮窗口' }} ·
                {{ t.done_when }}</small
              >
              <p v-if="t.detail" class="meta">{{ t.detail }}</p>
              <button v-if="!demo && data.event_status === 'active' && data.goal.version === r.goal_version" class="btn quiet"
                @click="inbox?.pointTo(r.id, r.goal_version, t.line_key || '', '关于「' + t.question + '」：')">补充这项调查</button>
              <details v-if="t.rejections?.length">
                <summary>本次调查曾退回的提议（{{ t.rejections.length }}）</summary>
                <p v-for="row in t.rejections" :key="row.subject + row.reason">{{ row.subject }} · {{ row.reason }}</p>
              </details>
              <button
                v-if="
                  ['failed', 'deferred'].includes(t.state) && r.goal_version === data.goal.version
                "
                class="btn quiet"
                :disabled="busy"
                @click="action('/rounds/' + r.id + '/retry?task=' + t.id, {}, '角色任务已排队')"
              >
                重试这个角色
              </button>
            </div>
            <button
              v-if="
                ['waiting', 'partial', 'failed', 'deferred'].includes(r.state) &&
                r.goal_version === data.goal.version
              "
              class="btn"
              :disabled="busy"
              @click="action('/rounds/' + r.id + '/retry', {}, '已安排补查')"
            >
              重试未完成的研究
            </button>
          </details>
        </section>
        <section class="panel">
          <header class="panel-head">
            <h2><FileText />来源与新闻巡检</h2>
          </header>
          <p v-if="!data.sources.length" class="rail-item">等待找到可用的发布方入口。</p>
          <div v-for="s in data.sources" :key="s.url" class="rail-item">
            <a :href="s.url" target="_blank" rel="noopener noreferrer">{{ s.name }}</a>
            <p>{{ s.reason }}</p>
            <small>{{ s.error || '最近成功 ' + stamp(s.success_at, true) }}</small>
            <button
              v-if="!demo"
              class="btn quiet"
              :disabled="busy"
              @click="action('/watch', { url: s.url, watching: !s.watching }, '已更新巡检平台')"
            >
              {{ s.watching ? '停止高频巡检' : '加入高频巡检' }}
            </button>
          </div>
          <details v-if="data.attempts.length" class="rail-item" :open="!data.sources.length">
            <summary>找源记录</summary>
            <p v-for="(a, i) in data.attempts" :key="i">{{ a.url }} · {{ a.reason }}</p>
          </details>
        </section>
      </div>
      <aside class="stack">
        <section class="panel">
          <header class="panel-head">
            <h2><Settings2 />追踪目标 V{{ data.goal.version }}</h2>
          </header>
          <form v-if="editing && draft" class="kb-form" @submit.prevent="save">
            <label>事件名称<input v-model.trim="draft.title" required /></label
            ><label>关注点<textarea v-model.trim="draft.scope" required /></label
            ><label
              >优先级<input v-model.number="draft.priority" type="number" min="1" max="5" /></label
            ><label v-if="choices.length"
              >关联事件<select v-model="draft.related" multiple>
                <option v-for="c in choices" :key="c.id" :value="c.id">
                  {{ c.title }}
                </option>
              </select></label
            >
            <div class="actions">
              <button class="btn primary" :disabled="busy">保存并重规划</button
              ><button type="button" class="btn" @click="editing = false">取消</button>
            </div>
          </form>
          <div v-else class="rail-item">
            <p>{{ data.goal.scope }}</p>
            <p class="meta">优先级 {{ data.goal.priority }} · {{ label(data.event_status) }}</p>
            <button v-if="!demo" class="btn" @click="edit">编辑目标</button>
          </div>
        </section>
        <section v-if="data.closure" class="panel">
          <header class="panel-head"><h2>事件结束建议</h2></header>
          <div class="rail-item">
            <p>{{ data.closure.reason }}</p>
            <div class="actions">
              <button
                class="btn"
                @click="
                  action('/closure/' + data.closure.id, { choice: 'continue' }, '继续观察后续影响')
                "
              >
                继续观察</button
              ><button
                class="btn"
                @click="action('/closure/' + data.closure.id, { choice: 'archived' }, '事件已归档')"
              >
                停止跟踪并归档
              </button>
            </div>
          </div>
        </section>
        <section class="panel">
          <header class="panel-head"><h2>下一轮观察</h2></header>
          <div
            v-for="o in data.observations.filter((v) => v.origin !== '待验证经验')"
            :key="o.id"
            class="rail-item"
          >
            <small>{{ o.origin }} · {{ label(o.state) }}</small>
            <h3>{{ o.question }}</h3>
            <p>{{ o.answer || o.condition }}</p>
          </div>
          <form
            v-if="!demo && data.event_status === 'active'"
            class="kb-form"
            @submit.prevent="research"
          >
            <label>专项研究问题<input v-model.trim="question" required minlength="2" /></label
            ><label>什么证据能回答它<input v-model.trim="condition" required minlength="2" /></label
            ><button class="btn" :disabled="busy">加入本轮研究</button>
          </form>
        </section>
        <section v-if="data.lessons?.length" class="panel">
          <header class="panel-head"><h2>可复用的复盘经验</h2></header>
          <article v-for="l in data.lessons" :key="l.id" class="rail-item">
            <small
              >来自 {{ l.title }} · {{ l.state === 'withdrawn' ? '已撤回' : '待独立验证' }}</small
            >
            <h3>{{ l.question }}</h3>
            <button v-if="!demo && l.event_id === eventId && (l.state !== 'withdrawn' || skillPanel?.has(l.id))"
              type="button" class="btn quiet" @click="skillPanel?.propose(l.id)">
              {{ skillPanel?.has(l.id) ? '查看调查方法' : '提炼为候选方法' }}
            </button>
            <p v-for="r in l.reviews" :key="r.event_id + '-' + r.reason">
              {{ outcomeLabel[r.outcome] }} · {{ r.reason }}
              <a :href="'#/events/' + r.event_id + '/doc/' + r.document_id">{{ r.title }}的证据</a>
            </p>
            <button
              v-if="!demo && l.state !== 'withdrawn' && lessonReview?.id !== l.id"
              class="btn quiet"
              @click="
                lessonReview = {
                  id: l.id,
                  outcome: l.event_id === eventId ? 'withdraw' : 'support',
                  fragment_id: null,
                  reason: '',
                }
              "
            >
              记录复核
            </button>
            <form
              v-if="lessonReview?.id === l.id && lessonReview"
              class="kb-form"
              @submit.prevent="saveLesson"
            >
              <label
                >复核结果<select v-model="lessonReview.outcome">
                  <option v-if="l.event_id === eventId" value="withdraw">撤回经验</option>
                  <template v-else
                    ><option value="support">独立案例支持</option>
                    <option value="counter">发现反例</option></template
                  >
                </select></label
              >
              <label
                >本事件证据片段编号<input
                  v-model.number="lessonReview.fragment_id"
                  type="number"
                  min="1"
                  required
              /></label>
              <label
                >适用条件或反例<textarea
                  v-model="lessonReview.reason"
                  required
                  minlength="2"
                  maxlength="800"
                />
              </label>
              <div class="actions">
                <button class="btn primary" :disabled="busy">保存复核</button
                ><button type="button" class="btn" @click="lessonReview = undefined">取消</button>
              </div>
            </form>
          </article>
        </section>
        <ResearchSkills v-if="!demo" ref="skillPanel" :event-id="eventId" :lessons="data.lessons || []" />
      </aside>
    </div>
  </template>
</template>
<style scoped>
.kb-layout {
  display: grid;
  grid-template-columns: minmax(145px, 200px) minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}
.kb-nav {
  display: grid;
  padding: 6px;
}
.kb-nav a {
  display: flex;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: 6px;
  color: inherit;
  text-decoration: none;
}
.kb-nav .chosen {
  background: var(--accent-soft, #e8f4f1);
  color: #145b50;
  font-weight: 650;
}
.kb-content .toolbar {
  padding: 12px;
  flex-wrap: wrap;
}
.toolbar label {
  display: flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
}
.toolbar svg {
  width: 16px;
}
.kb-summary,
.kb-form {
  padding: 14px;
}
.kb-summary {
  border-left: 3px solid #398372;
}
.kb-facets {
  display: grid;
  grid-template-columns: minmax(6rem, max-content) minmax(0, 1fr);
  gap: 0.3rem 0.8rem;
}
.kb-facets dt {
  color: var(--muted);
}
.kb-facets dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.kb-cover {
  padding-left: 1.2rem;
  font-size: 0.85rem;
}
.kb-form,
.kb-form label {
  display: grid;
  gap: 7px;
}
.kb-form {
  gap: 12px;
}
.kb-form input,
.kb-form textarea,
.kb-form select,
.toolbar input,
.toolbar select {
  min-width: 0;
  max-width: 100%;
  padding: 7px;
  border: 1px solid #bac9c6;
  border-radius: 5px;
  font: inherit;
  background: white;
  color: inherit;
}
.kb-form textarea {
  min-height: 72px;
}
.kb-entry,
.kb-task,
.rail-item {
  overflow-wrap: anywhere;
}
.kb-task,
blockquote {
  margin: 10px 0;
  padding-left: 12px;
  border-left: 2px solid #b3cec6;
}
.kb-entry h3,
.kb-task p,
blockquote p {
  margin: 5px 0;
}
summary {
  cursor: pointer;
}
.delivery-focus { border-left: 3px solid var(--teal); background: var(--mint); }
.kb-entry small,
.kb-task small {
  color: #576c68;
}
@media (max-width: 760px) {
  .kb-layout {
    grid-template-columns: minmax(0, 1fr);
  }
  .kb-nav {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .kb-nav a {
    padding: 7px;
  }
  .toolbar label {
    flex-wrap: wrap;
  }
}
</style>
