import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useAttachmentDraftPersistence } from './useAttachmentDraftPersistence'
import type { AttachmentDraftScope, AttachmentDraftStore } from '@/utils/chat/attachmentDrafts'
import type { Attachment } from '@/types/chat'

const item: Attachment = { kind: 'staged', local_id: 1, name: 'draft.txt', mime: 'text/plain', size: 4, file_uuid: 'draft-file' }
const initial = { identity: 'gateway-and-user-A', sessionKey: 'session-A' }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function fixture(overrides: Partial<AttachmentDraftStore> = {}) {
  const data = new Map<string, Attachment[]>()
  const key = (scope: AttachmentDraftScope) => JSON.stringify(scope)
  const store = {
    load: vi.fn(async (scope: AttachmentDraftScope) => data.get(key(scope)) || []),
    save: vi.fn(async (scope: AttachmentDraftScope, attachments: readonly Attachment[]) => { data.set(key(scope), [...attachments]) }),
    ...overrides,
  }
  const scope = ref<AttachmentDraftScope | null>(initial)
  const attachments = ref<Attachment[]>([])
  const onError = vi.fn()
  const persistence = useAttachmentDraftPersistence({ attachments, scope: () => scope.value, store,
    beforeScopeChange: () => { attachments.value = [] },
    restore: async restored => { attachments.value = restored }, onError })
  return { data, store, scope, attachments, persistence, onError }
}

async function ready(persistence: ReturnType<typeof useAttachmentDraftPersistence>) {
  await vi.waitFor(() => expect(persistence.restoring.value).toBe(false))
  await persistence.flush()
}

describe('attachment draft ownership', () => {
  it('restores a Blob-backed draft only for the matching gateway/user/session scope', async () => {
    const f = fixture({ load: vi.fn(async () => [{ ...item, file: new File(['text'], item.name) }]) })
    await ready(f.persistence)
    expect(f.store.load).toHaveBeenCalledWith(initial)
    expect(f.attachments.value[0].file?.size).toBe(4)
    f.persistence.dispose()
  })

  it('keeps the previous draft on navigation and restores the destination independently', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    f.persistence.retire()
    f.attachments.value = []
    f.scope.value = { ...initial, sessionKey: 'session-B' }
    await ready(f.persistence)
    expect(f.data.get(JSON.stringify(initial))).toEqual([item])
    expect(f.attachments.value).toEqual([])
    f.persistence.dispose()
  })

  it('clears only local draft data after composer ownership moves to an accepted queue/send', async () => {
    const f = fixture()
    await ready(f.persistence)
    f.attachments.value = [item]
    await f.persistence.flush()
    f.attachments.value = []
    await f.persistence.flush()
    expect(f.data.get(JSON.stringify(initial))).toEqual([])
    expect(Object.keys(f.store).sort()).toEqual(['load', 'save'])
    f.persistence.dispose()
  })

  it('gateway/account changes never restore late source files into the new composer', async () => {
    const oldLoad = deferred<Attachment[]>()
    const f = fixture({ load: vi.fn().mockImplementationOnce(() => oldLoad.promise).mockResolvedValue([]) })
    await Promise.resolve()
    f.scope.value = { identity: 'gateway-and-user-B', sessionKey: 'session-A' }
    oldLoad.resolve([item])
    await ready(f.persistence)
    expect(f.attachments.value).toEqual([])
    f.persistence.dispose()
  })

  it('a late restore never replaces attachments selected while IndexedDB was loading', async () => {
    const load = deferred<Attachment[]>()
    const f = fixture({ load: () => load.promise })
    f.attachments.value = [{ ...item, name: 'new.txt' }]
    load.resolve([item])
    await ready(f.persistence)
    expect(f.attachments.value[0].name).toBe('new.txt')
    f.persistence.dispose()
  })

  it('preserves structured workspace identity without reconstituting a native capability', async () => {
    const workspace = { kind: 'workspace' as const, local_id: 2, name: 'project.txt', mime: 'text/plain',
      workspaceFile: { workspaceId: 'project-A', relativePath: 'project.txt', name: 'project.txt', mime: 'text/plain' } }
    const f = fixture({ load: vi.fn(async () => [workspace]) })
    await ready(f.persistence)
    expect(f.attachments.value[0]).toEqual(workspace)
    expect(f.attachments.value[0]).not.toHaveProperty('token')
    expect(f.attachments.value[0]).not.toHaveProperty('file')
    f.persistence.dispose()
  })

  it('reports actual storage failure once while keeping the visible attachments', async () => {
    const f = fixture({ save: vi.fn(async () => { throw new Error('Storage quota exceeded') }) })
    await ready(f.persistence)
    f.attachments.value = [item]
    await f.persistence.flush()
    f.attachments.value = [{ ...item, name: 'changed.txt' }]
    await f.persistence.flush()
    expect(f.onError).toHaveBeenCalledTimes(1)
    expect(f.attachments.value).toHaveLength(1)
    f.persistence.dispose()
  })
})
