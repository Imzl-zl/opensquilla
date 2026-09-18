import { computed, ref, type ComputedRef } from 'vue'

interface BehaviorConfig {
  naming?: {
    enabled?: boolean
  }
  commit_message?: {
    instructions?: string | null
  }
}

interface BehaviorPanelContext {
  statusText: ComputedRef<string>
}

export function useSetupBehaviorForm() {
  const autoSessionTitles = ref(true)
  // The operator's own rule for the workspace review panel's ✨ draft. Empty
  // means the built-in guidance alone, which is why an empty value is a
  // meaningful patch rather than "unchanged".
  const commitMessageInstructions = ref('')
  const baseline = ref(autoSessionTitles.value)
  const instructionsBaseline = ref(commitMessageInstructions.value)
  const isDirty = computed(() => (
    autoSessionTitles.value !== baseline.value
    || commitMessageInstructions.value !== instructionsBaseline.value
  ))

  function initFromConfig(config: BehaviorConfig) {
    autoSessionTitles.value = config.naming?.enabled !== false
    commitMessageInstructions.value = config.commit_message?.instructions || ''
    baseline.value = autoSessionTitles.value
    instructionsBaseline.value = commitMessageInstructions.value
  }

  function setAutoSessionTitles(enabled: boolean) {
    autoSessionTitles.value = enabled
  }

  function setCommitMessageInstructions(value: string) {
    commitMessageInstructions.value = value
  }

  function patches(): Record<string, unknown> {
    // One entry per edit: a save that carries an untouched field would rewrite
    // a value the operator did not change.
    const patch: Record<string, unknown> = {}
    if (autoSessionTitles.value !== baseline.value) {
      patch['naming.enabled'] = autoSessionTitles.value
    }
    if (commitMessageInstructions.value !== instructionsBaseline.value) {
      patch['commit_message.instructions'] = commitMessageInstructions.value
    }
    return patch
  }

  function createPanel(context: BehaviorPanelContext) {
    return computed(() => ({
      autoSessionTitles: autoSessionTitles.value,
      autoSessionTitlesDirty: isDirty.value,
      commitMessageInstructions: commitMessageInstructions.value,
      statusText: context.statusText.value,
    }))
  }

  return {
    autoSessionTitles,
    commitMessageInstructions,
    isDirty,
    initFromConfig,
    setAutoSessionTitles,
    setCommitMessageInstructions,
    patches,
    createPanel,
  }
}
