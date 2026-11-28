<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  Activity,
  ArrowUpRight,
  BookOpen,
  Check,
  Compass,
  Database,
  History,
  Layers,
  Plus,
  Settings2,
  Target,
} from 'lucide-vue-next'
import {
  api,
  host,
  label,
  stamp,
  type Explore,
  type Ledger,
  type Limits,
  type Operations,
} from './data'
const props = defineProps<{ view: string; revision: number; job: number }>()
const emit = defineEmits<{ message: [s: string]; changed: []; topic: [] }>()
const ex = ref<Explore>(),
  ledger = ref<Ledger>(),
  op = ref<Operations>(),
  limits = ref<Limits>(),
  error = ref(''),
  busy = ref(false),
  tab = ref('all')
const defaults = [
  ['daily_calls', '每日模型调用上限'],
  ['monthly_calls', '每月模型调用上限'],
  ['verify_reserve', '为核验预留的调用数'],
  ['capacity', '活跃真实事件容量'],
  ['auto_limit', '每轮自动纳入上限'],
  ['max_agent_concurrency', 'Agent 最大并发（1–4）'],
  ['watch_minutes', '新闻巡检间隔（分钟）'],
  ['watch_sources', '每事件自动巡检入口数'],
] as const
const activeProfiles = computed(() => {
  if (!op.value) return {}
  const { discuss_codex, discuss_dsh, ...roles } = op.value.profiles
  return { ...roles, discussion: op.value.limits.agent_backend === 'dsh' ? discuss_dsh : discuss_codex }
})
let seq = 0
async function load() {
  const ticket = ++seq
  try {
    if (props.view === 'explore') {
      const data = await api<Explore>('/explore')
      if (ticket === seq) ex.value = data
    } else if (props.view === 'predictions') {
      const data = await api<Ledger>('/predictions')
      if (ticket === seq) ledger.value = data
    } else {
      const data = await api<Operations>(
        '/operations' + (props.view === 'operations' && props.job ? '?job=' + props.job : '')
      )
      if (ticket === seq) {
        op.value = data
        if (props.view === 'settings') limits.value = { ...data.limits }
      }
    }
    if (ticket === seq) error.value = ''
  } catch (e) {
    if (ticket === seq) error.value = (e as Error).message
  }
}
watch(
  () => [props.view, props.revision, props.job],
  () => {
    tab.value = 'all'
    void load()
  },
  { immediate: true }
)
const timer = window.setInterval(() => {
  if (props.view === 'operations' && !document.hidden) void load()
}, 5000)
onBeforeUnmount(() => window.clearInterval(timer))
async function action(path: string, data: unknown, msg: string) {
  if (busy.value) return
  busy.value = true
  try {
    await api(path, data)
    await load()
    emit('message', msg)
    emit('changed')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
const shown = computed(
  () =>
    ledger.value?.predictions.filter(
      (p) =>
        tab.value === 'all' ||
        (tab.value === 'open'
          ? ['open', 'frozen', 'review'].includes(p.status)
          : ['resolved', 'unverifiable'].includes(p.status))
    ) ?? []
)
</script>
<template>
  <div v-if="error" class="error" role="alert">{{ error }}</div>
  <template v-if="view === 'explore'"
    ><header class="page-head">
      <div>
        <p class="eyebrow">研究范围</p>
        <h1>探索中心</h1>
        <p class="subtitle">从兴趣主题和已有事件中发现下一条追踪线索。</p>
      </div>
      <button class="btn primary" @click="$emit('topic')"><Plus />添加兴趣主题</button>
    </header>
    <div v-if="ex" class="split">
      <section class="stack">
        <h2>候选事件</h2>
        <article v-for="c in ex.candidates" :key="c.id" class="panel">
          <span class="tag">{{ c.state === 'review' ? '待决定' : label(c.state) }}</span>
          <h3>{{ c.title }}</h3>
          <p class="scope">{{ c.scope }}</p>
          <p>{{ c.reason }}</p>
          <div class="evidence">
            <a
              v-for="e in c.evidence"
              :key="e.id"
              :href="e.url"
              target="_blank"
              rel="noopener noreferrer"
              >{{ e.title }}<ArrowUpRight
            /></a>
          </div>
          <div v-if="c.state === 'review'" class="actions spaced">
            <button
              class="btn primary"
              :disabled="busy"
              @click="action('/candidates/' + c.id, { action: 'accept' }, '候选已纳入追踪')"
            >
              <Check />纳入追踪</button
            ><button
              class="btn"
              :disabled="busy"
              @click="action('/candidates/' + c.id, { action: 'dismiss' }, '已忽略候选')"
            >
              忽略
            </button>
          </div>
          <a v-if="c.target_id" :href="'#/events/' + c.target_id" class="backlink"
            >查看事件<ArrowUpRight
          /></a>
        </article>
        <div v-if="!ex.candidates.length" class="empty">
          <Compass /><strong>还没有候选事件</strong>
          <p>事件研究或已开启的主题发现会在这里留下线索。</p>
        </div>
      </section>
      <aside class="panel">
        <div class="panel-head">
          <h2><Layers />兴趣主题</h2>
          <small>{{ ex.topics.length }} 个</small>
        </div>
        <div v-for="t in ex.topics" :key="t.id" class="rail-item">
          <strong>{{ t.name }}</strong><small>{{ t.feeds.map(host).join('、') }}</small>
          <p>
            {{ t.keywords.length ? '授权关键词 · ' + t.keywords.join('、') : '候选均待人工决定' }}
          </p>
          <label class="check"
            ><input
              type="checkbox"
              :checked="!!t.discovery"
              :disabled="busy"
              @change="
                action(
                  '/topics/' + t.id + '/discovery',
                  { enabled: !t.discovery },
                  '已更新主题发现'
                )
              "
            />持续发现</label
          >
        </div>
        <p v-if="!ex.topics.length" class="rail-item">添加主题后，可选择是否持续发现。</p>
      </aside>
    </div></template
  >

  <template v-if="view === 'predictions'"
    ><header class="page-head">
      <div>
        <p class="eyebrow">概率与核验</p>
        <h1>预测台账</h1>
        <p class="subtitle">同一问题的概率修订、决定性证据和评分都保留在同一题下。</p>
      </div>
    </header>
    <template v-if="ledger"
      ><div class="tabs view-tabs">
        <button
          v-for="[key, name] in [
            ['all', '全部预测'],
            ['open', '进行中'],
            ['resolved', '已裁定'],
          ]"
          :key="key"
          class="tab"
          :class="{ active: tab === key }"
          @click="tab = key"
        >
          {{ name }}
        </button>
      </div>
      <div class="stack">
        <a
          v-for="p in shown"
          :key="p.id"
          class="panel doc-link"
          :href="'#/events/' + p.event_id + '/p/' + p.id"
          ><div class="card-top">
            <span class="category">{{ label(p.status) }}</span><span>{{ p.event_title }}</span
            ><span v-if="p.demo" class="tag">演示</span>
          </div>
          <h3>{{ p.question }}</h3>
          <span class="probability">{{ p.scenario_label }} {{ p.p }}%</span>
          <small>截止 {{ stamp(p.deadline, true) }} · {{ p.versions.length }} 个概率版本</small></a
        >
        <div v-if="!shown.length" class="empty"><Target />此范围内暂无预测。</div>
        <section class="panel">
          <div class="panel-head">
            <h2><Activity />真实题评测</h2>
            <small>评分越低越好</small>
          </div>
          <div v-for="(m, g) in ledger.metrics" :key="g" class="rail-item">
            <strong>{{ label(g) }}</strong> · 登记 {{ m.submitted }} · 已裁定 {{ m.resolved }} ·
            有效评分 {{ m.scored }} · 无法核验 {{ m.unverifiable }} · 迟交 {{ m.late
            }}<small
              >初始 / 提前 7 天 / 最终 Brier　{{ m.initial_brier ?? '—' }} /
              {{ m.seven_days_brier ?? '—' }} / {{ m.final_brier ?? '—' }}　·　平均提前
              {{ m.mean_lead_hours ?? '—' }} 小时</small
            ><small v-if="m.graded_scored"
              >分级题 {{ m.graded_scored }} 道 · 初始 / 提前 7 天 / 最终 RPS　{{
                m.initial_rps ?? '—'
              }}
              / {{ m.seven_days_rps ?? '—' }} / {{ m.final_rps ?? '—' }}</small
            ><small v-if="g === 'fixed'"
              >到期应答 {{ m.due }} · 覆盖率
              {{ m.coverage === null ? '—' : Math.round(m.coverage * 100) + '%' }}</small
            >
          </div>
        </section>
        <section v-if="ledger.fixed.length || ledger.abstentions.length" class="panel">
          <div class="panel-head">
            <h2><BookOpen />固定题与弃答记录</h2>
          </div>
          <div v-for="f in ledger.fixed" :key="'f' + f.id" class="rail-item">
            <strong>{{ f.question }}</strong><small
              >{{ f.event_title }} · {{ f.prediction_id ? '已提交' : f.reason || '等待回答' }} ·
              截止 {{ stamp(f.deadline, true) }}</small
            >
          </div>
          <div v-for="n in ledger.abstentions" :key="'n' + n.id" class="rail-item">
            {{ n.event_title }} · {{ n.reason }}
          </div>
        </section>
      </div></template
    ></template
  >

  <template v-if="view === 'operations'"
    ><header class="page-head">
      <div>
        <p class="eyebrow">后台调度</p>
        <h1>{{ job ? '任务 #' + job : '运行记录' }}</h1>
        <a v-if="job" class="backlink" href="#/operations">所有运行记录</a>
        <p v-else class="subtitle">查看来源采集、模型调用和等待重试的任务。</p>
      </div>
      <button class="btn" @click="load"><History />刷新</button>
    </header>
    <template v-if="op">
      <p v-if="op.concurrency" class="scope spaced" role="status">
        正在调用 {{ op.concurrency.active }} 个 Agent · 当前并发上限
        {{ op.concurrency.effective }} / 设置 {{ op.concurrency.configured }} ·
        {{ op.concurrency.cpu }} 核<span v-if="op.concurrency.available_mib !== null">
          · 可用内存 {{ op.concurrency.available_mib }} MiB</span
        ><span v-if="op.concurrency.cpu_busy"> · CPU 繁忙</span>
      </p>
      <div class="tabs view-tabs">
        <button class="tab" :class="{ active: tab !== 'models' }" @click="tab = 'all'">
          任务队列</button
        ><button class="tab" :class="{ active: tab === 'models' }" @click="tab = 'models'">
          模型调用
        </button>
      </div>
      <div v-if="tab !== 'models'" class="panel">
        <div v-if="!op.jobs.length" class="empty">暂无后台任务。</div>
        <div v-for="j in op.jobs" :key="j.id" class="rail-item">
          <span class="pill" :class="{ warn: ['failed', 'deferred'].includes(j.status) }">{{
            label(j.status)
          }}</span>
          <strong>{{ label(j.kind) }}</strong> · {{ j.event_title || j.topic_name
          }}<small>{{ stamp(j.created_at, true) }} · #{{ j.id }}</small>
          <p>{{ j.info }}</p>
          <button
            v-if="j.event_status !== 'deleted' && ['failed', 'deferred'].includes(j.status)"
            class="btn"
            :disabled="busy"
            @click="action('/jobs/' + j.id + '/retry', {}, '任务已重新排队')"
          >
            重试任务
          </button>
        </div>
      </div>
      <div v-else class="panel">
        <div v-if="!op.runs.length" class="empty">暂无模型调用。</div>
        <div v-for="r in op.runs" :key="r.id" class="rail-item">
          <span class="category">{{ label(r.role) }}</span>
          <strong>{{ r.model }} · {{ r.effort }}</strong
          ><small
            >{{ label(r.status) }} · {{ r.elapsed?.toFixed(1) ?? '—' }} 秒 · 输入
            {{ r.input_tokens ?? '—' }} / 输出 {{ r.output_tokens ?? '—' }} tokens</small
          >
        </div>
      </div></template
    ></template
  >

  <template v-if="view === 'settings'"
    ><header class="page-head">
      <div>
        <p class="eyebrow">工作台配置</p>
        <h1>设置</h1>
        <p class="subtitle">配置 Agent 后端、调用额度和自动调度。</p>
      </div>
    </header>
    <div v-if="op && limits" class="split">
      <form class="panel" @submit.prevent="action('/settings', limits, '设置已保存')">
        <div class="panel-head">
          <h2><Settings2 />模型与运行额度</h2>
          <small>按 UTC 归日、归月</small>
        </div>
        <p class="scope spaced">额度按模型调用次数计算，包含已发出的失败请求。</p>
        <div class="form-grid">
          <label class="field"><span>其他新会话默认 Agent 后端</span>
            <select v-model="limits.agent_backend">
              <option value="codex">Codex</option>
              <option value="dsh">DSH / Kimi</option>
            </select>
          </label>
          <label class="field"><span>新建新闻侦查和探索补源 Agent 默认后端</span>
            <select v-model="limits.news_agent_backend" aria-describedby="news-search-capability">
              <option value="dsh">DSH / Kimi</option>
              <option value="codex">Codex</option>
            </select>
          </label>
          <label v-for="[key, name] in defaults" :key="key" class="field"
            ><span>{{ name }}</span
            ><input
              v-model.number="limits[key]"
              type="number"
              :min="
                ['max_agent_concurrency', 'capacity', 'watch_minutes', 'watch_sources'].includes(
                  key
                )
                  ? 1
                  : 0
              "
              :max="key === 'max_agent_concurrency' ? 4 : undefined"
              required
          /></label>
        </div>
        <p id="news-search-capability" class="scope spaced">
          DSH 可使用内置网页搜索和 EvoSight 搜索。当前 KPRSI 配置下 Codex 原生网页搜索不可用，可使用 EvoSight 搜索。
          默认后端设置应用于新建会话，已有会话按各自的后端续接。
        </p>
        <p v-if="op.concurrency" class="scope spaced">
          当前可同时运行 {{ op.concurrency.effective }} 个 Agent。调度器按 CPU
          和可用内存调整，并预留 {{ op.concurrency.reserve_mib }} MiB。
        </p>
        <label class="check"
          ><input v-model="limits.global_pause" type="checkbox" />全局暂停调度</label
        >
        <div class="actions spaced">
          <button class="btn primary" :disabled="busy">保存设置</button>
        </div>
      </form>
      <aside class="panel">
        <div class="panel-head">
          <h2><Database />新会话默认模型</h2>
          <span class="tag">{{ op.mode === 'test' ? '测试' : '正式' }}</span>
        </div>
        <div v-for="(v, k) in activeProfiles" :key="k" class="rail-item">
          <strong>{{ label(k) }}</strong><small>{{ v[0] }} · {{ v[1] }}</small>
        </div>
        <p class="rail-item">
          本月调用 {{ op.usage.monthly_used }} 次。输入
          {{ op.usage.input_tokens.toLocaleString() }} tokens，输出
          {{ op.usage.output_tokens.toLocaleString() }} tokens。
        </p>
      </aside>
    </div></template
  >
</template>
