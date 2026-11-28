<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  ArrowLeft,
  BookOpen,
  CornerDownLeft,
  Link2,
  MessagesSquare,
  RefreshCw,
  Send,
  Target,
} from 'lucide-vue-next'
import { api, label, stamp, type CollectionFocus } from './data'
import ResearchInbox from './ResearchInbox.vue'

interface Citation {
  key: string
  title: string
  href: string
  period: string[]
}
interface Turn {
  focus_actions?: (CollectionFocus & { added: number; round_id: number; state: string })[]
  workflow_actions?: {
    position: number
    action: string
    event_id: number
    event_title: string
    summary: string
    round_id: number | null
    round_state: string | null
    job_id: number | null
    job_status: string | null
    after_state: { run_at?: string }
  }[]
  event_id: number | null
  event_title: string | null
  revision_id: number | null
  basis: string
  id: number
  provider: string
  question: string
  answer: string | null
  citations: Citation[]
  status: string
  info: string
  model: string | null
  effort: string | null
  created_at: string
  answered_at: string | null
  snapshot_id: number | null
  job_id: number
  run_id: number | null
}
interface Conversation {
  anchor: {
    id: number
    prediction_id: number
    scenario_label?: string
    p: number
    question: string
    model: string
    created_at: string
    run_id: number | null
    snapshot_id: number | null
  } | null
  default_provider: string
  session_provider: string | null
  basis: string
  turns: Turn[]
  has_older: boolean
}
const props = defineProps<{ eventId: number; revisionId: number; latest: boolean; demo: boolean; researchReadOnly?: boolean }>()
const basis = props.latest ? 'latest' : 'original'
const base = props.eventId ? '/events/' + props.eventId + '/discussion' : '/agent/discussion'
const query = '?basis=' + basis + (props.revisionId ? '&revision=' + props.revisionId : '')
const scopeLink = (latest: boolean) =>
  '#/events/' + props.eventId + '/discussion/' + props.revisionId + (latest ? '/1' : '')
const state = ref<Conversation | null>(null),
  turns = ref<Turn[]>([]),
  older = ref(false)
const question = ref(''),
  provider = ref('auto'),
  error = ref(''),
  saving = ref(false)
const field = ref<HTMLTextAreaElement>(),
  conversationLog = ref<HTMLElement>()
interface Archive {
  id: number
  model: string
  effort: string
  input: Record<string, unknown>
  output: { reason: string }
}
const archive = ref<Archive | null>(null),
  archiveBusy = ref(false)
const busy = computed(() =>
  turns.value.some(
    (t) =>
      (props.eventId || t.event_id === null) && ['queued', 'running', 'deferred'].includes(t.status)
  )
)
const anchor = computed(() => state.value?.anchor)
const canSteer = computed(() => !!props.eventId && !props.revisionId && !props.demo)
const mode = ref<'chat' | 'investigation'>('chat')
const pendingQuestions = ref(0)
const switchingBackend = computed(
  () =>
    state.value?.session_provider &&
    provider.value !== 'auto' &&
    provider.value !== state.value.session_provider
)
const suggestions = computed(() =>
  anchor.value?.scenario_label
    ? ['为何把这一档作为主情景？', '哪些反向证据会改变力度或判断信心？']
    : props.revisionId
      ? props.latest
        ? ['哪些新证据改变了原先的判断？', '如果维持原概率，最有力的反证是什么？']
        : ['当时为什么给出这个概率？', '哪条假设一旦失效，最可能改变判断？']
      : props.eventId
        ? ['自上次阅读以来，哪些事实和判断变了？', '目前哪一项不确定性最值得跟踪？']
        : [
            '自上次阅读以来，各事件的事实和判断有哪些变化？',
            '哪些跟踪任务正在运行，下一轮在什么时候？',
          ]
)
let timer: ReturnType<typeof setTimeout> | undefined,
  alive = true,
  fetching = 0
