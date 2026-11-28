<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MessageCircleQuestion, Send } from 'lucide-vue-next'
import { api, label, stamp } from './data'

type Line = { line_key: string; role: string; question: string }
type Receipt = {
  snapshot_id: number; previous_snapshot_id: number | null; position: number; question: string
  outcome: 'added' | 'changed' | 'unchanged' | 'missing' | 'rejected'
  state: 'confirmed' | 'unknown' | 'missing' | 'rejected'
  before: string; after: string; reason: string; summary_excerpt: string
}
type Item = {
  id: number; round_id: number | null; goal_version: number; kind: 'requirement' | 'question'
  origin_round_id?: number | null; detail?: string
  text: string; answer: string | null; line_key: string; role: string; status: string
  created_at: string; report: Receipt | null
}
type Inbox = { goal_version: number; current_round_id: number | null; lines: Line[]; items: Item[]; pending_questions: Item[] }
const props = defineProps<{ eventId: number; compact?: boolean; readOnly?: boolean }>()
const emit = defineEmits<{ pending: [count: number] }>()
const base = '/events/' + props.eventId + '/steering'
const data = ref<Inbox>(), draft = ref(''), selectedLine = ref('')
const replying = ref<Item>(), target = ref<{ version: number; roundId: number | null }>()
const error = ref(''), notice = ref(''), saving = ref(false)
const field = ref<HTMLTextAreaElement>(), panel = ref<HTMLElement>(), log = ref<HTMLElement>()
const replyDrafts = new Map<number, string>()
const followed = new Set<number>()
const nextCursor = ref(0), loadingHistory = ref(false)
let visiblePages = 1
let sequence = 0, live = true, timer: ReturnType<typeof setTimeout>
let request: { key: string; nonce: string } | undefined
const statuses: Record<string, string> = {
  awaiting_user: '待你回答', queued: '已排队', assigned: '已分派', received: '调查员已接收',
  reported: '已有报告回执', superseded: '目标已变更', unavailable: '暂时无法处理',
}
const outcomes = { added: '新增核查项', changed: '已修改', unchanged: '与前版相同', missing: '报告漏写', rejected: '答复被退回' }
const reportStates = { confirmed: '已写入答复', unknown: '证据待补', missing: '漏写', rejected: '被退回' }
const activeRound = computed(() => target.value ? target.value.roundId : data.value?.current_round_id)
const changedGoal = computed(() => !!target.value && !!data.value && data.value.goal_version !== target.value.version)
const pendingCount = computed(() => data.value?.pending_questions.filter(x => x.status === 'awaiting_user').length ?? 0)
const items = computed(() => [...(data.value?.items ?? [])].sort((a, b) =>
  Number(b.status === 'awaiting_user') - Number(a.status === 'awaiting_user') || b.id - a.id))
const validReply = computed(() => !replying.value || data.value?.items.some(i =>
  i.id === replying.value?.id && i.status === 'awaiting_user' && i.goal_version === data.value?.goal_version))
