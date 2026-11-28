<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, label } from './data'

type Method = {
  title: string
  when_to_use: string
  steps: string[]
  roles: string[]
  scope: 'event' | 'workspace'
}
type Skill = {
  id: number
  lesson_id: number
  source_withdrawn?: boolean | number
  status: 'generating' | 'pending' | 'unsuitable' | 'failed' | 'approved' | 'rejected'
  method: Method | null
  reason: string
  revision: number
}
const props = defineProps<{ eventId: number; lessons: { id: number; question: string; state: string }[] }>()
const skills = ref<Skill[]>([])
const saving = ref(false), error = ref(''), notice = ref('')
const draft = ref<{ id: number; revision: number; method: Method; steps: string }>()
const focused = ref(0)
const sourceNames = computed(() => new Map(props.lessons.map(l => [l.id, l.question])))
const withdrawnLessons = computed(() => new Set(props.lessons.filter(l => l.state === 'withdrawn').map(l => l.id)))
function withdrawn(s: Skill) {
  return s.source_withdrawn || withdrawnLessons.value.has(s.lesson_id)
}
const names: Record<Skill['status'], string> = {
  generating: '正在提炼', pending: '待你确认', unsuitable: '未发现可复用方法',
  failed: '提炼失败', approved: '已启用', rejected: '已拒绝',
}
const availableRoles = [
  'news', 'source_scout', 'people', 'organizations', 'opinion', 'related', 'specialist',
  'counter', 'dependencies', 'plan', 'study', 'forecast',
]
const base = `/events/${props.eventId}`
let live = true, seq = 0, timer: ReturnType<typeof setTimeout>
function upsert(card: Skill) {
  skills.value = [card, ...skills.value.filter(x => x.id !== card.id)]
}
async function load() {
  const n = ++seq
  try {
    const data = await api<Skill[]>(base + '/skills')
    if (live && seq === n) { skills.value = data; error.value = '' }
  } catch (e) {
    if (live && seq === n) error.value = (e as Error).message
  } finally {
    if (live && seq === n) schedule()
  }
}
function schedule() {
  clearTimeout(timer)
  timer = setTimeout(() => {
    if (document.visibilityState === 'visible' && !saving.value) void load()
    else schedule()
  }, skills.value.some(s => s.status === 'generating') ? 3500 : 15000)
}
function show(card: Skill) {
  focused.value = card.id
  requestAnimationFrame(() => {
    const el = document.getElementById('skill-' + card.id) as HTMLDetailsElement | null
    if (el) { el.open = true; el.scrollIntoView({ block: 'nearest' }) }
  })
}
async function propose(lessonId: number) {
  if (saving.value) return
  const existing = skills.value.find(s => s.lesson_id === lessonId)
  if (existing && (withdrawn(existing) || existing.status !== 'failed')) { show(existing); return }
  saving.value = true
  ++seq
  error.value = notice.value = ''
  try {
    const card = await api<Skill>(base + '/lessons/' + lessonId + '/skill', {})
    if (!live) return
    upsert(card)
    show(card)
  } catch (e) { if (live) error.value = (e as Error).message }
  finally { if (live) { saving.value = false; schedule() } }
}
function startEdit(card: Skill) {
  if (!card.method) return
  draft.value = {
    id: card.id, revision: card.revision,
    method: { ...card.method, roles: [...card.method.roles], steps: [...card.method.steps] },
    steps: card.method.steps.join('\n'),
  }
}
async function decide(card: Skill, decision: 'approve' | 'reject') {
  if (saving.value || card.status !== 'pending' || withdrawn(card)) return
  const edit = draft.value?.id === card.id ? draft.value : null
  const method = edit ? { ...edit.method, steps: edit.steps.split('\n').map(s => s.trim()).filter(Boolean) } : card.method
  if (decision === 'approve' && (!method?.roles.length || method.steps.length < 2 || method.steps.length > 8 ||
      method.steps.some(step => step.length < 2 || step.length > 350))) {
    error.value = '请至少选择一个调查角色，并填写 2 至 8 个步骤，每步 2 至 350 字。'
    return
  }
  saving.value = true
  ++seq
  error.value = notice.value = ''
  try {
    const updated = await api<Skill>(base + '/skills/' + card.id + '/decision', {
      decision, expected_revision: edit?.revision ?? card.revision,
      ...(decision === 'approve' && edit ? { method } : {}),
    })
    if (!live) return
    upsert(updated)
    draft.value = undefined
    notice.value = decision === 'approve' ? '已启用，可供所选范围内的 Codex 和 DSH 调查使用。' : '已拒绝这个候选方法。'
  } catch (e) {
    if (live) { const msg = (e as Error).message; await load(); error.value = msg }
  } finally { if (live) { saving.value = false; schedule() } }
}
defineExpose({ propose, has: (lessonId: number) => skills.value.some(s => s.lesson_id === lessonId) })
onMounted(load)
onBeforeUnmount(() => { live = false; ++seq; clearTimeout(timer) })
</script>

