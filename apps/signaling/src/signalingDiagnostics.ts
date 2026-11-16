/** Ephemeral per-socket correlation, never device/session/auth identities. */
export function parseSignalingLink(value: unknown): string | undefined {
  return typeof value === 'string' && /^[a-f0-9]{16}$/u.test(value) ? value : undefined
}

export function createSignalingLink(value: unknown): string {
  return parseSignalingLink(value) ?? crypto.randomUUID().replaceAll('-', '').slice(0, 16)
}

interface LifecycleState {
  readonly role: 'agent' | 'viewer'
  readonly connectionId?: string | undefined
  readonly connectedAt: number
  readonly lastSeenAt: number
  readonly retired?: boolean | undefined
}

/** Fixed labels and bounded numeric values only. No close reason or payload. */
export function signalingLifecycle(
  state: LifecycleState,
  event: 'joined' | 'closed' | 'error' | 'heartbeat_timeout',
  now: number,
  code?: number,
): string {
  const age = (at: number) => Number.isFinite(now) && Number.isFinite(at)
    ? Math.max(0, Math.min(Number.MAX_SAFE_INTEGER, Math.trunc(now - at))) : 0
  const safeCode = typeof code === 'number' && Number.isInteger(code) && code >= 1000 && code <= 4999 ? code : -1
  return `signaling role=${state.role} event=${event} link=${parseSignalingLink(state.connectionId) ?? 'legacy'} age_ms=${age(state.connectedAt)} idle_ms=${age(state.lastSeenAt)} code=${safeCode} retired=${Boolean(state.retired)}`
}
