<template>
  <div class="chat-slash" role="listbox" :aria-label="t('chat.skillPalette.title')">
    <template v-for="(cmd, i) in items" :key="`${cmd.kind || 'command'}:${cmd.cmd}`">
      <div v-if="i === 0 || cmd.kind !== items[i - 1]?.kind" class="chat-slash-group">
        {{ t(`chat.skillPalette.${cmd.kind || 'command'}`) }}
      </div>
      <button
        type="button"
        role="option"
        class="chat-slash-item"
        :class="{ 'chat-slash-item--active': i === activeIndex }"
        :aria-selected="i === activeIndex"
        @mousedown.prevent
        @click="emit('choose', cmd)"
      >
        <span class="chat-slash-copy">
          <span class="chat-slash-cmd">{{ cmd.kind === 'command' || !cmd.kind ? cmd.cmd : cmd.label }}</span>
          <span class="chat-slash-desc" :title="cmd.desc">{{ cmd.desc }}</span>
          <span v-if="cmd.skill?.source" class="chat-slash-desc">{{ t(`cronSkills.skills.layerLabel.${cmd.skill.source}`) }}</span>
        </span>
        <span v-if="status(cmd)" class="chat-slash-status">{{ status(cmd) }}</span>
      </button>
    </template>
    <div v-if="!items.length && !loading" class="chat-slash-empty">{{ t('chat.skillPalette.empty') }}</div>
    <div v-if="loading" class="chat-slash-empty" role="status">{{ t('chat.skillPalette.loading') }}</div>
    <div v-if="error" class="chat-slash-empty" role="status">{{ error }}</div>
    <div class="chat-slash-hint">{{ t('chat.skillPalette.autoHint') }}</div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { ChatSlashCommand } from '@/composables/chat/useChatSlashCommands'

defineProps<{ items: ChatSlashCommand[]; activeIndex: number; loading: boolean; error: string }>()
const emit = defineEmits<{ choose: [command: ChatSlashCommand] }>()
const { t } = useI18n()

function status(command: ChatSlashCommand): string {
  const skill = command.skill
  if (skill?.disabled) return t('chat.skillPalette.disabled')
  if (skill && !skill.ready) return skill.reason || t('chat.skillPalette.needsSetup')
  if (skill?.manualOnly) return t('chat.skillPalette.manualOnly')
  return command.metaStatus === 'needs_setup' ? t('chat.metaRuns.needsSetup') : ''
}
</script>
