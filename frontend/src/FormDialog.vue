<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref } from 'vue'
import { X } from 'lucide-vue-next'
import { api, type Details, type FormKind, type Prediction, type Scale, type Outlook } from './data'
import ScenarioEditor from './ScenarioEditor.vue'
const props = defineProps<{
  kind: FormKind
  detail?: Details | null
  prediction?: Prediction | null
}>()
const emit = defineEmits<{ close: []; saved: [id?: number] }>()
const titles = {
  event: '新建追踪事件',
  topic: '添加兴趣主题',
  fixed: '登记固定题',
  forecast: '登记预测',
  version: '修订预测',
  settle: '裁定与复盘',
}
const title = ref(''),
  scope = ref(''),
  sources = ref(''),
  keywords = ref(''),
  enabled = ref(false),
  cadence = ref(6)
const grace = ref(72)
const question = ref(''),
  deadline = ref(''),
  rule = ref(''),
  authority = ref(''),
  reason = ref(''),
  lesson = ref(''),
  p = ref(props.prediction?.p ?? 50),
  refs = ref<number[]>([]),
  outcome = ref(props.prediction?.scale ? 'level' : 'yes'),
  publicAt = ref('')
const form = ref<'auto' | 'binary' | 'ordered'>(
  props.prediction?.scale ? 'ordered' : props.kind === 'fixed' ? 'auto' : 'binary'
)
const windowStart = ref('')
const lastVersion = props.prediction?.versions.at(-1)
const scale = ref<Scale>(
  props.prediction?.scale
    ? JSON.parse(JSON.stringify(props.prediction.scale))
    : {
        window_start: '',
        baseline: '',
        levels: [
          { label: '力度低', criterion: '' },
          { label: '力度中', criterion: '' },
          { label: '力度高', criterion: '' },
        ],
      }
)
const outlook = ref<Outlook>(
  lastVersion?.outlook
    ? JSON.parse(JSON.stringify(lastVersion.outlook))
    : {
        probabilities: [25, 50, 25],
        primary_level: 1,
        confidence: 'medium',
        confidence_reason: '',
        trajectory: '',
        arguments: [{ side: 'support', explanation: '', refs: [] }],
        dimensions: [],
        reconsider_if: '',
      }
)
const observedLevel = ref<number | null>(null)
const observedAt = ref('')
const editingOutlook = computed(
  () => (props.kind === 'forecast' || props.kind === 'version') && form.value === 'ordered'
)
function localMonth(n: number) {
  const d = new Date(),
    day = d.getDate()
  d.setDate(1)
  d.setMonth(d.getMonth() + n)
  const lastDay = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate()
  d.setDate(Math.min(day, lastDay))
  d.setHours(0, 0, 0, 0)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
function threeToSix() {
  form.value = 'ordered'
  windowStart.value = localMonth(3)
  deadline.value = localMonth(6)
}
const pending = ref(false),
  error = ref(''),
  dialog = ref<HTMLElement>()
const isDefinition = computed(() => ['fixed', 'forecast'].includes(props.kind))
const needsRefs = computed(() => ['forecast', 'version', 'settle'].includes(props.kind))
let previous: HTMLElement | null = null
const lines = (s: string) =>
  s
    .split(/\r?\n/)
    .map((v) => v.trim())
    .filter(Boolean)
function trap(e: KeyboardEvent) {
  if (e.key === 'Escape' && !pending.value) {
    emit('close')
    return
  }
  if (e.key !== 'Tab') return
  const nodes = dialog.value?.querySelectorAll<HTMLElement>(
    'button:not(:disabled),input:not(:disabled),textarea,select,a[href]'
  )
  if (!nodes?.length) return
  if (e.shiftKey && document.activeElement === nodes[0]) {
    e.preventDefault()
    nodes[nodes.length - 1].focus()
  } else if (!e.shiftKey && document.activeElement === nodes[nodes.length - 1]) {
    e.preventDefault()
    nodes[0].focus()
  }
}
onMounted(async () => {
  previous = document.activeElement as HTMLElement
  document.addEventListener('keydown', trap)
  await nextTick()
  dialog.value?.querySelector<HTMLElement>('input,select,textarea')?.focus()
})
onBeforeUnmount(() => {
  document.removeEventListener('keydown', trap)
  previous?.focus()
})
async function save() {
  if (pending.value) return
  pending.value = true
  error.value = ''
  try {
    const id = props.detail?.card.id,
      pi = props.prediction?.id
    const definition = () => ({
      question: question.value,
      deadline: new Date(deadline.value).toISOString(),
      rule: rule.value,
      authority: authority.value,
      form: form.value,
      ...(form.value === 'ordered' && windowStart.value
        ? { window_start: new Date(windowStart.value).toISOString() }
        : {}),
      ...(props.kind === 'forecast' && form.value === 'ordered'
        ? { scale: { ...scale.value, window_start: new Date(windowStart.value).toISOString() } }
        : {}),
    })
    const allRefs = editingOutlook.value
      ? [
          ...new Set([
            ...refs.value,
            ...[...outlook.value.arguments, ...outlook.value.dimensions].flatMap((x) => x.refs),
          ]),
        ]
      : refs.value
    const revision = () => ({
      p: editingOutlook.value ? null : p.value,
      ...(editingOutlook.value ? { outlook: outlook.value } : {}),
      reason: reason.value,
      refs: allRefs,
    })
    if (
      needsRefs.value &&
      !allRefs.length &&
      !(props.kind === 'settle' && outcome.value === 'unverifiable')
    )
      throw new Error('请至少选择一份支持此次操作的证据。')
    let result: { id?: number } = {}
    switch (props.kind) {
      case 'event':
        result = await api('/events', {
          title: title.value,
          scope: scope.value,
          feeds: lines(sources.value),
          cadence: cadence.value,
        })
        break
      case 'topic':
        result = await api('/topics', {
          name: title.value,
          feeds: lines(sources.value),
          keywords: keywords.value
            .split(/[,，、]/)
            .map((x) => x.trim())
            .filter(Boolean),
          discovery: enabled.value,
        })
        break
      case 'fixed':
        result = await api('/events/' + id + '/fixed', definition())
        break
      case 'forecast':
        result = await api('/events/' + id + '/predictions', {
          ...definition(),
          ...revision(),
          grace_hours: grace.value,
        })
        break
      case 'version':
        result = await api('/predictions/' + pi + '/versions', revision())
        break
      case 'settle':
        await api('/predictions/' + pi + '/settle', {
          outcome: outcome.value,
          ...(outcome.value === 'level'
            ? {
                observed_level: observedLevel.value,
                observed_at: new Date(observedAt.value).toISOString(),
              }
            : {}),
          public_at: publicAt.value ? new Date(publicAt.value).toISOString() : '',
          reason: reason.value,
          refs: refs.value,
          lesson: lesson.value,
        })
        break
    }
    emit('saved', result.id)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    pending.value = false
  }
}
</script>
<template>
  <div class="modal-backdrop" @mousedown.self="!pending && $emit('close')">
    <section
      ref="dialog"
      class="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="dialog-title"
    >
      <header>
        <h2 id="dialog-title">{{ titles[kind] }}</h2>
        <button class="icon-btn" aria-label="关闭" @click="$emit('close')">
          <X />
        </button>
      </header>
      <form @submit.prevent="save">
        <template v-if="kind === 'event' || kind === 'topic'"
          ><label class="field"
            ><span>{{ kind === 'event' ? '事件名称' : '兴趣主题' }}</span
            ><input
              v-model.trim="title"
              required
              maxlength="250"
              :placeholder="kind === 'event' ? '例如 Vue 3.6 正式版发布' : '例如 前端开源项目'"
          /></label>
          <label v-if="kind === 'event'" class="field"
            ><span>研究范围</span
            ><textarea
              v-model.trim="scope"
              required
              maxlength="1500"
              placeholder="你想持续弄清哪个问题？"
            ></textarea>
          </label>
          <label class="field"
            ><span>RSS / Atom 来源（选填）</span
            ><textarea
              v-model.trim="sources"
              placeholder="https://github.com/vuejs/core/releases.atom"
            ></textarea
            ><small>留空由系统寻找发布方。已有订阅可每行填一个，最多 6 个。</small></label
          >
          <label v-if="kind === 'event'" class="field"
            ><span>采集周期（小时）</span
            ><input v-model.number="cadence" type="number" min="1" max="168" required
          /></label>
          <template v-else
            ><label class="field"
              ><span>允许自动纳入的关键词</span
              ><input v-model="keywords" placeholder="Vue，Vite" /><small
                >逗号分隔。留空时，所有候选都交给你决定。</small
              ></label
            ><label class="check"
              ><input v-model="enabled" type="checkbox" />开启持续发现</label
            ></template
          >
        </template>
        <template v-if="isDefinition">
          <label class="field"
            ><span>预测题型</span>
            <select v-model="form">
              <option v-if="kind === 'fixed'" value="auto">由 Agent 按题意选择</option>
              <option value="binary">是否发生</option>
              <option value="ordered">分级趋势与力度</option>
            </select>
          </label>
          <button type="button" class="btn quiet" @click="threeToSix">
            观察未来第 3 至第 6 个月
          </button>
          <label v-if="form === 'ordered'" class="field"
            ><span>观察开始（本地时区）</span
            ><input v-model="windowStart" type="datetime-local" required
          /></label>
          <label class="field"
            ><span>预测问题</span
            ><textarea
              v-model.trim="question"
              required
              placeholder="希望 Agent 判断哪个结果、影响范围或变化力度？"
            ></textarea></label
          ><label class="field"
            ><span>截止时间（本地时区）</span
            ><input v-model="deadline" type="datetime-local" required /></label
          ><label class="field"
            ><span>判定规则</span><textarea v-model.trim="rule" required></textarea></label
          ><label class="field"
            ><span>认可的来源</span><input v-model.trim="authority" required /></label
        ></template>
        <label v-if="kind === 'forecast'" class="field"
          ><span>取证宽限期（小时）</span
          ><input v-model.number="grace" type="number" min="0" max="720" required
        /></label>
        <ScenarioEditor
          v-if="editingOutlook"
          v-model:scale="scale"
          v-model:outlook="outlook"
          :locked="kind === 'version'"
          :documents="detail?.documents ?? []"
        />
        <label v-else-if="kind === 'forecast' || kind === 'version'" class="field"
          ><span>事件发生的概率（%）</span
          ><input v-model.number="p" type="number" min="0" max="100" required
        /></label>
        <template v-if="kind === 'settle'"
          ><label class="field"
            ><span>裁定结果</span
            ><select v-model="outcome">
              <option v-if="prediction?.scale" value="level">实际力度</option>
              <option v-if="!prediction?.scale" value="yes">应验</option>
              <option v-if="!prediction?.scale" value="no">未应验</option>
              <option value="conflict">证据冲突，进入复核</option>
              <option value="unverifiable">无法核验</option>
            </select></label
          >
          <label v-if="outcome === 'level' && prediction?.scale" class="field"
            ><span>观察期内实际达到的档位</span
            ><select required v-model.number="observedLevel">
              <option disabled :value="null">选择档位</option>
              <option v-for="(s, i) in prediction.scale.levels" :key="s.label" :value="i">
                {{ s.label }} · {{ s.criterion }}
              </option>
            </select></label
          >
          <label v-if="outcome === 'level'" class="field"
            ><span>档位所对应的实际观察时间</span
            ><input required v-model="observedAt" type="datetime-local"
          /></label>
          <label class="field"
            ><span>决定性证据首次公开时间（本地时区）</span
            ><input
              v-model="publicAt"
              type="datetime-local"
              :required="['yes', 'no', 'level'].includes(outcome)" /></label
        ></template>
        <template v-if="needsRefs"
          ><label class="field"
            ><span>依据</span><textarea v-model.trim="reason" required></textarea>
          </label>
          <fieldset class="ref-list">
            <legend>引用证据</legend>
            <label v-for="doc in detail?.documents" :key="doc.id" class="check"
              ><input v-model="refs" type="checkbox" :value="doc.id" />{{ doc.title }}</label
            >
            <p v-if="!detail?.documents.length" class="muted">先采集来源，才能引用证据。</p>
          </fieldset></template
        >
        <label v-if="kind === 'settle'" class="field"
          ><span>复盘经验</span><textarea v-model="lesson"></textarea>
        </label>
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <div class="actions">
          <button type="button" class="btn" :disabled="pending" @click="$emit('close')">取消</button
          ><button class="btn primary" :disabled="pending">
            {{ pending ? '保存中…' : kind === 'event' ? '创建并采集' : '保存' }}
          </button>
        </div>
      </form>
    </section>
  </div>
</template>
