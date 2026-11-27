<script setup lang="ts">
import { computed } from 'vue'
import { confidenceLabel, type Scale, type Outlook } from './data'
const scale = defineModel<Scale>('scale', { required: true })
const outlook = defineModel<Outlook>('outlook', { required: true })
defineProps<{ locked: boolean; documents: { id: number; title: string }[] }>()
const total = computed(() => outlook.value.probabilities.reduce((a, b) => a + (Number(b) || 0), 0))
function addLevel() {
  scale.value.levels.push({ label: '', criterion: '' })
  outlook.value.probabilities.push(0)
}
function removeLevel() {
  scale.value.levels.pop()
  outlook.value.probabilities.pop()
  if (outlook.value.primary_level >= scale.value.levels.length) outlook.value.primary_level = 0
}
</script>
<template>
  <fieldset class="scenario-editor">
    <legend>分级情景与力度</legend>
    <label v-if="!locked" class="field"
      ><span>比较基线</span><textarea v-model.trim="scale.baseline" required />
    </label>
    <div v-for="(level, i) in scale.levels" :key="i" class="scale-row">
      <template v-if="!locked">
        <label class="field"
          ><span>第 {{ i + 1 }} 档名称</span><input v-model.trim="level.label" required
        /></label>
        <label class="field"
          ><span>可观察的判定条件</span><input v-model.trim="level.criterion" required
        /></label>
      </template>
      <strong v-else>{{ level.label }}</strong>
      <label class="field"
        ><span>{{ level.label || '本档' }}概率（%）</span
        ><input v-model.number="outlook.probabilities[i]" type="number" min="0" max="100" required
      /></label>
    </div>
    <div v-if="!locked" class="actions">
      <button type="button" class="btn" :disabled="scale.levels.length >= 6" @click="addLevel">
        增加一档
      </button>
      <button
        type="button"
        class="btn quiet"
        :disabled="scale.levels.length <= 3"
        @click="removeLevel"
      >
        移除末档
      </button>
    </div>
    <p class="meta" aria-live="polite">合计 {{ total }}% / 100%</p>
    <label class="field"
      ><span>主情景</span
      ><select v-model.number="outlook.primary_level">
        <option v-for="(l, i) in scale.levels" :key="i" :value="i">
          {{ l.label || '第 ' + (i + 1) + ' 档' }}
        </option>
      </select></label
    >
    <label class="field"
      ><span>预期变化</span><textarea v-model.trim="outlook.trajectory" required />
    </label>
    <label class="field"
      ><span>判断信心</span
      ><select v-model="outlook.confidence">
        <option v-for="(label, k) in confidenceLabel" :key="k" :value="k">{{ label }}</option>
      </select></label
    >
    <label class="field"
      ><span>信心依据与资料局限</span><textarea v-model.trim="outlook.confidence_reason" required />
    </label>
    <div v-for="(a, i) in outlook.arguments" :key="i" class="scale-row">
      <label class="field"
        ><span>证据方向</span
        ><select v-model="a.side">
          <option value="support">支持</option>
          <option value="counter">反向</option>
        </select></label
      >
      <label class="field"
        ><span>这份证据如何影响判断</span><textarea v-model.trim="a.explanation" required />
      </label>
      <label class="field"
        ><span>所据材料（可多选）</span
        ><select multiple required v-model="a.refs">
          <option v-for="d in documents" :key="d.id" :value="d.id">
            #{{ d.id }} {{ d.title }}
          </option>
        </select></label
      >
      <button
        v-if="outlook.arguments.length > 1"
        type="button"
        class="btn quiet"
        @click="outlook.arguments.splice(i, 1)"
      >
        移除此条依据
      </button>
    </div>
    <button
      v-if="outlook.arguments.length < 8"
      class="btn"
      type="button"
      @click="outlook.arguments.push({ side: 'counter', explanation: '', refs: [] })"
    >
      增加一条依据
    </button>
    <div v-for="(d, i) in outlook.dimensions" :key="i" class="scale-row">
      <label class="field"><span>研判维度</span><input required v-model.trim="d.name" /></label>
      <label class="field"
        ><span>该维度的预期变化</span><textarea required v-model.trim="d.expectation" />
      </label>
      <label class="field"
        ><span>该维度所据材料</span
        ><select multiple required v-model="d.refs">
          <option v-for="doc in documents" :key="doc.id" :value="doc.id">
            #{{ doc.id }} {{ doc.title }}
          </option>
        </select></label
      >
      <button type="button" class="btn quiet" @click="outlook.dimensions.splice(i, 1)">
        移除此维度
      </button>
    </div>
    <button
      v-if="outlook.dimensions.length < 5"
      class="btn"
      type="button"
      @click="outlook.dimensions.push({ name: '', expectation: '', refs: [] })"
    >
      增加研判维度
    </button>
    <label class="field"
      ><span>出现什么信号会改判</span><textarea v-model.trim="outlook.reconsider_if" required />
    </label>
  </fieldset>
</template>
<style scoped>
.scenario-editor {
  display: grid;
  gap: 12px;
  min-width: 0;
  border: 1px solid #bdc9c0;
  border-radius: 8px;
  padding: 14px;
}
.scenario-editor legend,
.scale-row strong {
  font-weight: 600;
}
.scale-row {
  display: grid;
  gap: 8px;
  border-top: 1px solid #d9dfd9;
  padding-top: 10px;
}
.scenario-editor select,
.scenario-editor input {
  min-width: 0;
  width: 100%;
}
</style>
