<script setup lang="ts">
import {
  ArrowUpRight,
  CheckCheck,
  FileText,
  GitBranch,
  Crosshair,
  Clock3,
  TrendingUp,
  Link2,
} from 'lucide-vue-next'
import { go, stamp, host, type Card } from './data'
defineProps<{ card: Card }>()
defineEmits<{ read: [card: Card] }>()
</script>
<template>
  <article class="event-card" :class="{ unread: card.unread }">
    <div class="card-top">
      <span class="category"
        ><span class="dot"></span>{{ card.demo ? '演示事件' : '公开来源' }}</span
      ><span v-if="card.status === 'paused'" class="tag">已暂停</span
      ><a
        v-if="card.unread"
        class="card-count"
        :href="'#/events/' + card.id + '/range/' + card.seen + '/' + card.upper"
        >查看 {{ card.unread }} 条未读</a
      >
    </div>
    <h3 class="card-title">
      <a :href="'#/events/' + card.id">{{ card.title }}</a>
    </h3>
    <p class="scope">{{ card.scope }}</p>
    <div v-if="card.fact" class="change-row">
      <span class="row-label"><FileText />新增事实</span
      ><span class="row-content clamp">{{ card.fact.title }}</span>
    </div>
    <div v-if="card.judgment" class="change-row">
      <span class="row-label"><GitBranch />判断变化</span
      ><span class="row-content"
        ><small v-if="card.prior_judgment" class="meta"
          >上次读到 · {{ card.prior_judgment.body }}<br />现在 · </small
        >{{ card.judgment.body }}</span
      >
    </div>
    <a
      v-if="card.judgment?.refs?.length"
      class="evidence-link"
      :href="
        '#/events/' +
        card.id +
        '/doc/' +
        (card.judgment.parts?.[0]
          ? card.judgment.parts[0].document_id + '/' + card.judgment.parts[0].id
          : card.judgment.refs[0])
      "
      >查看判断依据</a
    >
    <div v-if="card.judgment?.watch" class="change-row">
      <span class="row-label"><Crosshair />下一观察点</span
      ><span class="row-content clamp">{{ card.judgment.watch }}</span>
    </div>
    <p v-if="card.correction" class="notice warn">
      {{ card.correction.title }} · {{ card.correction.body }}
    </p>
    <p v-if="!card.judgment && !card.fact && !card.correction" class="muted rail-item">
      {{
        card.last_node
          ? card.last_node.title
          : card.coverage === '已检查'
            ? '已检查，暂无未读变化。'
            : card.coverage
      }}
    </p>
    <button
      v-if="card.predictions.length"
      class="forecast-chip"
      @click="go('events/' + card.id + '/p/' + card.predictions[0].id)"
    >
      <TrendingUp /><span>{{ card.predictions[0].question }}</span
      ><b class="probability"
        >{{ card.predictions[0].scenario_label }} {{ card.predictions[0].p }}%</b
      ><small>截止 {{ stamp(card.predictions[0].deadline) }}</small
      ><small v-if="card.predictions.length > 1">另 {{ card.predictions.length - 1 }} 题</small>
    </button>
    <div class="card-foot">
      <span :class="{ warning: card.coverage.includes('未能') }"
        ><Link2 />
        {{
          card.coverage === '已检查'
            ? card.demo
              ? '演示资料'
              : host(card.sources[0]?.feed || '')
            : card.coverage
        }}</span
      ><time><Clock3 /> {{ stamp(card.updated, true) }}</time>
      <div class="actions">
        <a
          v-if="
            card.last_round &&
            [
              'waiting',
              'partial',
              'failed',
              'deferred',
              'planning',
              'researching',
              'queued',
            ].includes(card.last_round.state)
          "
          class="btn quiet"
          :href="'#/events/' + card.id + '/research'"
          >研究进度</a
        >
        <button v-if="card.unread" class="btn quiet" @click="$emit('read', card)">
          <CheckCheck />读到这里</button
        ><a class="btn quiet" :href="'#/events/' + card.id">时间线<ArrowUpRight /></a>
      </div>
    </div>
  </article>
</template>
