import { createClientRequestId } from './messageIdentity'

interface PendingImplementation {
  clientRequestId: string
  targetSessionKey: string
}

const STORAGE_KEY = 'opensquilla.planImplementationRecovery.v1'
const MAX_PENDING = 32
const fallback = new Map<string, PendingImplementation>()
let memoryAuthoritative = false

function pendingRequests(): Map<string, PendingImplementation> {
  if (memoryAuthoritative) return fallback
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '[]')
    if (!Array.isArray(raw)) return fallback
    return new Map(raw.filter((entry): entry is [string, PendingImplementation] => {
      if (!Array.isArray(entry) || typeof entry[0] !== 'string') return false
      const value = entry[1]
      return value && typeof value.clientRequestId === 'string'
        && typeof value.targetSessionKey === 'string'
    }).slice(-MAX_PENDING))
  } catch {
    return fallback
  }
}

function save(requests: Map<string, PendingImplementation>) {
  while (requests.size > MAX_PENDING) requests.delete(requests.keys().next().value!)
  const entries = [...requests]
  fallback.clear()
  for (const [key, value] of entries) fallback.set(key, value)
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(entries))
  } catch {
    // The stored copy may now be stale, including a request we just forgot.
    // Keep memory authoritative until this module unloads. Recovery after a
    // reload requires a successful storage write.
    memoryAuthoritative = true
  }
}

export function planImplementationIdentity(
  sessionKey: string, epoch: number, revisionId: string, inNewSession: boolean,
): string {
  return JSON.stringify([sessionKey, epoch, revisionId, inNewSession])
}

export function recoverPlanImplementation(
  identity: string, createTargetSessionKey: () => string,
): PendingImplementation {
  const requests = pendingRequests()
  const previous = requests.get(identity)
  if (previous) return previous
  const pending = {
    clientRequestId: createClientRequestId(),
    targetSessionKey: createTargetSessionKey(),
  }
  requests.set(identity, pending)
  save(requests)
  return pending
}

export function forgetPlanImplementation(identity: string) {
  const requests = pendingRequests()
  requests.delete(identity)
  save(requests)
}
