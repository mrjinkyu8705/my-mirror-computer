// Optional process-lifetime nonce; never an authentication credential. Only
// already-authenticated agents with the same device AND subject may resume.
export function parseAgentInstance(value: unknown): string | undefined {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{32,64}$/.test(value)
    ? value
    : undefined
}

interface AgentIdentity {
  readonly role: string
  readonly deviceId: string
  readonly subject: string
  readonly agentInstanceId?: string | undefined
}

export function canResumeAgent(previous: AgentIdentity | null, next: AgentIdentity): boolean {
  const instance = parseAgentInstance(next.agentInstanceId)
  return previous !== null && next.role === 'agent' && previous.role === 'agent' &&
    next.deviceId === previous.deviceId && next.subject === previous.subject &&
    instance !== undefined && instance === parseAgentInstance(previous.agentInstanceId)
}