const message = computed({
  get: () => draft.value,
  set: value => {
    if (!target.value && data.value) target.value = { version: data.value.goal_version, roundId: data.value.current_round_id }
    draft.value = value
  },
})
watch(pendingCount, n => emit('pending', n), { immediate: true })
function schedule() {
  clearTimeout(timer)
  timer = setTimeout(() => {
    if (document.visibilityState === 'visible' && !saving.value) void load()
    else schedule()
  }, 6000)
}
async function load() {
  const n = ++sequence
  try {
    const pages = [await api<Inbox>(base)]
    while (live && n === sequence && pages.length < visiblePages) {
      const last = pages[pages.length - 1].items
      if (last.length < 50) break
      pages.push(await api<Inbox>(base + '?before=' + last[last.length - 1].id))
    }
    if (!live || n !== sequence) return
    const latest = pages[0], merged = new Map<number, Item>()
    for (const page of [...pages].reverse()) for (const item of page.items) merged.set(item.id, item)
    for (const item of latest.pending_questions) merged.set(item.id, item)
    // Keep the receipt visible when answering an older question outside the loaded history.
    const outside = [...followed].filter(id => !merged.has(id))
    await Promise.all(outside.map(async id => {
      const page = await api<Inbox>(base + '?before=' + (id + 1))
      const item = page.items.find(i => i.id === id)
      if (item) merged.set(id, item)
    }))
    if (!live || n !== sequence) return
    data.value = { ...latest, items: [...merged.values()] }
    const last = pages[pages.length - 1].items
    nextCursor.value = last.length === 50 ? last[last.length - 1].id : 0
    error.value = ''
  } catch (e) { if (live && n === sequence) error.value = (e as Error).message }
  finally { if (live && n === sequence) schedule() }
}
async function loadHistory() {
  if (!nextCursor.value || loadingHistory.value || saving.value) return
  loadingHistory.value = true
  visiblePages++
  try { await load() } finally { loadingHistory.value = false }
}
function recipient(item: Item) {
  const line = data.value?.lines.find(l => l.line_key === item.line_key)
  const roleName = ({ main: '事件主 Agent', coordinator: '事件主 Agent', study: '综合研究 Agent' } as Record<string, string>)[item.role] || (item.role && label(item.role))
  return [roleName, line?.question || item.line_key].filter(Boolean).join(' · ') || '事件主 Agent'
}
function focus() {
  void nextTick(() => field.value?.focus())
}
function useCurrent() {
  if (!data.value) return
  target.value = { version: data.value.goal_version, roundId: data.value.current_round_id }
  selectedLine.value = ''
}
let savedDraft: { text: string; line: string; target: typeof target.value } | null = null
function reply(item: Item) {
  if (replying.value) replyDrafts.set(replying.value.id, draft.value)
  else savedDraft = { text: draft.value, line: selectedLine.value, target: target.value }
  replying.value = item
  draft.value = replyDrafts.get(item.id) || ''
  target.value = { version: item.goal_version, roundId: item.round_id }
  notice.value = error.value = ''
  focus()
}
function leaveReply() {
  if (replying.value) replyDrafts.set(replying.value.id, draft.value)
  replying.value = undefined
  draft.value = savedDraft?.text || ''
  selectedLine.value = savedDraft?.line || ''
  target.value = savedDraft?.target
  savedDraft = null
}
function pointTo(roundId: number, version: number, lineKey = '', context = '') {
  if (replying.value) leaveReply()
  target.value = { version, roundId }
  selectedLine.value = lineKey
  if (context && !draft.value.includes(context)) draft.value = [draft.value, context].filter(Boolean).join('\n') + '\n'
  panel.value?.scrollIntoView({ block: 'nearest' })
  focus()
}
async function submit() {
  const text = draft.value.trim(), version = target.value?.version ?? data.value?.goal_version
  if (!text || !version || props.readOnly || saving.value || changedGoal.value || !validReply.value) return
  const item = replying.value, path = item ? base + '/' + item.id + '/answer' : base
  const body = item ? { text, goal_version: version }
    : { text, goal_version: version, round_id: activeRound.value || null, line_key: selectedLine.value || undefined }
  const key = JSON.stringify([path, body])
  if (request?.key !== key) request = { key, nonce: crypto.randomUUID() }
  saving.value = true
  ++sequence
  error.value = notice.value = ''
  try {
    const result = await api<Item>(path, { ...body, nonce: request.nonce })
    if (!live) return
    followed.add(result.id)
    if (data.value) data.value.items = [result, ...data.value.items.filter(i => i.id !== result.id)]
    notice.value = item ? '回答已提交，可在下方记录查看去向。' : '已提交调查要求。'
    request = undefined
    if (item) { leaveReply(); replyDrafts.delete(item.id) }
    else { draft.value = ''; target.value = undefined; selectedLine.value = '' }
    await load()
    if (log.value) log.value.scrollTop = 0
  } catch (e) {
    if (live) { const msg = (e as Error).message; await load(); error.value = msg }
  } finally { if (live) { saving.value = false; schedule() } }
}
function onEnter(e: KeyboardEvent) {
  if (e.shiftKey || e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  if (!e.repeat) void submit()
}
defineExpose({ pointTo })
onMounted(load)
onBeforeUnmount(() => { live = false; ++sequence; clearTimeout(timer) })
</script>

<template>
  <section ref="panel" class="research-inbox" :class="{ 'panel': !compact, 'inbox-chat': compact }" aria-label="调查沟通">
    <header v-if="!compact" class="panel-head"><h2><MessageCircleQuestion />调查沟通</h2></header>
    <p v-if="error" class="error" role="alert">{{ error }} <button type="button" class="btn quiet" @click="load">刷新</button></p>
    <p v-if="notice" class="inbox-notice" role="status">{{ notice }}</p>
    <div ref="log" class="inbox-log" tabindex="0" aria-label="调查要求、提问及回执">
      <p v-if="!data && !error" class="meta">正在读取调查沟通…</p>
      <p v-else-if="data && !items.length" class="meta">暂无调查要求或 Agent 提问。</p>
      <article v-for="i in items" :key="i.id" class="inbox-message">
        <div class="message-head">
          <b>{{ i.kind === 'question' ? 'Agent 提问' : '你的调查要求' }}</b>
          <span :class="{ attention: i.status === 'awaiting_user' }">{{ statuses[i.status] || label(i.status) }}</span>
        </div>
        <p class="message-body">{{ i.text }}</p>
        <p class="meta">{{ i.kind === 'question' ? '提问者' : '处理者' }} · {{ recipient(i) }}</p>
        <p class="meta">目标 V{{ i.goal_version }} · <a v-if="i.round_id" :href="`#/events/${eventId}/research/0/${i.round_id}`">轮次 #{{ i.round_id }}</a><span v-else>待安排轮次</span> · {{ stamp(i.created_at, true) }}</p>
        <p v-if="i.origin_round_id && i.origin_round_id !== i.round_id" class="meta">从 <a :href="`#/events/${eventId}/research/0/${i.origin_round_id}`">轮次 #{{ i.origin_round_id }}</a> 接续</p>
        <p v-if="i.detail" class="meta">{{ i.detail }}</p>
        <p v-if="i.answer" class="response"><b>你的回答</b><br />{{ i.answer }}</p>
        <button v-if="!readOnly && i.status === 'awaiting_user' && i.goal_version === data?.goal_version" class="btn" type="button" :disabled="saving" @click="reply(i)">回答此问题</button>
        <details v-if="i.report" class="receipt">
          <summary>报告 #{{ i.report.snapshot_id }} · {{ outcomes[i.report.outcome] }}</summary>
          <p><b>{{ i.report.question }}</b> · {{ reportStates[i.report.state] }}</p>
          <p v-if="i.report.before"><strong>前版答复</strong><br />{{ i.report.before }}</p>
          <p v-if="i.report.after"><strong>本版答复</strong><br />{{ i.report.after }}</p>
          <p v-if="i.report.reason">{{ i.report.reason }}</p>
          <blockquote v-if="i.report.summary_excerpt">{{ i.report.summary_excerpt }}</blockquote>
          <a :href="`#/events/${eventId}/knowledge/${i.report.snapshot_id}/${i.report.position}`">定位报告核查项 #{{ i.report.position }}</a>
          <a v-if="i.report.previous_snapshot_id" :href="`#/events/${eventId}/knowledge/${i.report.previous_snapshot_id}`">查看前版</a>
        </details>
      </article>
      <button v-if="nextCursor" class="btn quiet" :disabled="loadingHistory || saving" @click="loadHistory">查看更早的记录</button>
    </div>
    <form v-if="data && !readOnly" class="inbox-form" @submit.prevent="submit">
      <div v-if="replying" class="reply-context">
        <strong>回答{{ recipient(replying) }}</strong> · {{ replying.text }}
        <button class="btn quiet" type="button" :disabled="saving" @click="leaveReply">返回补充要求</button>
      </div>
      <div v-else class="inbox-tools">
        <b>补充调查要求</b>
        <label>交给
          <select v-model="selectedLine" :disabled="saving || changedGoal">
            <option value="">主 Agent 分派</option>
            <option v-if="selectedLine && !data.lines.some(l => l.line_key === selectedLine)" :value="selectedLine">{{ selectedLine }}</option>
            <option v-for="l in data.lines" :key="l.line_key" :value="l.line_key">{{ label(l.role) }} · {{ l.question }}</option>
          </select>
        </label>
      </div>
      <div class="compose-row">
        <textarea ref="field" v-model="message" rows="2" maxlength="1500" enterkeyhint="send" :disabled="saving"
          :aria-label="replying ? '回答 Agent' : '补充调查要求'" :placeholder="replying ? '回答这个调查员的问题…' : '例如：请分别核实复购权益和面向所有买家的限时优惠…'" @keydown.enter="onEnter" />
        <button class="btn primary" type="submit" :disabled="!draft.trim() || saving || changedGoal || !validReply"><Send />{{ replying ? '回答' : '提交' }}</button>
      </div>
      <p v-if="changedGoal || !validReply" class="meta" role="status">原调查目标或问题状态已更新。
        <button v-if="!replying" class="btn quiet" type="button" @click="useCurrent">改为当前目标</button>
      </p>
      <small v-else class="meta">目标 V{{ target?.version || data.goal_version }}<template v-if="activeRound"> · 轮次 #{{ activeRound }}</template> · Enter 发送，Shift + Enter 换行</small>
    </form>
  </section>
</template>

<style scoped>
.research-inbox { min-width: 0; width: 100%; max-width: 100%; box-sizing: border-box; display: flex; flex-direction: column; overflow-wrap: anywhere; }
.inbox-log { max-height: 23rem; overflow-y: auto; overscroll-behavior-y: contain; padding: .3rem .9rem; scrollbar-gutter: stable; }
.inbox-chat { flex: 1 0 auto; min-height: 0; }
.inbox-chat .inbox-log { flex: 1 0 auto; height: 10rem; max-height: none; }
.inbox-message { padding: .7rem 0; border-bottom: 1px solid var(--line-soft); }
.inbox-message:last-child { border-bottom: 0; }
.message-head, .inbox-tools { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: .35rem .7rem; }
.message-head span { color: var(--sub); font-size: .83rem; }
.message-head .attention { color: var(--teal-deep); font-weight: 650; }
.inbox-message p { margin: .28rem 0; white-space: pre-wrap; }
.response, .receipt, .reply-context { padding: .4rem .7rem; border-left: 2px solid var(--teal); }
.receipt summary { color: var(--teal-deep); cursor: pointer; }
.receipt a + a { margin-left: 1rem; }
.receipt blockquote { margin: .4rem 0; }
.inbox-form { width: 100%; min-width: 0; box-sizing: border-box; display: grid; grid-template-columns: minmax(0, 1fr); gap: .45rem; padding: .8rem .9rem; border-top: 1px solid var(--line); background: var(--paper); }
.inbox-chat .inbox-form { border: 1px solid var(--line); border-radius: var(--radius); margin-top: .4rem; }
.inbox-tools { min-width: 0; }
.inbox-tools label { display: grid; min-width: 0; max-width: 100%; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: .35rem; color: var(--sub); }
.inbox-tools select { min-width: 0; width: 100%; max-width: 21rem; text-overflow: ellipsis; }
.compose-row { display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: end; gap: .5rem; }
.compose-row, .compose-row textarea { min-width: 0; }
.compose-row textarea { width: 100%; box-sizing: border-box; min-height: 3.5rem; resize: vertical; padding: .55rem; border: 1px solid var(--line); border-radius: 7px; color: var(--ink); font: inherit; }
.inbox-notice { color: var(--teal-deep); padding: 0 .9rem; margin-block: .25rem; }
.inbox-form > p { margin: 0; }
.inbox-form svg { width: 1rem; height: 1rem; }
</style>
