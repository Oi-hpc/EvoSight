<script setup lang="ts">
import { confidenceLabel, type Outlook, type Scale } from './data'
defineProps<{ scale: Scale; outlook: Outlook; eventId: number }>()
</script>
<template>
  <section class="scenario-report" aria-label="分级预测依据">
    <p>{{ outlook.trajectory }}</p>
    <div v-for="(level, i) in scale.levels" :key="level.label" class="scenario-level">
      <div>
        <strong>{{ level.label }}</strong
        ><span v-if="i === outlook.primary_level" class="tag">主情景</span
        ><b class="probability">{{ outlook.probabilities[i] }}%</b>
      </div>
      <meter
        min="0"
        max="100"
        :value="outlook.probabilities[i]"
        :aria-label="level.label + '的概率'"
      />
      <small>{{ level.criterion }}</small>
    </div>
    <p>
      <b>判断信心 · {{ confidenceLabel[outlook.confidence] }}</b
      >　{{ outlook.confidence_reason }}
    </p>
    <div v-for="(a, i) in outlook.arguments" :key="i" class="scenario-argument">
      <strong>{{ a.side === 'support' ? '支持证据' : '反向证据' }}</strong>
      <p>{{ a.explanation }}</p>
      <div class="evidence">
        <a v-for="id in a.refs" :key="id" :href="'#/events/' + eventId + '/doc/' + id"
          >证据 #{{ id }}</a
        >
      </div>
    </div>
    <div v-for="x in outlook.dimensions" :key="x.name" class="scenario-argument">
      <strong>{{ x.name }}</strong>
      <p>{{ x.expectation }}</p>
      <div class="evidence">
        <a v-for="id in x.refs" :key="id" :href="'#/events/' + eventId + '/doc/' + id"
          >证据 #{{ id }}</a
        >
      </div>
    </div>
    <p><b>改判条件</b>　{{ outlook.reconsider_if }}</p>
  </section>
</template>
<style scoped>
.scenario-report {
  display: grid;
  gap: 14px;
  margin: 14px 0;
}
.scenario-report p,
.scenario-argument p {
  margin: 0;
  line-height: 1.65;
}
.scenario-level {
  display: grid;
  gap: 4px;
}
.scenario-level > div {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.scenario-level b {
  margin-left: auto;
}
.scenario-level meter {
  width: 100%;
  height: 10px;
  accent-color: #397862;
}
.scenario-level small {
  line-height: 1.5;
}
.scenario-argument {
  border-left: 2px solid #a4b9ac;
  padding-left: 12px;
}
</style>