let request: { question: string; provider: string; nonce: string } | null = null
function backend(s: string) {
  return s === 'codex' ? 'Codex' : 'dsh / Kimi'
}
function segments(t: Turn) {
  let strong = false
  return (t.answer || '').split(/(\*\*|\[[A-Z]+\d+\])/).map((text) => {
    if (text === '**') {
      strong = !strong
      text = ''
    }
    return { text, strong, source: t.citations.find((c) => '[' + c.key + ']' === text) }
  })
}
async function openArchive(e: Event) {
  if (!(e.currentTarget as HTMLDetailsElement).open || archive.value || archiveBusy.value) return
  archiveBusy.value = true
  try {
    const result = await api<{ run: Archive | null }>(base + '/archive/' + props.revisionId)
    if (alive) archive.value = result.run
  } catch (e) {
    if (alive) error.value = (e as Error).message
  } finally {
    archiveBusy.value = false
  }
}
function sourceLabel(c: Citation) {
  if (/^W\d+$/.test(c.key)) return '本次讨论取回的原文'
  if (!props.revisionId) return ''
  return c.period.length === 2
    ? '当时及当前'
    : c.period[0] === 'original'
      ? '登记当时'
      : '提问时收录'
}
async function load(before = 0, followLatest = false) {
  const seq = ++fetching
  clearTimeout(timer)
  try {
    const data = await api<Conversation>(base + query + (before ? '&before=' + before : ''))
    if (!alive || seq !== fetching) return
    const log = conversationLog.value
    const priorHeight = log?.scrollHeight || 0
    const priorTop = log?.scrollTop || 0
    const nearEnd = !log || log.scrollHeight - log.scrollTop - log.clientHeight < 60
    state.value = data
    // Preserve older pages while polling the latest pending turn.
    const known = new Map(turns.value.map((t) => [t.id, t]))
    for (const t of data.turns) known.set(t.id, t)
    if (before || !turns.value.length || data.turns[0]?.id === turns.value[0]?.id)
      older.value = data.has_older
    turns.value = [...known.values()].sort((a, b) => a.id - b.id)
    error.value = ''
    await nextTick()
    const updated = conversationLog.value
    if (updated) {
      if (before) updated.scrollTop = priorTop + updated.scrollHeight - priorHeight
      else if (followLatest || nearEnd) updated.scrollTop = updated.scrollHeight
    }
  } catch (e) {
    if (alive && seq === fetching) error.value = (e as Error).message
  } finally {
    if (alive && seq === fetching) {
      const checking = turns.value.some(
        (t) =>
          t.focus_actions?.some((a) => ['queued', 'planning', 'researching'].includes(a.state)) ||
          t.workflow_actions?.some(
            (a) => a.round_state && ['queued', 'planning', 'researching'].includes(a.round_state)
          )
      )
      const scheduled = turns.value.some((t) =>
        t.workflow_actions?.some(
          (a) => a.action === 'schedule_run' && ['queued', 'running'].includes(a.job_status || '')
        )
      )
      if (busy.value || checking || scheduled)
        timer = setTimeout(() => void load(), busy.value ? 2500 : checking ? 8000 : 30000)
    }
  }
}
async function send() {
  const draft = question.value
  const q = draft.trim()
  if (!q || busy.value || saving.value) return
  saving.value = true
  const selectedProvider = provider.value
  const prior = request
  request =
    prior && prior.question === q && prior.provider === selectedProvider
      ? prior
      : { question: q, provider: selectedProvider, nonce: crypto.randomUUID() }
  try {
    await api(base, {
      ...request,
      revision: props.revisionId || null,
      basis,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      utc_offset_minutes: -new Date().getTimezoneOffset(),
    })
    if (!alive) return
    request = null
    if (question.value === draft) question.value = ''
    await load(0, true)
    if (provider.value === selectedProvider && state.value?.session_provider === selectedProvider)
      provider.value = 'auto'
  } catch (e) {
    if (alive) error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}
function onEnter(event: KeyboardEvent) {
  if (event.shiftKey || event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  if (!event.repeat) void send()
}
async function retry(t: Turn) {
  saving.value = true
  try {
    await api('/jobs/' + t.job_id + '/retry', {})
    await load()
  } catch (e) {
    if (alive) error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}
function suggest(text: string) {
  question.value = text
  field.value?.focus()
}
onMounted(() => void load())
onBeforeUnmount(() => {
  alive = false
  clearTimeout(timer)
})
</script>
<template>
  <div class="discussion-pane">
    <div v-if="error" class="error" role="alert">
      {{ error }} <button class="btn quiet" @click="load()">刷新</button>
    </div>
    <p v-if="!state && !error" class="loading">正在读取讨论记录…</p>
    <template v-if="state">
      <header class="discussion-head">
        <div>
          <a
            v-if="anchor"
            class="backlink"
            :href="'#/events/' + eventId + '/p/' + anchor.prediction_id + '/' + anchor.id"
            ><ArrowLeft />返回预测历史</a
          >
          <p v-if="anchor" class="eyebrow">
            概率版本 #{{ anchor.id }} · {{ stamp(anchor.created_at, true) }}
          </p>
          <h2>{{ anchor ? anchor.question : eventId ? '事件讨论' : '全局 Agent' }}</h2>
          <p v-if="!eventId" class="meta">
            查看各事件的进展，用对话新建或修改跟踪、调整周期、安排研究。
          </p>
          <p v-if="anchor" class="meta">
            <strong class="probability">{{ anchor.scenario_label }} {{ anchor.p }}%</strong> ·
            {{ anchor.model }} ·
            {{ anchor.run_id ? '已关联原预测输入与输出' : '按已登记理由与引文解释' }}
          </p>
          <p v-if="demo" class="meta">此讨论使用虚构演示事件的资料。</p>
        </div>
        <div v-if="eventId && (!demo || revisionId)" class="discussion-tools">
          <a v-if="!demo" class="backlink" href="#/agent"><MessagesSquare />全局 Agent</a>
          <div v-if="revisionId" class="discussion-scope" aria-label="讨论资料范围">
            <a
              :href="scopeLink(false)"
              :aria-current="!latest ? 'page' : undefined"
              :class="{ selected: !latest }"
              ><Target />登记当时</a
            >
            <a
              :href="scopeLink(true)"
              :aria-current="latest ? 'page' : undefined"
              :class="{ selected: latest }"
              ><BookOpen />结合最新</a
            >
          </div>
        </div>
      </header>
      <div v-if="canSteer" class="discussion-modes" role="group" aria-label="沟通方式">
        <button type="button" :aria-pressed="mode === 'chat'" @click="mode = 'chat'">与主 Agent 讨论</button>
        <button type="button" :aria-pressed="mode === 'investigation'" @click="mode = 'investigation'">
          调查要求与提问<span v-if="pendingQuestions"> · {{ pendingQuestions }} 待答</span>
        </button>
      </div>
      <ResearchInbox v-if="canSteer" v-show="mode === 'investigation'" :event-id="eventId" :read-only="researchReadOnly" compact @pending="pendingQuestions = $event" />
      <details v-if="anchor?.run_id" class="discussion-archive" @toggle="openArchive">
        <summary>查看原预测输入与登记理由</summary>
        <p v-if="archiveBusy" class="meta">正在读取原预测归档…</p>
        <template v-if="archive"
          ><p>{{ archive.output.reason }}</p>
          <details>
            <summary>
              结构化输入 · 运行 #{{ archive.id }} · {{ archive.model }} / {{ archive.effort }}
            </summary>
            <pre>{{ JSON.stringify(archive.input, null, 2) }}</pre>
          </details>
        </template>
      </details>
      <div
        v-show="mode === 'chat'"
        ref="conversationLog"
        class="discussion-log"
        role="region"
        aria-label="讨论记录"
        tabindex="0"
      >
        <div v-if="older" class="toolbar">
          <button class="btn quiet" @click="load(turns[0].id)">查看更早的讨论</button>
        </div>
        <div v-if="!turns.length" class="discussion-start">
          <MessagesSquare />
          <div>
            <h3>
              {{
                anchor
                  ? latest
                    ? '用新进展重新审视这版预测'
                    : '从这版概率的依据问起'
                  : '从你关心的变化问起'
              }}
            </h3>
            <div class="discussion-prompts">
              <button v-for="s in suggestions" :key="s" @click="suggest(s)">{{ s }}</button>
            </div>
          </div>
        </div>
        <article v-for="t in turns" :key="t.id" class="discussion-turn">
          <div class="discussion-question">
            <span class="eyebrow">你 · {{ stamp(t.created_at, true) }}</span>
            <a
              v-if="!eventId && t.event_id"
              class="meta"
              :href="
                '#/events/' +
                t.event_id +
                '/discussion' +
                (t.revision_id ? '/' + t.revision_id + (t.basis === 'latest' ? '/1' : '') : '')
              "
              >{{ t.event_title }}{{ t.revision_id ? ' · 预测版本 #' + t.revision_id : '' }}</a
            >
            <p>{{ t.question }}</p>
          </div>
          <div class="discussion-reply">
            <header>
              <b>{{ t.answer && !t.run_id ? '工作台' : backend(t.provider) }}</b><small
                class="meta"
                v-if="t.model"
                >{{ t.model }} · {{ t.effort }}</small
              >
              <a
                v-if="t.snapshot_id && t.event_id"
                :href="'#/events/' + t.event_id + '/knowledge/' + t.snapshot_id"
                >知识索引 #{{ t.snapshot_id }}</a
              >
            </header>
            <template v-if="t.answer">
              <p class="discussion-prose">
                <template v-for="(part, i) in segments(t)" :key="i"
                  ><a v-if="part.source" :href="part.source.href" :title="part.source.title">{{
                    part.text
                  }}</a
                  ><strong v-else-if="part.strong">{{ part.text }}</strong
                  ><template v-else>{{ part.text }}</template></template
                >
              </p>
              <div v-if="t.focus_actions?.length" class="scope">
                <div v-for="a in t.focus_actions" :key="a.id">
                  <p>
                    <a :href="'#/events/' + a.event_id + '/research'"
                      >{{ a.target }} · 持续收集范围</a
                    >
                    · 首次核查 #{{ a.round_id }} {{ label(a.state) }}
                  </p>
                  <p v-if="a.checked_at">
                    最近核查 {{ stamp(a.checked_at, true) }} · #{{ a.last_round }} · {{ a.detail }}
                  </p>
                  <ul v-if="a.documents?.length" class="discussion-citations">
                    <li v-for="d in a.documents" :key="d.id">
                      <a :href="'#/events/' + a.event_id + '/doc/' + d.id"
                        ><Link2 />{{ d.title }}</a
                      >
                    </li>
                  </ul>
                </div>
              </div>
              <ul
                v-if="t.workflow_actions?.length"
                class="discussion-citations"
                aria-label="工作流执行记录"
              >
                <li v-for="a in t.workflow_actions" :key="a.position">
                  <a :href="'#/events/' + a.event_id + '/research'">{{ a.event_title }}</a>
                  <a
                    v-if="a.round_id"
                    :href="'#/events/' + a.event_id + '/research/0/' + a.round_id"
                    >轮次 #{{ a.round_id }} · {{ label(a.round_state || '') }}</a
                  >
                  <small v-else>{{
                    a.after_state.run_at
                      ? stamp(a.after_state.run_at, true) + ' · ' + label(a.job_status || 'queued')
                      : a.job_status
                        ? label(a.job_status)
                        : '已保存'
                  }}</small>
                  <a v-if="a.job_id" :href="'#/operations/' + a.job_id">任务 #{{ a.job_id }}</a>
                </li>
              </ul>
              <ul v-if="t.citations.length" class="discussion-citations">
                <li v-for="s in t.citations" :key="s.key">
                  <a :href="s.href"><Link2 />{{ s.key }} · {{ s.title }}</a
                  ><small>{{ sourceLabel(s) }}</small>
                </li>
              </ul>
            </template>
            <p v-else role="status" class="meta">
              {{ t.status === 'running' ? '正在阅读本轮资料并回答…' : label(t.status) }}
              <template v-if="t.info"> · {{ t.info }}</template>
              <a v-if="t.status === 'deferred'" href="#/settings">打开调度设置</a>
              <button
                v-if="t.status === 'failed' && t.id === turns[turns.length - 1].id"
                class="btn quiet"
                :disabled="saving"
                @click="retry(t)"
              >
                <RefreshCw />重试
              </button>
            </p>
          </div>
        </article>
      </div>
      <form v-show="mode === 'chat'" class="discussion-compose" @submit.prevent="send">
        <div class="discussion-compose-heading">
          <label for="discussion-question">向{{ eventId ? '事件' : '工作台' }}主 Agent 提问</label>
          <label class="discussion-provider"
            >回答后端
            <select v-model="provider">
              <option value="auto">
                {{ state.session_provider ? '沿用会话' : '新会话默认' }} ·
                {{ backend(state.session_provider || state.default_provider) }}
              </option>
              <option value="codex">Codex</option>
              <option value="dsh">dsh / Kimi</option>
            </select></label
          >
        </div>
        <div class="discussion-input-row">
          <textarea
            id="discussion-question"
            ref="field"
            v-model="question"
            maxlength="2400"
            rows="2"
            enterkeyhint="send"
            aria-describedby="discussion-hint"
            :placeholder="
              eventId
                ? '询问证据、追问判断，或给当前事件安排跟踪任务…'
                : '询问各事件进展，或说「把某个事件改为每四小时跟踪一次」…'
            "
            @keydown.enter="onEnter"
          />
          <button class="btn primary" type="submit" :disabled="!question.trim() || busy || saving">
            <Send />{{ busy ? '等待上一条回答' : '发送' }}
          </button>
        </div>
        <small id="discussion-hint" class="meta">
          <CornerDownLeft />
          <span>Enter 发送，Shift + Enter 换行。{{
            switchingBackend
              ? '更换后端只能带入已保存的问答和材料，建议沿用当前会话。'
              : '讨论中的概率推演单独保存在对话里。'
          }}</span>
          <button v-if="busy && canSteer && !researchReadOnly" type="button" class="btn quiet" @click="mode = 'investigation'">现在补充调查要求</button>
        </small>
      </form>
    </template>
  </div>
</template>

<style scoped>
.discussion-pane {
  display: flex;
  flex: 1 0 auto;
  flex-direction: column;
  width: 100%;
  max-width: 940px;
  margin: 0 auto;
}
.discussion-log {
  flex: 1 0 auto;
  height: 10rem;
  overflow-y: auto;
  overflow-anchor: none;
  overscroll-behavior-y: contain;
  scrollbar-gutter: stable;
  padding-right: 6px;
}
.discussion-archive {
  padding: 5px 14px;
  margin-bottom: 8px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.discussion-archive summary {
  color: var(--teal);
  cursor: pointer;
}
.discussion-archive pre {
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 0.85rem;
}
.discussion-head,
.discussion-start,
.discussion-compose {
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: clamp(16px, 3vw, 26px);
}
.discussion-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px 16px;
  padding-block: 10px;
  margin-bottom: 8px;
}
.discussion-head .backlink {
  margin-bottom: 0;
}
.discussion-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
}
.discussion-modes { display: flex; gap: 4px; margin-block: 0 8px; flex-wrap: wrap; }
.discussion-modes button { padding: 5px 11px; font: inherit; font-size: .9rem; border: 1px solid var(--line); border-radius: 7px; background: var(--paper); color: var(--sub); }
.discussion-modes button[aria-pressed='true'] { background: var(--mint); border-color: var(--teal); color: var(--teal-deep); font-weight: 650; }
.discussion-modes span { font-weight: 650; }
.discussion-head h2,
.discussion-start h3,
.discussion-question p {
  margin: 0;
}
.discussion-scope {
  display: flex;
  flex: none;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.discussion-scope a,
.discussion-citations a {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.discussion-scope a {
  padding: 7px 10px;
  color: var(--sub);
  text-decoration: none;
  font-size: 0.86rem;
}
.discussion-scope .selected {
  background: var(--mint);
  color: var(--teal-deep);
  font-weight: 650;
}
.discussion-pane svg {
  width: 15px;
  height: 15px;
  flex: none;
}
.discussion-start {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 15px;
}
.discussion-start > svg {
  color: var(--teal);
  width: 22px;
  height: 22px;
}
.discussion-prompts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.discussion-prompts button {
  background: transparent;
  border: 1px solid var(--line);
  color: var(--teal);
  border-radius: 7px;
  padding: 5px 10px;
  text-align: left;
}
.discussion-question {
  margin: 12px 0 6px auto;
  padding: 10px 16px;
  background: var(--mint);
  width: fit-content;
  max-width: 88%;
  border-radius: 12px 12px 3px 12px;
}
.discussion-question p,
.discussion-prose {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.discussion-reply {
  padding: 14px 18px;
  margin-bottom: 18px;
  border-left: 3px solid var(--teal);
  border-radius: 0 9px 9px 0;
  background: var(--paper);
}
.discussion-reply header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}
.discussion-reply header a {
  font-size: 0.8rem;
  margin-left: auto;
}
.discussion-prose {
  line-height: 1.8;
}
.discussion-citations {
  border-top: 1px solid var(--line-soft);
  list-style: none;
  padding: 10px 0 0;
  margin: 8px 0 0;
  font-size: 0.85rem;
}
.discussion-citations li {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 3px;
}
.discussion-citations small,
.discussion-compose small {
  color: var(--muted);
}
.discussion-compose {
  display: grid;
  gap: 6px;
  padding: 10px clamp(12px, 2vw, 18px);
  margin-top: 8px;
}
.discussion-input-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 8px;
}
.discussion-compose textarea {
  width: 100%;
  resize: vertical;
  min-height: 4rem;
  border: 1px solid var(--line);
  border-radius: 7px;
  padding: 9px 11px;
  color: var(--ink);
}
.discussion-compose-heading,
.discussion-provider,
.discussion-compose small {
  display: flex;
  align-items: center;
  gap: 8px;
}
.discussion-compose-heading {
  justify-content: space-between;
  flex-wrap: wrap;
}
.discussion-provider {
  font-size: 0.85rem;
  color: var(--sub);
}
@media (max-width: 720px) {
  .discussion-head {
    flex-direction: column;
  }
  .discussion-question {
    max-width: 95%;
  }
}
</style>
