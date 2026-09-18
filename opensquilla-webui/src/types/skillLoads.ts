import type { SelectedSkillRef } from './selectedSkills'

export interface SkillLoadReceipt extends SelectedSkillRef {
  source: 'user' | 'auto'
  status: 'loading' | 'loaded' | 'failed'
  error?: string
  turnId?: string
}

export function skillLoadReceipt(value: unknown): SkillLoadReceipt | null {
  if (!value || typeof value !== 'object') return null
  const data = value as Record<string, unknown>
  if (typeof data.name !== 'string' || typeof data.instanceId !== 'string'
    || typeof data.digest !== 'string' || !['user', 'auto'].includes(String(data.source))
    || !['loading', 'loaded', 'failed'].includes(String(data.status))) return null
  return { name: data.name, instanceId: data.instanceId, digest: data.digest,
    source: data.source as SkillLoadReceipt['source'], status: data.status as SkillLoadReceipt['status'],
    ...(typeof data.error === 'string' ? { error: data.error } : {}),
    ...(typeof data.turnId === 'string' ? { turnId: data.turnId } : {}) }
}

export function mergeSkillLoad(receipts: readonly SkillLoadReceipt[], next: SkillLoadReceipt): SkillLoadReceipt[] {
  const index = receipts.findIndex(item => item.name === next.name && item.instanceId === next.instanceId
    && item.digest === next.digest && item.source === next.source && item.turnId === next.turnId)
  if (index < 0) return [...receipts, next]
  if (next.status === 'loading' && receipts[index]?.status !== 'loading') return [...receipts]
  return receipts.map((item, position) => position === index ? next : item)
}

export function skillLoadsFromSegments(segments: readonly unknown[] = []): SkillLoadReceipt[] {
  return segments.reduce<SkillLoadReceipt[]>((receipts, segment) => {
    if (!segment || typeof segment !== 'object' || !('type' in segment) || segment.type !== 'skill_load') return receipts
    const receipt = skillLoadReceipt(segment)
    return receipt ? mergeSkillLoad(receipts, receipt) : receipts
  }, [])
}
