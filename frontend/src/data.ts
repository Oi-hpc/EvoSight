export interface CollectionFocus {
  id: number
  event_id: number
  kind: string
  target: string
  angle: string
  active: number
  first_round_id: number
  first_state: string
  last_round: number | null
  checked_at: string | null
  detail: string | null
  person_target_id: number | null
  documents: { id: number; title: string }[]
}

export interface Note {
  id: number
  kind: string
  title: string
  body: string
  watch: string
  refs: number[]
  created_at: string
  occurred_at: string | null
  parts?: { id: number; locator: string; document_id: number }[]
  item_id: number | null
  prediction_id: number | null
}
export interface Source {
  feed: string
  error: string | null
  last_success: string | null
  last_attempt: string | null
}
export interface SmallPrediction {
  scenario_label?: string | null
  id: number
  question: string
  deadline: string
  status: string
  p: number
}
export interface Card {
  id: number
  title: string
  scope: string
  status: string
  demo: number
  cadence: number
  unread: number
  pending_questions: number
  upper: number
  token: string
  seen: number
  updated: string
  last_checked: string | null
  coverage: string
  sources: Source[]
  predictions: SmallPrediction[]
  fact: Note | null
  judgment: Note | null
  prior_judgment?: Note | null
  last_round?: { id: number; state: string; detail: string } | null
  correction: Note | null
  last_node: Note | null
  pinned_prediction: number | null
}
export interface Limits {
  agent_backend: 'codex' | 'dsh'
  news_agent_backend: 'codex' | 'dsh'
  max_agent_concurrency: number
  daily_calls: number
  monthly_calls: number
  verify_reserve: number
  capacity: number
  auto_limit: number
  global_pause: boolean
  watch_minutes: number
  watch_sources: number
}
export interface Usage {
  daily_used: number
  monthly_used: number
  input_tokens: number
  output_tokens: number
}
export interface Job {
  id: number
  kind: string
  status: string
  created_at: string
  info: string
  event_title: string | null
  event_status?: string | null
  topic_name?: string
}
export interface Run {
  id: number
  role: string
  model: string
  effort: string
  status: string
  elapsed: number | null
  input_tokens: number | null
  output_tokens: number | null
}
export interface Home {
  cards: Card[]
  limits: Limits
  usage: Usage
  stats: { events: number; unread: number; pending: number; candidates: number }
  recent_jobs: Job[]
  mode: string
}
export interface Doc {
  id: number
  url: string
  title: string
  summary: string
  published_at: string | null
  published_label: string | null
  source_times_checked: number
  source_updated_at: string | null
  intake: string
  observed_at: string
  mentions: { feed: string; url: string }[]
  supersedes: number | null
  affected?: { id: number; kind: string; title: string }[]
  linked_events: { id: number; title: string }[]
}
export interface Candidate {
  id: number
  title: string
  scope: string
  reason: string
  state: string
  target_id: number | null
  evidence?: { id: number; title: string; url: string }[]
}
export interface Scale {
  window_start: string
  baseline: string
  levels: { label: string; criterion: string }[]
}
export interface Outlook {
  probabilities: number[]
  primary_level: number
  confidence: 'low' | 'medium' | 'high'
  confidence_reason: string
  trajectory: string
  arguments: { side: 'support' | 'counter'; explanation: string; refs: number[] }[]
  dimensions: { name: string; expectation: string; refs: number[] }[]
  reconsider_if: string
}
export const confidenceLabel = { low: '低', medium: '中', high: '高' }
export interface Version {
  outlook?: Outlook
  scenario_label?: string
  id: number
  snapshot_id?: number
  dependencies?: { question: string; condition: string }[]
  lessons?: { id: number; event_id: number; question: string }[]
  p: number
  reason: string
  refs: number[]
  created_at: string
  model: string
  late: boolean
}
export interface Score {
  revision: number
  p: number
  brier?: number
  rps?: number
  lead_hours: number
}
export interface Resolution {
  id: number
  outcome: string
  public_at: string | null
  reason: string
  refs: number[]
  scores: {
    observed_level?: number
    observed_label?: string
    observed_at?: string
    initial?: Score | null
    seven_days?: Score | null
    final?: Score | null
    late_only?: boolean
  }
  lesson: string
  created_at: string
  actor: string
}
export interface Prediction extends SmallPrediction {
  scale?: Scale | null
  event_id: number
  rule: string
  authority: string
  cohort: string
  grace_hours: number
  versions: Version[]
  settlements: Resolution[]
  event_title?: string
  demo?: number
}
export interface Details {
  card: Card
  items: { id: number; name: string; pinned: number }[]
  documents: Doc[]
  predictions: Prediction[]
  candidates: Candidate[]
  related: { id: number; title: string; shared: number }[]
}
export interface Timeline {
  nodes: Note[]
  total: number
  page: number
  token: string
}
export interface Topic {
  id: number
  name: string
  feeds: string[]
  keywords: string[]
  discovery: number
}
export interface Explore {
  topics: Topic[]
  candidates: Candidate[]
}
export interface Metric {
  graded_scored: number
  initial_rps: number | null
  seven_days_rps: number | null
  final_rps: number | null
  submitted: number
  resolved: number
  scored: number
  unverifiable: number
  late: number
  due: number
  coverage: number | null
  initial_brier: number | null
  seven_days_brier: number | null
  final_brier: number | null
  mean_lead_hours: number | null
}
export interface Ledger {
  predictions: Prediction[]
  metrics: Record<string, Metric>
  fixed: {
    id: number
    event_title: string
    question: string
    deadline: string
    prediction_id: number | null
    reason: string
  }[]
  abstentions: { id: number; event_title: string; reason: string }[]
}
export interface Operations {
  concurrency: {
    configured: number
    effective: number
    active: number
    cpu: number
    available_mib: number | null
    memory_limit_mib: number | null
    reserve_mib: number
    per_agent_mib: number
    cpu_busy: boolean
  }
  jobs: Job[]
  runs: Run[]
  usage: Usage
  limits: Limits
  mode: string
  profiles: Record<string, string[]>
}
export const labels: Record<string, string> = {
  archived: '已归档',
  active: '追踪中',
  paused: '已暂停',
  planning: '研究规划中',
  researching: '正在研究',
  published: '已发布',
  partial: '部分完成',
  waiting: '待补来源或材料',
  recovered: '原窗口已补采',
  unchanged: '已检查，无变化',
  superseded: '已按新目标重规划',
  archive: '历史材料',
  historical: '历史补证',
  reference: '知识引用',
  support: '补充证据',
  unreference: '移出引用',
  retrospective: '预测复盘',
  people: '人物',
  organizations: '组织与国家',
  news: '新闻与多模态',
  opinion: '社会舆情',
  related: '关联事件',
  specialist: '专项研究',
  counter: '反证研究',
  dependencies: '预测依赖',
  research_retry: '补做研究',
  investigation_wake: '交接补查',
  handoff_publish: '更新交接结论',
  claim_review: '关键结论复核',
  scheduled_collect: '定时采集',
  cancelled: '已取消',
  plan: '研究规划',
  source_scout: '探索补源',
  history: '含历史补查',
  background: '背景',
  reported: '来源声明',
  view: '公开观点',
  hypothesis: '待验证假设',
  relation: '关系',
  answered: '已回答',
  initial: '首次收录',
  window: '窗口内信号',
  backfill: '历史补录',
  undated: '时间待核',
  future: '来源时间超前',
  person_candidate: '人物账号归属待核',
  revision: '来源修订',
  fact: '新增事实',
  judgment: '研究判断',
  item: '研究子项',
  prediction: '概率版本',
  resolution: '裁定复盘',
  correction: '来源更正',
  open: '进行中',
  frozen: '冻结待核验',
  review: '待复核',
  resolved: '已结算',
  unverifiable: '无法核验',
  yes: '应验',
  no: '未应验',
  conflict: '证据冲突',
  collect: '采集与研究',
  forecast: '预测判断',
  discussion: '人机讨论',
  discuss_codex: '讨论 · Codex',
  discuss_dsh: '讨论 · dsh',
  discover: '主题发现',
  verify: '到期核验',
  study: '材料研究',
  done: '已完成',
  failed: '失败',
  running: '运行中',
  queued: '排队中',
  deferred: '额度等待',
  interrupted: '已中断',
  accepted: '已纳入',
  dismissed: '已忽略',
  auto: '自主题',
  fixed: '固定题',
}
export const label = (s: string) => labels[s] || s
export const knowledgeKind = (s: string) => (s === 'fact' ? '事实' : label(s))
export function stamp(s?: string | null, full = false) {
  if (!s) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: full ? 'numeric' : undefined,
    month: '2-digit',
    day: '2-digit',
    hour: full ? '2-digit' : undefined,
    minute: full ? '2-digit' : undefined,
    hour12: false,
  }).format(new Date(s))
}
export function publicationTime(doc: Doc) {
  if (!doc.source_times_checked)
    return doc.published_at ? '历史时间待核 ' + stamp(doc.published_at, true) : '发布时间待核'
  if (doc.published_at) return '发布 ' + stamp(doc.published_at, true)
  return doc.published_label ? '来源标注 ' + doc.published_label : '发布时间未提供'
}

export function host(s: string) {
  try {
    return new URL(s).hostname
  } catch {
    return s
  }
}
export async function api<T>(path: string, body?: unknown, method = 'POST'): Promise<T> {
  const response = await fetch(
    '/api' + path,
    body === undefined
      ? {}
      : {
          method,
          headers: {
            'Content-Type': 'application/json',
            'X-EvoSight': 'local',
          },
          body: JSON.stringify(body),
        }
  )
  const result = await response.json()
  if (!response.ok)
    throw new Error(
      typeof result.detail === 'string'
        ? result.detail
        : Array.isArray(result.detail)
          ? '请检查填写内容与取值范围'
          : '请求失败'
    )
  return result as T
}
export function go(path: string) {
  location.hash = '/' + path
}
export type FormKind = 'event' | 'topic' | 'fixed' | 'forecast' | 'version' | 'settle'
