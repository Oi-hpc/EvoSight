<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Activity,
  ArrowUpRight,
  BookOpen,
  CalendarDays,
  Check,
  ChevronRight,
  Compass,
  Focus,
  Globe2,
  Layers3,
  ListFilter,
  Menu,
  MessagesSquare,
  PanelsTopLeft,
  Plus,
  Radio,
  Radar,
  RefreshCw,
  Search,
  Settings2,
  Target,
  Workflow,
} from 'lucide-vue-next'
import ChangeCard from './ChangeCard.vue'
import EventView from './EventView.vue'
import DiscussionView from './DiscussionView.vue'
import WorkspaceView from './WorkspaceView.vue'
import WorldView from './WorldView.vue'
import FormDialog from './FormDialog.vue'
import {
  api,
  go,
  label,
  stamp,
  type Card,
  type Details,
  type FormKind,
  type Home,
  type Prediction,
} from './data'
const nav = [
  { id: 'home', text: '关注动态', icon: PanelsTopLeft },
  { id: 'explore', text: '探索中心', icon: Compass },
  { id: 'predictions', text: '预测台账', icon: Target },
  { id: 'world', text: '世界知识库', icon: Globe2 },
  { id: 'agent', text: '全局 Agent', icon: MessagesSquare },
  { id: 'operations', text: '运行记录', icon: Activity },
  { id: 'settings', text: '设置', icon: Settings2 },
]
const path = ref(location.hash.replace(/^#\/?/, '') || 'home'),
  parts = computed(() => path.value.split('/'))
const view = computed(() =>
  parts.value[0] === 'events'
    ? 'events'
    : nav.some((n) => n.id === parts.value[0])
      ? parts.value[0]
      : 'home'
)
const eventId = computed(() => Number(parts.value[1]) || 0)
const name = computed(() =>
  view.value === 'events' ? '事件详情' : nav.find((n) => n.id === view.value)?.text || '关注动态'
)
const data = ref<Home>(),
  error = ref(''),
  toast = ref(''),
  filter = ref('all'),
  query = ref(''),
  search = ref<HTMLInputElement>(),
  menu = ref(false),
  revision = ref(0),
  form = ref<FormKind | null>(null),
  formDetail = ref<Details | null>(null),
  formPrediction = ref<Prediction | null>(null),
  readBusy = ref(false)
let toastTimer = 0,
  seq = 0
const today = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  weekday: 'long',
}).format(new Date())
function notify(value: string) {
  toast.value = value
  clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => (toast.value = ''), 3600)
}
function refocus() {
  if (!document.hidden && view.value === 'home') void refresh()
}
function route() {
  path.value = location.hash.replace(/^#\/?/, '') || 'home'
  menu.value = false
  form.value = null
  document.title = name.value + ' · EvoSight'
  if (view.value === 'home') void refresh()
  window.scrollTo(0, 0)
}
function shortcut(e: KeyboardEvent) {
  if (
    e.key === '/' &&
    !['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target as HTMLElement)?.tagName) &&
    !form.value
  ) {
    e.preventDefault()
    search.value?.focus()
  } else if (e.key === 'Escape') {
    menu.value = false
    search.value?.blur()
  }
}
async function refresh() {
  const s = ++seq
  try {
    const result = await api<Home>('/home')
    if (s === seq) {
      data.value = result
      error.value = ''
    }
  } catch (e) {
    if (s === seq) error.value = (e as Error).message
  }
}
async function mark(c: Card) {
  if (readBusy.value) return
  readBusy.value = true
  try {
    await api('/events/' + c.id + '/read', { token: c.token })
    await refresh()
    notify('已标记此卡片展示的变化')
  } catch (e) {
    notify((e as Error).message)
  } finally {
    readBusy.value = false
  }
}
function openForm(k: FormKind, d: Details | null = null, p: Prediction | null = null) {
  formDetail.value = d
  formPrediction.value = p
  form.value = k
}
function saved(id?: number) {
  const k = form.value
  form.value = null
  revision.value++
  void refresh()
  notify(k === 'event' ? '事件已创建，首次采集已排队' : '已保存')
  if (k === 'event' && id) go('events/' + id + '/research')
  if (k === 'topic') go('explore')
}
function eventDeleted() {
  data.value = undefined
  notify('追踪事件已删除')
  go('home')
}
function doSearch() {
  if (view.value !== 'home') go('home')
}
function visit(id: string) {
  query.value = ''
  go(id)
  menu.value = false
}
onMounted(() => {
  document.addEventListener('visibilitychange', refocus)
  window.addEventListener('hashchange', route)
  window.addEventListener('keydown', shortcut)
  void refresh()
})
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', refocus)
  window.removeEventListener('hashchange', route)
  window.removeEventListener('keydown', shortcut)
  clearTimeout(toastTimer)
})
const visible = computed(
  () =>
    data.value?.cards.filter(
      (c) =>
        (filter.value !== 'unread' || c.unread > 0) &&
        (filter.value !== 'real' || !c.demo) &&
        (!query.value ||
          (c.title + ' ' + c.scope).toLowerCase().includes(query.value.toLowerCase()))
    ) || []
)
const stats = computed(() =>
  data.value
    ? [
        { title: '关注事件', value: data.value.stats.events, icon: Layers3, note: '当前工作台' },
        {
          title: '未读变化',
          value: data.value.stats.unread,
          icon: BookOpen,
          note: '自上次标记以来',
        },
        {
          title: '待核验预测',
          value: data.value.stats.pending,
          icon: Target,
          note: '含进行中预测',
        },
        { title: '探索候选', value: data.value.stats.candidates, icon: Compass, note: '等待决定' },
      ]
    : []
)
const due = computed(
  () =>
    data.value?.cards
      .flatMap((c) =>
        c.predictions
          .filter((p) => ['open', 'frozen', 'review'].includes(p.status))
          .map((p) => ({ ...p, event: c }))
      )
      .sort((a, b) => a.deadline.localeCompare(b.deadline))
      .slice(0, 3) ?? []
)
</script>
<template>
  <div class="shell">
    <aside class="sidebar" :class="{ 'mobile-open': menu }">
      <a class="brand" href="#/home" @click="menu = false"
        ><span class="brand-mark"><Radar /></span
        ><span class="brand-name">EvoSight<small>事件研究工作台</small></span></a
      >
      <nav class="nav-group">
        <small>工作台</small
        ><button
          v-for="n in nav.slice(0, 5)"
          :key="n.id"
          class="nav-item"
          :class="{ active: view === n.id || (view === 'events' && n.id === 'home') }"
          @click="visit(n.id)"
        >
          <component :is="n.icon" />{{ n.text }}
        </button>
      </nav>
      <nav class="nav-group">
        <small>管理</small
        ><button
          v-for="n in nav.slice(5)"
          :key="n.id"
          class="nav-item"
          :class="{ active: view === n.id }"
          @click="visit(n.id)"
        >
          <component :is="n.icon" />{{ n.text }}
        </button>
      </nav>
      <div class="sidebar-bottom">
        <span class="identity"
          ><span class="avatar">E</span
          ><span><strong>个人工作区</strong><small>本地 SQLite</small></span></span
        >
      </div>
    </aside>
    <button v-if="menu" aria-label="关闭导航" class="menu-shade" @click="menu = false"></button>
    <div class="workspace">
      <header class="topbar">
        <button class="mobile-nav icon-btn" aria-label="打开导航" @click="menu = !menu">
          <Menu />
        </button>
        <div class="breadcrumb">
          <Focus /><span>工作台</span><ChevronRight /><strong>{{ name }}</strong>
        </div>
        <label class="searchbox"
          ><Search /><input
            ref="search"
            v-model="query"
            aria-label="搜索关注事件"
            placeholder="搜索事件…"
            @input="doSearch"
            @keydown.enter="doSearch"
          /><kbd>/</kbd></label
        ><span class="local"><span class="dot"></span>本地工作区</span>
      </header>
      <main class="main" id="main">
        <p v-if="error" class="error" role="alert">
          {{ error }} <button class="btn" @click="refresh">重新连接</button>
        </p>
        <template v-if="view === 'home'"
          ><header class="page-head">
            <div>
              <p class="eyebrow date"><CalendarDays />{{ today }}</p>
              <h1>关注动态</h1>
              <p class="subtitle">自上次阅读以来，关注事件的事实与判断发生了哪些变化？</p>
            </div>
            <div class="actions">
              <button
                class="icon-btn"
                aria-label="刷新关注动态"
                title="刷新关注动态"
                @click="refresh"
              >
                <RefreshCw /></button
              ><button class="btn primary" @click="openForm('event')"><Plus />新建追踪</button>
            </div>
          </header>
          <div v-if="!data && !error" class="loading"><Radio />正在加载工作台…</div>
          <template v-if="data"
            ><section class="stats" aria-label="工作台概况">
              <div v-for="s in stats" :key="s.title" class="stat">
                <span class="stat-icon"><component :is="s.icon" /></span
                ><span class="stat-label">{{ s.title }}</span
                ><strong class="stat-value">{{ s.value }}</strong
                ><small class="stat-note">{{ s.note }}</small>
              </div>
            </section>
            <div class="dashboard-grid">
              <section class="feed-column">
                <div class="section-head">
                  <h2><span class="section-stripe"></span>事件变化</h2>
                  <div class="tabs">
                    <button
                      v-for="[key, title] in [
                        ['all', '全部'],
                        ['unread', '未读'],
                        ['real', '真实事件'],
                      ]"
                      :key="key"
                      class="tab"
                      :class="{ active: filter === key }"
                      @click="filter = key"
                    >
                      {{ title }}
                    </button>
                  </div>
                </div>
                <ChangeCard v-for="event in visible" :key="event.id" :card="event" @read="mark" />
                <div v-if="!visible.length" class="empty">
                  <ListFilter /><strong>{{
                    query
                      ? '没有匹配的事件'
                      : filter === 'unread'
                        ? '这次的变化都读完了'
                        : '这里还没有追踪事件'
                  }}</strong
                  ><button v-if="query" class="btn quiet" @click="query = ''">清除搜索</button
                  ><button v-else-if="filter !== 'unread'" class="btn" @click="openForm('event')">
                    新建追踪
                  </button>
                </div>
              </section>
              <aside class="rail">
                <section class="panel forecast-panel">
                  <header class="panel-head">
                    <h2><Target />预测观察</h2>
                    <a href="#/predictions" class="icon-link" aria-label="打开预测台账"
                      ><ArrowUpRight
                    /></a>
                  </header>
                  <a
                    v-for="p in due"
                    :key="p.id"
                    :href="'#/events/' + p.event.id + '/p/' + p.id"
                    class="rail-item block-link"
                    ><span class="forecast-meta"
                      >{{ p.event.demo ? '演示 · ' : '' }}截止 {{ stamp(p.deadline) }}</span
                    ><strong>{{ p.question }}</strong>
                    <div class="odds">
                      <span class="meter"><span :style="{ width: p.p + '%' }"></span></span
                      ><b>{{ p.scenario_label }} {{ p.p }}%</b>
                    </div></a
                  >
                  <p v-if="!due.length" class="rail-item">暂无进行中预测。</p>
                </section>
                <section class="panel">
                  <header class="panel-head">
                    <h2><Workflow />运行概览</h2>
                    <a href="#/operations" class="icon-link" aria-label="查看运行记录"
                      ><ArrowUpRight
                    /></a>
                  </header>
                  <div class="rail-item">
                    <span class="inline"
                      ><span>今日模型调用</span
                      ><strong
                        >{{ data.usage.daily_used }} / {{ data.limits.daily_calls }}</strong
                      ></span
                    >
                    <div class="meter">
                      <span
                        :style="{
                          width:
                            Math.min(
                              100,
                              (data.usage.daily_used / Math.max(1, data.limits.daily_calls)) * 100
                            ) + '%',
                        }"
                      ></span>
                    </div>
                    <small>核验预留 {{ data.limits.verify_reserve }} 次</small>
                  </div>
                  <div v-for="j in data.recent_jobs.slice(0, 2)" :key="j.id" class="rail-item">
                    <span class="inline"
                      ><strong>{{ label(j.kind) }}</strong
                      ><small>{{ label(j.status) }}</small></span
                    ><small>{{ j.event_title || '主题任务' }} · {{ stamp(j.created_at) }}</small>
                  </div>
                  <small class="rail-item block-link"
                    ><span class="dot"></span>
                    {{ data.limits.global_pause ? '调度已暂停' : '本地调度已开启' }}</small
                  >
                </section>
              </aside>
            </div></template
          ></template
        >
        <EventView
          v-else-if="view === 'events'"
          :key="eventId"
          :event-id="eventId"
          :section="parts[2] || 'timeline'"
          :selected="Number(parts[3]) || 0"
          :through="Number(parts[4]) || 0"
          :revision="revision"
          @message="notify"
          @changed="refresh"
          @deleted="eventDeleted"
          @form="openForm"
        />
        <DiscussionView
          v-else-if="view === 'agent'"
          :event-id="0"
          :revision-id="0"
          :latest="true"
          :demo="false"
        />
        <WorldView
          v-else-if="view === 'world'"
          :entry-id="Number(parts[1]) || 0"
          :version-id="Number(parts[2]) || 0"
          :target="Number(parts[3]) || 0"
          :catalog-path="(parts[4] || '').split('~').join('/')"
          :events="data?.cards || []"
        />
        <WorkspaceView
          v-else
          :view="view"
          :revision="revision"
          :job="view === 'operations' ? Number(parts[1]) || 0 : 0"
          @topic="openForm('topic')"
          @changed="refresh"
          @message="notify"
        />
      </main>
    </div>
  </div>
  <FormDialog
    v-if="form"
    :key="form"
    :kind="form"
    :detail="formDetail"
    :prediction="formPrediction"
    @close="form = null"
    @saved="saved"
  />
  <div v-if="toast" class="toast" role="status"><Check /> {{ toast }}</div>
</template>
