import { describe, expect, it } from 'vitest'
import { canResumeAgent, parseAgentInstance } from '../src/agentResume'

const identity = { role: 'agent', deviceId: 'device_test', subject: 'owner', agentInstanceId: 'a'.repeat(43) }

describe('authenticated same-process reconnect', () => {
  it('accepts a same-instance reconnect regardless of adopted viewer session', () => {
    expect(canResumeAgent(identity, { ...identity })).toBe(true)
  })
  it.each([
    { agentInstanceId: 'b'.repeat(43) }, { agentInstanceId: undefined },
    { subject: 'other' }, { deviceId: 'other' }, { role: 'viewer' },
    { agentInstanceId: 'short' }, { agentInstanceId: '../'.repeat(20) },
  ])('does not allow a different identity or malformed nonce: %j', (change) => {
    expect(canResumeAgent(identity, { ...identity, ...change })).toBe(false)
  })
  it('does not replace legacy agents or absent/invalid attachments', () => {
    expect(canResumeAgent({ ...identity, agentInstanceId: undefined }, identity)).toBe(false)
    expect(canResumeAgent(null, identity)).toBe(false)
    expect(canResumeAgent({ ...identity, role: 'viewer' }, identity)).toBe(false)
  })
  it.each([null, undefined, 123, '', 'a'.repeat(31), 'a'.repeat(65), 'a '.repeat(20)])('rejects invalid format: %j', (value) => {
    expect(parseAgentInstance(value)).toBeUndefined()
  })
})
