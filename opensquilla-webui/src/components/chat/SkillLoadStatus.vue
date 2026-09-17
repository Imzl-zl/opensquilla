<template>
  <div
    v-if="receipts.length"
    class="skill-loads"
    role="status"
    aria-live="polite"
    data-testid="skill-load-status"
  >
    <div
      v-for="receipt in receipts"
      :key="`${receipt.turnId}:${receipt.name}:${receipt.instanceId}:${receipt.digest}:${receipt.source}`"
      class="skill-loads__item"
      :class="{ 'skill-loads__item--failed': receipt.status === 'failed' }"
    >
      <strong>{{ receipt.name }}</strong>
      <span>{{ t(`chat.skillPalette.${receipt.status === 'loading' ? 'loadingSkill' : receipt.status}`) }} · {{ t(`chat.skillPalette.${receipt.source}`) }}</span>
      <span v-if="receipt.error">{{ receipt.error }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { SkillLoadReceipt } from '@/types/skillLoads'

defineProps<{ receipts: readonly SkillLoadReceipt[] }>()
const { t } = useI18n()
</script>

<style scoped>
.skill-loads {
  margin-block: 0.5rem;
  color: var(--text-muted);
  font-size: 0.75rem;
}

.skill-loads__item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.375rem;
}

.skill-loads__item--failed {
  color: var(--danger);
}
</style>
