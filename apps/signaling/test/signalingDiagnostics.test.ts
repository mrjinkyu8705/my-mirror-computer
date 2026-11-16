import { describe, expect, it } from 'vitest'
import { createSignalingLink, parseSignalingLink, signalingLifecycle } from '../src/signalingDiagnostics'

describe('privacy-safe signaling lifecycle', () => {
  it('shares only a validated per-socket id, not an identity or authentication value', () => {
    const id = '12345678abcdef00'
    expect(parseSignalingLink(id)).toBe(id)
    expect(createSignalingLink(id)).toBe(id)
    expect(createSignalingLink(undefined)).toMatch(/^[a-f0-9]{16}$/u)
    expect(createSignalingLink(undefined)).not.toBe(createSignalingLink(undefined))
  })
  it.each([undefined, null, 123, '', 'short', 'A'.repeat(16), 'x'.repeat(16),
    'https://secret/?ticket=secret', 'a'.repeat(16)+'\n', 'a'.repeat(500)])('rejects untrusted correlation %j', (value) => {
    expect(parseSignalingLink(value)).toBeUndefined()
  })
  it('emits role, ages, code and retirement state without other attachment fields', () => {
    const state = { role: 'agent' as const, connectionId: '12345678abcdef00', connectedAt: 1000,
      lastSeenAt: 9500, retired: false, deviceId: 'secret-device', subject: 'secret-user', sessionId: 'secret-session' }
    const log = signalingLifecycle(state, 'closed', 10000, 1006)
    expect(log).toBe('signaling role=agent event=closed link=12345678abcdef00 age_ms=9000 idle_ms=500 code=1006 retired=false')
    expect(log).not.toContain('secret')
  })
  it('supports legacy hibernated attachments and clamps invalid numeric diagnostics', () => {
    const state = { role: 'viewer' as const, connectionId: 'secret-url', connectedAt: Infinity, lastSeenAt: 5000 }
    expect(signalingLifecycle(state, 'error', 1000, NaN)).toBe(
      'signaling role=viewer event=error link=legacy age_ms=0 idle_ms=0 code=-1 retired=false')
  })
})
