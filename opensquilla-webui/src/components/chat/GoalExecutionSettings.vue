<template>
  <div class="goal-settings">
    <label>
      <span>{{ t('chat.goal.tokenBudget') }}</span>
      <input
        type="number"
        min="1"
        step="1"
        :value="modelValue.tokenBudget ?? ''"
        :placeholder="t('chat.goal.noTokenBudget')"
        :disabled="disabled || incompleteUsage"
        :aria-invalid="!valid"
        @input="updateBudget"
      >
    </label>
    <p v-if="!valid" class="goal-settings__note" role="alert">{{ t('chat.goal.invalidTokenBudget') }}</p>
    <p v-if="incompleteUsage" class="goal-settings__note">{{ t(usageCoverage === 'partial_usage' ? 'chat.goal.pendingUsageReceipts' : 'chat.goal.partialUsageHistory') }}</p>
    <button
      v-if="incompleteUsage && existingBudget != null && modelValue.tokenBudget !== null"
      type="button"
      :disabled="disabled"
      @click="emit('update:modelValue', { ...modelValue, tokenBudget: null })"
    >{{ t('chat.goal.removeTokenBudget') }}</button>
    <p v-if="usageAccountingStartedAtMs != null" class="goal-settings__note">
      {{ t('chat.goal.usageAccountingSince', { time: new Date(usageAccountingStartedAtMs).toLocaleString() }) }}
    </p>
    <label>
      <span>{{ t('chat.goal.executionPolicy') }}</span>
      <select
        :value="modelValue.executionPolicy ?? 'foreground'"
        :disabled="disabled"
        @change="updatePolicy"
      >
        <option value="foreground">{{ t('chat.goal.foreground') }}</option>
        <option value="background">{{ t('chat.goal.background') }}</option>
      </select>
    </label>
    <p v-if="modelValue.executionPolicy === 'background'" class="goal-settings__note">{{ t('chat.goal.backgroundHint') }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { GoalExecutionOptions } from '@/modules/goalCenter'
import { goalExecutionOptionsValid } from '@/composables/chat/useChatGoals'

const props = defineProps<{
  modelValue: GoalExecutionOptions
  disabled?: boolean
  usageCoverage?: 'complete' | 'partial_history' | 'partial_usage'
  existingBudget?: number | null
  usageAccountingStartedAtMs?: number | null
}>()
const emit = defineEmits<{ 'update:modelValue': [value: GoalExecutionOptions] }>()
const { t } = useI18n()
const incompleteUsage = computed(() => props.usageCoverage !== undefined && props.usageCoverage !== 'complete')
const valid = computed(() => goalExecutionOptionsValid(props.modelValue))

function updateBudget(event: Event) {
  const value = (event.target as HTMLInputElement).value
  emit('update:modelValue', { ...props.modelValue, tokenBudget: value === '' ? null : Number(value) })
}

function updatePolicy(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (value !== 'foreground' && value !== 'background') return
  emit('update:modelValue', { ...props.modelValue, executionPolicy: value })
}
</script>

<style scoped>
.goal-settings {
  display: grid;
  gap: var(--sp-2);
  min-width: 0;
  font-size: var(--fs-xs);
}
.goal-settings label {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.goal-settings input,
.goal-settings button,
.goal-settings select {
  box-sizing: border-box;
  max-width: 100%;
  min-height: 44px;
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text);
  font: inherit;
}
.goal-settings__note {
  margin: 0;
  color: var(--text-muted);
}
</style>
