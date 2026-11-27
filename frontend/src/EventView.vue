<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import KnowledgeView from './KnowledgeView.vue'
import EvidenceParts from './EvidenceParts.vue'
import DiscussionView from './DiscussionView.vue'
import PeopleView from './PeopleView.vue'
import ScenarioForecast from './ScenarioForecast.vue'
import {
  Activity,
  ArrowLeft,
  ArrowUpRight,
  BookOpen,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  FileText,
  GitBranch,
  History,
  Link2,
  MessagesSquare,
  Pause,
  Pin,
  Play,
  Plus,
  RefreshCw,
  Target,
  UsersRound,
} from 'lucide-vue-next'
import {
  api,
  go,
  host,
  label,
  publicationTime,
  stamp,
  type Details,
  type Prediction,
  type Timeline,
  type FormKind,
} from './data'
const props = defineProps<{
  eventId: number
  section: string
  selected: number
  through: number
  revision: number
}>()
const emit = defineEmits<{
  message: [s: string]
  changed: []
  form: [k: FormKind, d: Details, p?: Prediction]
}>()
const d = ref<Details | null>(null),
  timeline = ref<Timeline | null>(null),
  err = ref(''),
  busy = ref(false)
const kind = ref(''),
  order = ref('known'),
  item = ref(0),
  page = ref(1)
let sequence = 0,
  ts = 0
