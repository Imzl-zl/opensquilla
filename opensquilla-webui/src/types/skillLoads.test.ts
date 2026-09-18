import { describe, expect, it } from 'vitest'
import { mergeSkillLoad, skillLoadsFromSegments, type SkillLoadReceipt } from './skillLoads'

const loaded: SkillLoadReceipt = {
  name: 'report', instanceId: 'personal:report', digest: 'synthetic-digest',
  source: 'user', status: 'loaded', turnId: 'turn-one',
}

describe('Skill load receipts', () => {
  it('deduplicates replay without regressing loaded status to loading', () => {
    expect(mergeSkillLoad([loaded], { ...loaded, status: 'loading' })).toEqual([loaded])
    expect(mergeSkillLoad([loaded], loaded)).toEqual([loaded])
  })
  it('retains distinct turn and content identities', () => {
    const next = { ...loaded, status: 'loading' as const, turnId: 'turn-two' }
    expect(mergeSkillLoad([loaded], next)).toEqual([loaded, next])
    expect(mergeSkillLoad([loaded], { ...loaded, digest: 'replacement' })).toHaveLength(2)
  })
  it('reads only actual receipts from historical segments', () => {
    expect(skillLoadsFromSegments([
      { type: 'tool_use', name: 'skill_view', input: { name: 'report' } },
      { type: 'skill_load', ...loaded, status: 'loading' },
      { type: 'skill_load', ...loaded },
      { type: 'skill_load', name: 'broken' },
    ])).toEqual([loaded])
  })
})
