import { describe, expect, it } from 'vitest'
import { projectConversationEvent } from './conversationContentV4'
import { decodeConversationEvent } from './conversationEventsV4'

describe('actual Skill load event contract', () => {
  it('projects the engine content receipt while preserving outer task ownership', () => {
    const content = {
      name: 'report', instanceId: 'personal:report', digest: 'synthetic-digest',
      source: 'user', status: 'loaded', turnId: 'provider-turn',
    }
    const event = projectConversationEvent(decodeConversationEvent('session.event.skill_load', {
      key: 'agent:main:test', task_id: 'accepted-task', turn_id: 'provider-turn',
      stream_seq: 8, epoch: 2, content,
    }))
    expect(event).toMatchObject({
      kind: 'known', semanticKind: 'skill-load', taskId: 'accepted-task', turnId: 'provider-turn',
      payload: { skillLoad: content, task_id: 'accepted-task', turn_id: 'provider-turn' },
    })
  })
})