const active = computed(() =>
  ['p', 'predictions'].includes(props.section)
    ? 'predictions'
    : ['doc', 'sources'].includes(props.section)
      ? 'sources'
      : ['knowledge', 'research', 'discussion', 'people'].includes(props.section)
        ? props.section
        : props.section === 'candidates'
          ? 'candidates'
          : 'timeline'
)
const pred = computed(() => d.value?.predictions.find((v) => v.id === props.selected))
const displayedVersion = computed(
  () => pred.value?.versions.find((v) => v.id === props.through) ?? pred.value?.versions.at(-1)
)
const doc = computed(() => d.value?.documents.find((v) => v.id === props.selected))
const route = (part = '') => 'events/' + props.eventId + (part ? '/' + part : '')
const link = (part = '') => '#/' + route(part)
async function load() {
  const seq = ++sequence
  try {
    const data = await api<Details>('/events/' + props.eventId)
    if (seq === sequence) {
      d.value = data
      err.value = ''
    }
  } catch (e) {
    if (seq === sequence) err.value = (e as Error).message
  }
}
async function loadTimeline() {
  const seq = ++ts
  timeline.value = null
  try {
    const data = await api<Timeline>(
      '/events/' +
        props.eventId +
        '/timeline?' +
        new URLSearchParams({
          kind: kind.value,
          order: order.value,
          item: String(item.value),
          page: String(page.value),
          ...(props.section === 'range'
            ? { after: String(props.selected), through: String(props.through) }
            : {}),
        })
    )
    if (seq === ts) timeline.value = data
  } catch (e) {
    if (seq === ts) err.value = (e as Error).message
  }
}
watch(
  () => [props.eventId, props.revision],
  () => {
    void load()
    void loadTimeline()
  },
  { immediate: true }
)
watch(
  () => [props.section, props.selected, props.through],
  () => {
    page.value = 1
    void loadTimeline()
  }
)
watch([kind, order, item], () => {
  page.value = 1
  void loadTimeline()
})
watch(page, loadTimeline)
watch(
  () => props.section,
  () => {
    if (props.section === 'item') {
      kind.value = 'item'
      item.value = props.selected
    }
  },
  { immediate: true }
)
async function mutate(path: string, body: unknown, note: string) {
  if (busy.value) return
  busy.value = true
  try {
    await api(path, body)
    await Promise.all([load(), loadTimeline()])
    emit('message', note)
    emit('changed')
  } catch (e) {
    err.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
function form(k: FormKind, p?: Prediction) {
  if (d.value) emit('form', k, d.value, p)
}
async function knowledgeChanged() {
  await Promise.all([load(), loadTimeline()])
  emit('changed')
}
function itemFilter(id: number) {
  item.value = id
  kind.value = id ? 'item' : ''
  go(route('timeline'))
}
const tabList = [
  ['timeline', '事件时间线', History],
  ['discussion', '和 Agent 讨论', MessagesSquare],
  ['people', '相关人物', UsersRound],
  ['knowledge', '事件知识索引', BookOpen],
  ['research', '研究与目标', Activity],
  ['sources', '证据与来源', BookOpen],
  ['predictions', '相关预测', Target],
  ['candidates', '探索线索', GitBranch],
] as const
</script>
<template>
  <div v-if="err" class="error" role="alert">
    {{ err }} <button class="btn quiet" @click="load">重试</button>
  </div>
  <div v-if="!d && !err" class="loading">正在读取事件…</div>
  <template v-if="d"
    ><a class="backlink" href="#/home"><ArrowLeft />关注动态</a>
    <header class="page-head">
      <div>
        <p class="eyebrow">
          {{ d.card.demo ? '演示事件' : '追踪事件' }} ·
          {{ label(d.card.status) }}
        </p>
        <h1>{{ d.card.title }}</h1>
        <p class="subtitle">{{ d.card.scope }}</p>
      </div>
      <div class="actions">
        <button
          v-if="!d.card.demo"
          class="btn"
          :disabled="busy"
          @click="
            mutate(
              '/events/' + eventId + '/status',
              { status: d.card.status === 'active' ? 'paused' : 'active' },
              '已更新追踪状态'
            )
          "
        >
          <component :is="d.card.status === 'active' ? Pause : Play" />{{
            d.card.status === 'active' ? '暂停追踪' : '恢复追踪'
          }}
        </button>
        <button
          v-if="!d.card.demo && d.card.status === 'active'"
          class="btn"
          :disabled="busy"
          @click="mutate('/events/' + eventId + '/refresh', {}, '采集任务已排队，可在运行记录查看')"
        >
          <RefreshCw />立即采集
        </button>
        <button
          v-if="d.card.unread"
          class="btn primary"
          :disabled="busy || (section === 'range' && !timeline)"
          @click="
            mutate(
              '/events/' + eventId + '/read',
              { token: section === 'range' ? timeline?.token : d.card.token },
              '此页已展示的变化已标为已读'
            )
          "
        >
          <CheckCheck />读到这里
        </button>
      </div>
    </header>
    <div class="tabs view-tabs">
      <a
        v-for="[id, name, icon] in tabList"
        :key="id"
        class="tab"
        :class="{ active: active === id }"
        :href="link(id)"
        ><component :is="icon" />{{ name }}</a
      >
    </div>

    <template v-if="active === 'timeline'"
      ><div class="split">
        <div>
          <p v-if="section === 'range'" class="notice">
            本次卡片展示的 {{ timeline?.total ?? '…' }} 条变化。
            <a :href="link('timeline')">查看完整时间线</a>
          </p>
          <div class="toolbar">
            <select v-model="kind" aria-label="节点类别">
              <option value="">全部类别</option>
              <option
                v-for="k in [
                  'fact',
                  'judgment',
                  'item',
                  'prediction',
                  'resolution',
                  'correction',
                  'historical',
                  'retrospective',
                ]"
                :key="k"
                :value="k"
              >
                {{ label(k) }}
              </option></select
            ><select v-model="order" aria-label="时间顺序">
              <option value="known">按获知时间</option>
              <option value="occurred">按发生时间</option></select
            ><button v-if="item" class="btn quiet" @click="itemFilter(0)">清除子项筛选</button>
          </div>
          <div v-if="!timeline?.nodes.length" class="empty">
            <History /><strong>此范围内暂无节点</strong>
            <a
              v-if="!d.documents.length && d.card.last_round?.state === 'waiting'"
              :href="link('research/0/' + d.card.last_round.id)"
              >查看本轮找源记录</a
            >
          </div>
          <div v-else class="timeline">
            <article v-for="n in timeline.nodes" :key="n.id" class="timeline-node">
              <div class="node-body">
                <div class="section-head">
                  <span class="category">{{ label(n.kind) }}</span><time
                    >获知 {{ stamp(n.created_at, true) }}</time
                  >
                </div>
                <h3>{{ n.title }}</h3>
                <p v-if="n.body">{{ n.body }}</p>
                <p v-if="n.watch" class="watch">观察 · {{ n.watch }}</p>
                <small v-if="n.occurred_at" class="meta"
                  >发生
                  {{
                    n.occurred_at.length === 10 ? n.occurred_at : stamp(n.occurred_at, true)
                  }}</small
                >
                <small v-else-if="order === 'occurred'" class="meta">发生时刻待核</small>
                <div class="evidence">
                  <a
                    v-for="part in n.parts"
                    :key="'f' + part.id"
                    :href="link('doc/' + part.document_id + '/' + part.id)"
                    >{{ part.locator }}</a
                  >
                  <a v-for="refId in n.refs" :key="refId" :href="link('doc/' + refId)"
                    ><Link2 />证据 #{{ refId }}</a
                  ><a v-if="n.prediction_id" :href="link('p/' + n.prediction_id)"
                    >预测与复盘<ArrowUpRight
                  /></a>
                </div>
              </div>
            </article>
          </div>
          <div v-if="timeline && timeline.total > 30" class="toolbar">
            <button class="btn" :disabled="page <= 1" @click="page--"><ChevronLeft />上一页</button
            ><span>{{ page }} / {{ Math.ceil(timeline.total / 30) }}</span
            ><button class="btn" :disabled="page * 30 >= timeline.total" @click="page++">
              下一页<ChevronRight />
            </button>
          </div>
        </div>
        <aside class="rail">
          <section class="panel">
            <header class="panel-head">
              <h2><Activity />长期研究子项</h2>
            </header>
            <div v-for="i in d.items" :key="i.id" class="rail-item inline">
              <button class="text-action" @click="itemFilter(i.id)">
                {{ i.name }}</button><button
                class="icon-btn"
                :aria-label="i.pinned ? '取消固定' : '固定子项'"
                :title="i.pinned ? '取消固定' : '固定子项'"
                :class="{ selected: i.pinned }"
                @click="mutate('/items/' + i.id + '/pin', { enabled: !i.pinned }, '已更新固定状态')"
              >
                <Pin />
              </button>
            </div>
            <p v-if="!d.items.length" class="rail-item">首轮研究后显示子项。</p>
          </section>
          <section class="panel">
            <header class="panel-head">
              <h2><GitBranch />关联事件</h2>
            </header>
            <a
              v-for="r in d.related"
              :key="r.id"
              class="rail-item block-link"
              :href="'#/events/' + r.id"
              >{{ r.title }}<small>共享 {{ r.shared }} 份原始材料</small></a
            >
            <p v-if="!d.related.length" class="rail-item">暂未发现共享证据的其他事件。</p>
          </section>
        </aside>
      </div></template
    >

    <PeopleView
      v-else-if="active === 'people'"
      :key="[eventId, selected].join('-')"
      :event-id="eventId"
      :selected="selected"
      :can-refresh="!d.card.demo && d.card.status === 'active'"
      @message="(m) => emit('message', m)"
    />
    <DiscussionView
      v-else-if="active === 'discussion'"
      :key="[eventId, selected, through].join('-')"
      :event-id="eventId"
      :revision-id="selected"
      :latest="!selected || through === 1"
      :demo="!!d.card.demo"
    />
    <KnowledgeView
      v-else-if="active === 'research' || active === 'knowledge'"
      :key="eventId"
      :event-id="eventId"
      :mode="active"
      :selected="selected"
      :round-id="active === 'research' ? through : 0"
      :demo="!!d.card.demo"
      @changed="knowledgeChanged"
    />
    <template v-else-if="active === 'sources'"
      ><div v-if="section === 'doc' && doc" class="panel document">
        <a class="backlink" :href="link('sources')"><ArrowLeft />所有证据</a
        ><span class="eyebrow">证据 #{{ doc.id }}</span>
        <h2>{{ doc.title }}</h2>
        <p class="meta">
          <template v-if="doc.intake">{{ label(doc.intake) }} · </template
          >{{ publicationTime(doc) }}
          <template v-if="doc.source_updated_at">
            · 来源更新时间 {{ stamp(doc.source_updated_at, true) }}</template
          >
          · 首次采到 {{ stamp(doc.observed_at, true) }}
        </p>
        <p class="long-copy">{{ doc.summary || '原始订阅仅提供标题。' }}</p>
        <a v-if="!d.card.demo" :href="doc.url" target="_blank" rel="noopener noreferrer" class="btn"
          ><ExternalLink />打开原文</a
        >
        <p v-else class="notice">此材料来自本地虚构案例。</p>
        <EvidenceParts :event-id="eventId" :document-id="doc.id" :fragment-id="through" />
        <div v-if="doc.supersedes" class="rail-item">
          修订自
          <a :href="link('doc/' + doc.supersedes)">证据 #{{ doc.supersedes }}</a>
          <p v-for="n in doc.affected" :key="n.id">受影响的{{ label(n.kind) }} · {{ n.title }}</p>
        </div>
        <p class="rail-item">
          收录于 {{ doc.mentions.length }} 个来源条目 ·
          {{ doc.linked_events.map((r) => r.title).join('、') }}
        </p>
      </div>
      <div v-else class="split">
        <section class="stack">
          <h2>
            原始材料 <small>{{ d.documents.length }}</small>
          </h2>
          <a v-for="r in d.documents" :key="r.id" class="panel doc-link" :href="link('doc/' + r.id)"
            ><span class="eyebrow"
              ><FileText /> #{{ r.id }} · {{ d.card.demo ? '演示资料' : host(r.url) }}</span
            >
            <h3>{{ r.title }}</h3>
            <p class="scope clamp">{{ r.summary }}</p>
            <small class="meta"
              ><template v-if="r.intake">{{ label(r.intake) }} · </template
              >{{ publicationTime(r) }} · {{ r.mentions.length }} 个来源条目</small
            ></a
          >
          <div v-if="!d.documents.length" class="empty">
            <strong>尚未收录原始材料</strong>
            <template v-if="d.card.last_round">
              <p>{{ d.card.last_round.detail || label(d.card.last_round.state) }}</p>
              <a :href="link('research/0/' + d.card.last_round.id)">查看本轮找源与核查记录</a>
            </template>
          </div>
        </section>
        <aside class="panel">
          <header class="panel-head">
            <h2><Link2 />来源覆盖</h2>
          </header>
          <div v-for="s in d.card.sources" :key="s.feed" class="rail-item">
            <strong>{{ d.card.demo ? '演示资料库' : host(s.feed) }}</strong><small>{{
              s.error || (!s.last_attempt ? '尚待首次采集' : '最近检查成功')
            }}</small
            ><small>{{ stamp(s.last_attempt, true) }}</small>
          </div>
        </aside>
      </div></template
    >

    <template v-else-if="active === 'predictions'"
      ><div v-if="section === 'p' && pred" class="stack">
        <a class="backlink" :href="link('predictions')"><ArrowLeft />事件预测</a>
        <section class="panel">
          <div class="card-top">
            <span class="category">{{ label(pred.status) }}</span><span>{{
              label(pred.cohort)
            }}</span>
          </div>
          <h2 class="card-title">{{ pred.question }}</h2>
          <template v-if="pred.scale && displayedVersion?.outlook">
            <p class="eyebrow">
              分级趋势预测 · {{ stamp(displayedVersion.created_at, true) }} 登记
            </p>
            <h3>
              {{ displayedVersion.scenario_label }}　<span class="probability"
                >{{ displayedVersion.p }}%</span
              >
            </h3>
            <p><b>比较基线</b> {{ pred.scale.baseline }}</p>
            <p>
              <b>观察窗口</b> {{ stamp(pred.scale.window_start, true) }} 至
              {{ stamp(pred.deadline, true) }}
            </p>
            <ScenarioForecast
              :scale="pred.scale"
              :outlook="displayedVersion.outlook"
              :event-id="eventId"
            />
          </template>
          <div v-else class="prob-large">{{ displayedVersion?.p ?? pred.p }}<small>%</small></div>
          <p><b>截止</b> {{ stamp(pred.deadline, true) }}</p>
          <p><b>判定规则</b> {{ pred.rule }}</p>
          <p><b>认可来源</b> {{ pred.authority }}</p>
          <p><b>取证宽限期</b> 截止后 {{ pred.grace_hours }} 小时</p>
          <div class="actions spaced">
            <a
              v-if="displayedVersion"
              class="btn"
              :href="link('discussion/' + displayedVersion.id)"
            >
              <MessagesSquare />询问预测依据
            </a>
            <button v-if="pred.status === 'open'" class="btn" @click="form('version', pred)">
              <Plus />{{ pred.scale ? '修订分级研判' : '修订概率' }}</button
            ><button class="btn" @click="form('settle', pred)">人工裁定</button
            ><button
              class="btn quiet"
              @click="
                mutate(
                  '/predictions/' + pred.id + '/pin',
                  { enabled: d.card.pinned_prediction !== pred.id },
                  '已更新首页主预测'
                )
              "
            >
              <Pin />{{ d.card.pinned_prediction === pred.id ? '取消首页置顶' : '首页置顶' }}
            </button>
          </div>
        </section>
        <section class="panel">
          <header class="panel-head">
            <h2><History />{{ pred.scale ? '分级研判历史' : '概率历史' }}</h2>
          </header>
          <div
            v-for="v in [...pred.versions].reverse()"
            :key="v.id"
            class="rail-item"
            :class="{ 'discussion-target': through === v.id }"
          >
            <b class="probability">{{ v.scenario_label }} {{ v.p }}%</b> {{ v.reason }}
            <span v-if="v.late" class="tag warn">晚于结算截点</span
            ><small>{{ stamp(v.created_at, true) }} · {{ v.model }}</small>
            <div class="evidence">
              <a v-if="v.outlook" :href="link('p/' + pred.id + '/' + v.id)">查看本版完整研判</a>
              <a :href="link('discussion/' + v.id)"
                ><MessagesSquare />讨论这版 {{ v.scenario_label }} {{ v.p }}%</a
              >
              <a v-if="v.snapshot_id" :href="link('knowledge/' + v.snapshot_id)"
                >当时的知识版本 #{{ v.snapshot_id }}</a
              >
              <a v-for="id in v.refs" :key="id" :href="link('doc/' + id)">证据 #{{ id }}</a>
            </div>
            <p v-for="d in v.dependencies" :key="d.question" class="meta">
              {{ d.question }} · {{ d.condition }}
            </p>
            <a v-for="l in v.lessons" :key="l.id" :href="'#/events/' + l.event_id + '/research'"
              >采用的复盘经验 · {{ l.question }}</a
            >
          </div>
        </section>
        <section class="panel">
          <header class="panel-head">
            <h2><CheckCheck />裁定与复盘</h2>
          </header>
          <p v-if="!pred.settlements.length" class="rail-item">等待决定性证据。</p>
          <div v-for="(r, idx) in [...pred.settlements].reverse()" :key="r.id" class="rail-item">
            <span v-if="idx > 0 || pred.status === 'review'" class="tag warn">{{
              idx > 0 ? '历史裁定' : '等待复核'
            }}</span>
            <strong>{{ r.scores.observed_label ?? label(r.outcome) }} · {{ r.reason }}</strong
            ><small
              >裁定 {{ stamp(r.created_at, true) }} · 证据首次公开 {{ stamp(r.public_at, true) }} ·
              {{ r.actor }}</small
            >
            <p v-if="r.scores.observed_at">实际观察时点 {{ stamp(r.scores.observed_at, true) }}</p>
            <p v-if="r.scores.final?.rps != null">
              分级评分 RPS · 初始 {{ r.scores.initial?.rps }} / 提前 7 天
              {{ r.scores.seven_days?.rps ?? '—' }} / 最终 {{ r.scores.final.rps }}
            </p>
            <p v-else-if="r.scores.final">
              Brier 初始 {{ r.scores.initial?.brier }} / 提前 7 天
              {{ r.scores.seven_days?.brier ?? '—' }} / 最终
              {{ r.scores.final.brier }}
            </p>
            <p v-if="r.scores.late_only" class="warning">全部版本均迟交，此题未计分。</p>
            <p v-if="r.lesson">复盘 · {{ r.lesson }}</p>
            <div class="evidence">
              <a v-for="id in r.refs" :key="id" :href="link('doc/' + id)">证据 #{{ id }}</a>
            </div>
          </div>
        </section>
      </div>
      <div v-else class="stack">
        <div class="section-head">
          <h2>已登记预测</h2>
          <div class="actions">
            <button
              v-if="!d.card.demo && d.card.status === 'active'"
              class="btn"
              @click="form('fixed')"
            >
              登记固定题</button
            ><button class="btn primary" @click="form('forecast')"><Plus />登记预测</button>
          </div>
        </div>
        <a
          v-for="pr in d.predictions"
          :key="pr.id"
          class="panel doc-link"
          :href="link('p/' + pr.id)"
          ><span class="tag">{{ label(pr.status) }}</span>
          <h3>{{ pr.question }}</h3>
          <span class="probability">{{ pr.scenario_label }} {{ pr.p }}%</span>
          <small>截止 {{ stamp(pr.deadline, true) }} · {{ pr.versions.length }} 个版本</small></a
        >
        <div v-if="!d.predictions.length" class="empty"><Target />这条事件尚无登记的预测。</div>
      </div></template
    >

    <div v-else class="stack">
      <div v-for="cand in d.candidates" :key="cand.id" class="panel">
        <span class="tag">{{ cand.state === 'review' ? '候选' : label(cand.state) }}</span>
        <h3>{{ cand.title }}</h3>
        <p>{{ cand.reason }}</p>
        <a href="#/explore" class="backlink">前往探索中心<ArrowUpRight /></a>
      </div>
      <div v-if="!d.candidates.length" class="empty"><GitBranch />暂未产生相关线索。</div>
    </div></template
  >
</template>
