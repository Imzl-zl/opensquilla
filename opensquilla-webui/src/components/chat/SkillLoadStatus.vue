<template>
  <div
    v-if="receipts.length"
    class="skill-loads"
    :class="{ 'skill-loads--standalone': standalone }"
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
      <span class="skill-loads__summary">
        <strong class="skill-loads__name">{{ receipt.name }}</strong>
        <span>{{ t(`chat.skillPalette.${receipt.status === 'loading' ? 'loadingSkill' : receipt.status}`) }} · {{ t(`chat.skillPalette.${receipt.source}`) }}</span>
      </span>
      <span v-if="receipt.error" class="skill-loads__error">{{ receipt.error }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { SkillLoadReceipt } from '@/types/skillLoads'

defineProps<{ receipts: readonly SkillLoadReceipt[]; standalone?: boolean }>()
const { t } = useI18n()
</script>

<style scoped>
.skill-loads {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  min-width: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
}

.skill-loads__item {
  min-width: 0;
  overflow-wrap: anywhere;
}

.skill-loads--standalone {
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  max-width: calc(100% - 48px);
  margin: 0.25rem auto 0.75rem;
  box-sizing: border-box;
}

.skill-loads__summary {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.125rem 0.375rem;
}

.skill-loads__name {
  min-width: 0;
  color: var(--text);
  font-weight: 600;
}

.skill-loads__error {
  display: block;
  margin-top: 0.125rem;
}

.skill-loads__item--failed {
  color: var(--danger);
}
</style>
