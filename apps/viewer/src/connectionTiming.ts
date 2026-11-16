import type { ControlMessage } from '@mirror/protocol'

export type ConnectionTiming = Extract<ControlMessage, { event: 'video.timing-report' }>['data']
type Stats = { id?: string; type?: string; selectedCandidatePairId?: string; state?: string;
  selected?: boolean; nominated?: boolean; currentRoundTripTime?: number; responsesReceived?: number }
const bounded = (value: unknown, max = 60_000): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= max
    ? Math.round(value * 10) / 10 : null

// Native STUN RTT and JS scheduling are independent observations, not an
// additive decomposition of a single packet. Never subtract them or feed ABR.
export class ConnectionTimingProbe {
  private pair: string | null = null
  private responses: number | null = null
  private iceUpdatedAt: number | null = null
  private lastTick: number | null = null
  private loop: { at: number; delay: number }[] = []
  private tasks: { at: number; duration: number }[] = []
  private pending = new Map<number, { at: number; timestamp: number }>()
  private app: { at: number; rtt: number } | null = null
  private observer: PerformanceObserver | null = null
  private longTasksSupported = false
  private visible = true
  private visibleSince = 0

  start(): void {
    this.visible = document.visibilityState !== 'hidden'
    this.visibleSince = performance.now()
    document.addEventListener('visibilitychange', this.visibilityChanged)
    try {
      if (typeof PerformanceObserver !== 'undefined' && PerformanceObserver.supportedEntryTypes?.includes('longtask')) {
        this.observer = new PerformanceObserver(list => {
          for (const task of list.getEntries()) this.recordLongTask(task.startTime, task.duration)
        })
        this.observer.observe({ type: 'longtask', buffered: false })
        this.longTasksSupported = true
      }
    } catch { this.observer?.disconnect(); this.observer = null }
  }

  stop(): void {
    this.observer?.disconnect()
    this.observer = null
    document.removeEventListener('visibilitychange', this.visibilityChanged)
    this.pending.clear()
  }

  private visibilityChanged = (): void => {
    this.setVisibility(document.visibilityState !== 'hidden', performance.now())
  }

  setVisibility(visible: boolean, now: number): void {
    this.visible = visible
    this.visibleSince = now
    this.lastTick = null
    this.loop = []
    this.tasks = []
    this.app = null
    this.pending.clear()
  }

  tick(now: number, interval = 1000): void {
    if (this.visible && this.lastTick !== null) {
      const delay = bounded(Math.max(0, now - this.lastTick - interval))
      if (delay !== null) this.loop.push({ at: now, delay })
    }
    this.lastTick = now
    this.loop = this.loop.filter(x => now - x.at <= 5000).slice(-32)
  }

  recordLongTask(start: number, duration: number): void {
    if (!this.visible || start < this.visibleSince || bounded(duration) === null || duration < 50) return
    this.tasks.push({ at: start + duration, duration })
    this.tasks = this.tasks.slice(-128)
  }

  ping(sequence: number, timestamp: number, now: number): void {
    for (const [key, value] of this.pending) if (now - value.at > 60_000) this.pending.delete(key)
    this.pending.set(sequence, { at: now, timestamp })
    while (this.pending.size > 16) this.pending.delete(this.pending.keys().next().value!)
  }

  pong(sequence: number, timestamp: number, now: number): void {
    const before = this.pending.get(sequence)
    if (!before || before.timestamp !== timestamp) return
    this.pending.delete(sequence)
    const rtt = bounded(now - before.at)
    if (rtt !== null) this.app = { at: now, rtt }
  }

  sample(report: RTCStatsReport, now: number, statsAwaitMs: number): ConnectionTiming {
    const records: Stats[] = []
    report.forEach(value => records.push(value as Stats))
    const ids = [...new Set(records.filter(x => x.type === 'transport' && x.selectedCandidatePairId)
      .map(x => x.selectedCandidatePairId!))]
    const pairs = records.filter(x => x.type === 'candidate-pair' && x.state === 'succeeded')
    const selected = pairs.filter(x => x.selected)
    const nominated = pairs.filter(x => x.nominated)
    // Ambiguous transports/pairs remain unknown; never use any succeeded pair.
    const pair = ids.length === 1 ? pairs.find(x => x.id === ids[0])
      : ids.length > 1 ? undefined : selected.length === 1 ? selected[0]
      : selected.length > 1 ? undefined : nominated.length === 1 ? nominated[0] : undefined
    const responseCount = Number.isSafeInteger(pair?.responsesReceived) && pair!.responsesReceived! >= 0
      ? pair!.responsesReceived! : null
    if ((pair?.id ?? null) !== this.pair || (responseCount !== null && this.responses !== null && responseCount < this.responses)) {
      this.pair = pair?.id ?? null
      this.responses = responseCount
      this.iceUpdatedAt = null
    } else if (responseCount !== null && this.responses !== null && responseCount > this.responses) {
      this.iceUpdatedAt = now
    } else if (responseCount === null || this.responses === null) {
      this.iceUpdatedAt = null
    }
    this.responses = responseCount
    const iceRttMs = pair?.id ? bounded(typeof pair.currentRoundTripTime === 'number' ? pair.currentRoundTripTime * 1000 : null) : null
    const age = this.iceUpdatedAt === null ? null : Math.max(0, now - this.iceUpdatedAt)
    const appAge = this.app ? bounded(now - this.app.at) : null
    const tasks = this.tasks.filter(x => now >= x.at && now - x.at <= 5000)
    this.tasks = tasks
    return {
      iceRttMs,
      iceAgeMs: age === null ? null : bounded(age),
      iceState: iceRttMs === null ? 'unavailable' : age === null ? 'unverified' : age <= 15_000 ? 'recent' : 'stale',
      appRttMs: appAge === null ? null : this.app!.rtt,
      appAgeMs: appAge,
      loopDelayMaxMs: this.visible && this.loop.length ? Math.max(...this.loop.map(x => x.delay)) : null,
      statsAwaitMs: bounded(statsAwaitMs),
      longTasksSupported: this.longTasksSupported,
      longTaskCount: this.visible && this.longTasksSupported ? tasks.length : null,
      longTaskMaxMs: this.visible && this.longTasksSupported ? Math.max(0, ...tasks.map(x => x.duration)) : null,
      visibility: this.visible ? 'visible' : 'hidden',
    }
  }
}
