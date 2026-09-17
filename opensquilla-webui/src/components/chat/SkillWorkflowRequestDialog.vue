<template>
  <Teleport to="body">
    <div class="modal-overlay" @click="emit('cancel')">
      <form
        ref="dialogRef"
        class="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="skill-workflow-title"
        @click.stop
        @submit.prevent="emit('launch')"
      >
        <h3 id="skill-workflow-title" class="modal__title">{{ t('chat.skillPalette.workflowTitle', { name }) }}</h3>
        <div class="modal__body">
          <label for="skill-workflow-request">{{ t('chat.skillPalette.workflowRequest') }}</label>
          <textarea
            id="skill-workflow-request"
            ref="inputRef"
            v-model="text"
            class="input skill-workflow-request"
            rows="5"
            maxlength="100000"
          />
        </div>
        <div class="modal__footer">
          <button class="btn btn--primary" type="submit" :disabled="!text.trim()">{{ t('chat.skillPalette.continue') }}</button>
          <button class="btn btn--ghost" type="button" @click="emit('cancel')">{{ t('common.cancel') }}</button>
        </div>
      </form>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDialogA11y } from '@/composables/useDialogA11y'

defineProps<{ name: string }>()
const text = defineModel<string>({ required: true })
const emit = defineEmits<{ cancel: []; launch: [] }>()
const { t } = useI18n()
const dialogRef = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLTextAreaElement | null>(null)
useDialogA11y(dialogRef, ref(true), () => emit('cancel'), { initialFocus: inputRef })
</script>

<style scoped>
.skill-workflow-request {
  width: 100%;
  margin-top: 0.5rem;
  resize: vertical;
}
</style>
