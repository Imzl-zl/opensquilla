import { createClientRequestId } from './messageIdentity'
import type { GoalExecutionOptions } from '@/modules/goalCenter'

interface PendingGoalSet {
  clientRequestId: string
  clientMessageId: string
}

const STORAGE_KEY = 'opensquilla.goalSetRecovery.v1'
const MAX_PENDING = 32
const fallback = new Map<string, PendingGoalSet>()
let memoryAuthoritative = false

function pendingRequests(): Map<string, PendingGoalSet> {
  if (memoryAuthoritative) return fallback
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '[]')
    if (!Array.isArray(raw)) return fallback
    return new Map(raw.filter((entry): entry is [string, PendingGoalSet] => {
      if (!Array.isArray(entry) || typeof entry[0] !== 'string') return false
      const value = entry[1]
      return value && typeof value.clientRequestId === 'string'
        && typeof value.clientMessageId === 'string'
    }).slice(-MAX_PENDING))
  } catch {
    return fallback
  }
}

function save(requests: Map<string, PendingGoalSet>) {
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

export function goalSetIdentity(
  sessionKey: string, epoch: number, objective: string, options: GoalExecutionOptions,
): string {
  return JSON.stringify([
    sessionKey, epoch, objective,
    options.tokenBudget ?? null, options.executionPolicy ?? 'foreground',
  ])
}

export function recoverGoalSet(identity: string): PendingGoalSet {
  const requests = pendingRequests()
  const previous = requests.get(identity)
  if (previous) return previous
  const pending = { clientRequestId: createClientRequestId(), clientMessageId: createClientRequestId() }
  requests.set(identity, pending)
  save(requests)
  return pending
}

export function forgetGoalSet(identity: string) {
  const requests = pendingRequests()
  requests.delete(identity)
  save(requests)
}

export function forgetGoalSetsForSession(sessionKey: string) {
  const requests = pendingRequests()
  for (const identity of requests.keys()) {
    try {
      if (JSON.parse(identity)[0] === sessionKey) requests.delete(identity)
    } catch {
      requests.delete(identity)
    }
  }
  save(requests)
}
