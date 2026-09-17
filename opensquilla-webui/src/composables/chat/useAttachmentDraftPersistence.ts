import { getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import type { Attachment } from '@/types/chat'
import { attachmentDraftKey, createAttachmentDraftStore, type AttachmentDraftScope, type AttachmentDraftStore } from '@/utils/chat/attachmentDrafts'

/** Draft-only storage. It never removes staged uploads, accepted queue material or WAL records. */
export function useAttachmentDraftPersistence(options: {
  attachments: Ref<Attachment[]>
  scope: () => AttachmentDraftScope | null
  store?: AttachmentDraftStore | null
  beforeScopeChange: () => void
  restore: (attachments: Attachment[]) => Promise<void>
  onError: (message: string) => void
}) {
  const store = options.store === undefined ? createAttachmentDraftStore() : options.store
  const restoring = ref(false)
  let scope: AttachmentDraftScope | null = null
  let scopeKey: string | null = null
  let epoch = 0
  let revision = 0
  let suppressed = false
  let retired = false
  let failureReported = false
  let writes = Promise.resolve()
  let nextWrite = 0
  const latestWrite = new Map<string, number>()
  function report(error: unknown) {
    if (failureReported) return
    failureReported = true
    options.onError(error instanceof Error ? error.message : 'Attachment draft recovery is unavailable')
  }
  function save(
    target = scope,
    attachments = options.attachments.value,
    retireSource?: AttachmentDraftScope,
  ): void {
    if (!target) return
    if (!store) { if (attachments.length) report(new Error('Attachment draft recovery is unavailable in this browser')); return }
    const snapshot = attachments.map(attachment => ({ ...attachment,
      ...(attachment.workspaceFile ? { workspaceFile: { ...attachment.workspaceFile } } : {}),
    }))
    let key: string
    try { key = attachmentDraftKey(target) } catch (error) { report(error); return }
    const version = ++nextWrite
    latestWrite.set(key, version)
    writes = writes.then(async () => {
      if (latestWrite.get(key) !== version) return
      await store.save(target, snapshot)
      if (latestWrite.get(key) === version) latestWrite.delete(key)
      if (retireSource) {
        const previousKey = attachmentDraftKey(retireSource)
        // Destination storage must acknowledge ownership before the source is
        // removed. A quota failure retains the old durable draft for recovery.
        // A return to the source or a newer source write also keeps it alive.
        if (scopeKey !== previousKey && !latestWrite.has(previousKey)) {
          await store.save(retireSource, [])
        }
      }
    }).catch(report)
  }
  const stopAttachments = watch(options.attachments, () => {
    revision += 1
    if (!suppressed && !retired) save()
  }, { deep: true, flush: 'sync' })
  const stopScope = watch(() => {
    const next = options.scope()
    try { return next ? attachmentDraftKey(next) : null } catch (error) { report(error); return null }
  }, async key => {
    const next = options.scope()
    const previousScope = scope
    const wasScoped = scope !== null
    const preserveHandoff = scope?.identity === next?.identity && !retired
      && options.attachments.value.length > 0
    if (scope && !retired) save()
    scope = key && next ? { ...next } : null
    scopeKey = key
    const currentEpoch = ++epoch
    failureReported = false
    suppressed = true
    if ((wasScoped || retired) && !preserveHandoff) options.beforeScopeChange()
    retired = false
    suppressed = false
    if (!scope || !store) { restoring.value = false; return }
    // A newly authenticated identity must not overwrite files the operator just
    // selected while its proof was arriving.
    if (options.attachments.value.length) {
      save(scope, options.attachments.value, preserveHandoff && previousScope ? previousScope : undefined)
      restoring.value = false
      return
    }
    const currentRevision = revision
    restoring.value = true
    try {
      await writes
      const loaded = await store.load(scope)
      if (epoch !== currentEpoch || revision !== currentRevision || scopeKey !== key) return
      suppressed = true
      await options.restore(loaded)
      if (epoch === currentEpoch) { suppressed = false; save() }
    } catch (error) {
      if (epoch === currentEpoch) report(error)
    } finally {
      if (epoch === currentEpoch) { suppressed = false; restoring.value = false }
    }
  }, { immediate: true, flush: 'sync' })
  function retire(): void {
    save()
    retired = true
    epoch += 1
    restoring.value = false
  }
  function resume(): void {
    if (!retired) return
    retired = false
    suppressed = false
  }
  function dispose(): void {
    if (!retired) save()
    epoch += 1
    stopAttachments()
    stopScope()
  }
  if (getCurrentScope()) onScopeDispose(dispose)
  return { restoring, retire, resume, dispose, flush: () => writes }
}