<template>
  <section v-if="skills.length || error || saving" class="panel skill-panel" aria-label="调查方法">
    <header class="panel-head"><h2>调查方法</h2></header>
    <p v-if="error" class="error" role="alert">{{ error }} <button class="btn quiet" @click="load">刷新</button></p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <details v-for="s in skills" :id="'skill-' + s.id" :key="s.id" class="rail-item"
      :open="focused === s.id || s.status === 'pending'">
      <summary><strong>{{ s.method?.title || '复盘方法 #' + s.id }}</strong> · {{ withdrawn(s) ? '来源已撤回，停用' : names[s.status] }}</summary>
      <p class="meta">来自复盘 #{{ s.lesson_id }}<template v-if="sourceNames.has(s.lesson_id)"> · {{ sourceNames.get(s.lesson_id) }}</template></p>
      <p v-if="s.reason">{{ s.reason }}</p>
      <template v-if="draft?.id === s.id && s.status === 'pending' && !withdrawn(s)">
        <form class="method-form" @submit.prevent="decide(s, 'approve')">
          <label>方法名称<input v-model.trim="draft.method.title" minlength="2" maxlength="100" required /></label>
          <label>什么时候使用<textarea v-model.trim="draft.method.when_to_use" minlength="2" maxlength="350" required /></label>
          <label>操作步骤（每行一步，共 2 至 8 步）<textarea v-model="draft.steps" rows="5" required /></label>
          <label>应用范围<select v-model="draft.method.scope"><option value="event">仅本事件</option><option value="workspace">整个工作台</option></select></label>
          <fieldset class="method-roles"><legend>可以使用的角色</legend>
            <label v-for="role in availableRoles" :key="role">
              <input v-model="draft.method.roles" type="checkbox" :value="role" />{{ label(role) }}
            </label>
          </fieldset>
          <div class="actions">
            <button class="btn primary" :disabled="saving">确认并启用</button>
            <button class="btn" type="button" :disabled="saving" @click="draft = undefined">取消修改</button>
          </div>
        </form>
      </template>
      <template v-else>
        <template v-if="s.method">
          <p><b>适用条件</b> · {{ s.method.when_to_use }}</p>
          <ol><li v-for="(step, i) in s.method.steps" :key="i">{{ step }}</li></ol>
          <p class="meta">{{ s.method.scope === 'workspace' ? '整个工作台' : '仅本事件' }} · {{ s.method.roles.map(label).join('、') || '所有调查角色' }} · Codex / DSH</p>
        </template>
        <div v-if="s.status === 'pending' && s.method && !withdrawn(s)" class="actions">
          <button class="btn primary" :disabled="saving" @click="decide(s, 'approve')">确认并启用</button>
          <button class="btn" :disabled="saving" @click="startEdit(s)">修改方法</button>
          <button class="btn quiet" :disabled="saving" @click="decide(s, 'reject')">拒绝</button>
        </div>
        <button v-if="s.status === 'failed' && !withdrawn(s)" class="btn" :disabled="saving" @click="propose(s.lesson_id)">重新提炼</button>
      </template>
    </details>
  </section>
</template>

<style scoped>
summary { cursor: pointer; overflow-wrap: anywhere; }
ol { padding-left: 1.4rem; }
li { margin-block: .25rem; }
.method-form, .method-form label { display: grid; gap: .35rem; }
.method-form { gap: .7rem; }
.method-form :is(input, textarea, select) { min-width: 0; width: 100%; padding: .5rem; border: 1px solid var(--line); border-radius: 5px; font: inherit; }
.method-roles { min-width: 0; display: flex; flex-wrap: wrap; gap: .4rem .8rem; border: 1px solid var(--line); border-radius: 6px; }
.method-roles label { display: inline-flex; align-items: center; }
.method-roles input { width: auto; }
.skill-panel { overflow-wrap: anywhere; }
</style>
