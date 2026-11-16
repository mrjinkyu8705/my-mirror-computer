import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConnectionTimingProbe } from '../src/connectionTiming'
import { validateControlMessage } from '@mirror/protocol'

function stats(overrides: Record<string, unknown> = {}): RTCStatsReport {
  return new Map([['transport', { id: 'transport', type: 'transport', selectedCandidatePairId: 'pair' }],
    ['pair', { id: 'pair', type: 'candidate-pair', state: 'succeeded', responsesReceived: 10,
      currentRoundTripTime: .012, ...overrides }]]) as unknown as RTCStatsReport
}
afterEach(() => vi.unstubAllGlobals())

describe('separate connection timings', () => {
  it('tracks native RTT with unknown initial age, counter-observed freshness, and stale age', () => {
    const probe = new ConnectionTimingProbe()
    expect(probe.sample(stats(), 1000, 1)).toMatchObject({ iceRttMs: 12, iceState: 'unverified', iceAgeMs: null })
    expect(probe.sample(stats({ responsesReceived: 11 }), 2000, 1)).toMatchObject({ iceState: 'recent', iceAgeMs: 0 })
    expect(probe.sample(stats({ responsesReceived: 11 }), 18_000, 1)).toMatchObject({ iceState: 'stale', iceAgeMs: 16_000 })
    expect(probe.sample(stats({ responsesReceived: 11 }), 90_000, 1)).toMatchObject({ iceState: 'stale', iceAgeMs: null })
  })
  it('does not treat repeated values or unavailable response counters as a new response', () => {
    const probe = new ConnectionTimingProbe()
    probe.sample(stats(), 0, 1)
    expect(probe.sample(stats(), 1000, 1).iceState).toBe('unverified')
    expect(probe.sample(stats({ responsesReceived: undefined }), 2000, 1).iceAgeMs).toBeNull()
  })
  it('resets age on selected pair change or counter reset', () => {
    const probe = new ConnectionTimingProbe()
    probe.sample(stats(), 0, 1)
    probe.sample(stats({ responsesReceived: 11 }), 1000, 1)
    expect(probe.sample(stats({ responsesReceived: 1 }), 2000, 1).iceState).toBe('unverified')
    const changed = stats({ responsesReceived: 2 }) as unknown as Map<string, Record<string, unknown>>
    changed.get('transport')!.selectedCandidatePairId = 'next'
    changed.set('next', { ...changed.get('pair'), id: 'next' })
    expect(probe.sample(changed as unknown as RTCStatsReport, 3000, 1).iceState).toBe('unverified')
  })
  it('never selects arbitrary succeeded or ambiguous candidate pairs', () => {
    const probe = new ConnectionTimingProbe()
    const report = stats() as unknown as Map<string, Record<string, unknown>>
    report.delete('transport')
    expect(probe.sample(report as unknown as RTCStatsReport, 0, 1).iceRttMs).toBeNull()
    report.get('pair')!.nominated = true
    expect(probe.sample(report as unknown as RTCStatsReport, 0, 1).iceRttMs).toBe(12)
    report.set('other', { ...report.get('pair'), id: 'other' })
    expect(probe.sample(report as unknown as RTCStatsReport, 0, 1).iceRttMs).toBeNull()
    report.set('transport', { type: 'transport', selectedCandidatePairId: 'missing' })
    expect(probe.sample(report as unknown as RTCStatsReport, 0, 1).iceRttMs).toBeNull()
    report.set('transport2', { type: 'transport', selectedCandidatePairId: 'pair' })
    expect(probe.sample(report as unknown as RTCStatsReport, 0, 1).iceRttMs).toBeNull()
  })
  it.each([undefined, NaN, Infinity, -1, 61])('leaves missing/invalid native RTT unknown: %s', value => {
    expect(new ConnectionTimingProbe().sample(stats({ currentRoundTripTime: value }), 0, 1).iceRttMs).toBeNull()
  })
  it('accepts zero native RTT as a real measurement', () => {
    expect(new ConnectionTimingProbe().sample(stats({ currentRoundTripTime: 0 }), 0, 1).iceRttMs).toBe(0)
  })
  it('uses correlated monotonic app RTT without wall-clock subtraction or duplicates', () => {
    const probe = new ConnectionTimingProbe()
    probe.ping(1, 50000, 100)
    probe.pong(1, 50001, 650)
    expect(probe.sample(stats(), 650, 1).appRttMs).toBeNull()
    probe.pong(1, 50000, 650)
    probe.pong(1, 50000, 999)
    expect(probe.sample(stats(), 1000, 1)).toMatchObject({ iceRttMs: 12, appRttMs: 550, appAgeMs: 350 })
    expect(probe.sample(stats(), 62000, 1).appRttMs).toBeNull()
  })
  it('bounds pending probes and excludes expired pongs', () => {
    const probe = new ConnectionTimingProbe()
    for (let i = 0; i < 100; i++) probe.ping(i, i, i)
    probe.pong(0, 0, 200)
    expect(probe.sample(stats(), 200, 1).appRttMs).toBeNull()
    probe.pong(99, 99, 100000)
    expect(probe.sample(stats(), 100000, 1).appRttMs).toBeNull()
  })
  it('tracks timer lateness on existing ticks, not normal interval duration', () => {
    const probe = new ConnectionTimingProbe()
    probe.tick(100)
    expect(probe.sample(stats(), 100, 1).loopDelayMaxMs).toBeNull()
    probe.tick(1100)
    probe.tick(2650)
    expect(probe.sample(stats(), 2650, 1).loopDelayMaxMs).toBe(550)
    for (let now = 3650; now <= 8650; now += 1000) probe.tick(now)
    expect(probe.sample(stats(), 8650, 1).loopDelayMaxMs).toBe(0)
  })
  it('excludes background throttling and resets observations when returning', () => {
    const probe = new ConnectionTimingProbe()
    probe.tick(0); probe.tick(1600)
    probe.setVisibility(false, 1700)
    probe.tick(40000)
    expect(probe.sample(stats(), 40000, 1)).toMatchObject({ visibility: 'hidden', loopDelayMaxMs: null, longTaskCount: null })
    probe.setVisibility(true, 41000)
    probe.tick(41000)
    expect(probe.sample(stats(), 41000, 1).loopDelayMaxMs).toBeNull()
  })
  it('records feature-detected long tasks and disconnects its observer/listener', () => {
    let callback: PerformanceObserverCallback | undefined
    const disconnect = vi.fn(), observe = vi.fn()
    class Observer {
      static supportedEntryTypes = ['longtask']
      constructor(cb: PerformanceObserverCallback) { callback = cb }
      observe = observe
      disconnect = disconnect
    }
    vi.stubGlobal('PerformanceObserver', Observer)
    const remove = vi.spyOn(document, 'removeEventListener')
    const probe = new ConnectionTimingProbe()
    probe.start(); probe.setVisibility(true, 0)
    callback!({ getEntries: () => [{ startTime: 100, duration: 550 }] } as PerformanceObserverEntryList, {} as PerformanceObserver)
    expect(probe.sample(stats(), 1000, 1)).toMatchObject({ longTasksSupported: true, longTaskCount: 1, longTaskMaxMs: 550 })
    expect(probe.sample(stats(), 7000, 1).longTaskCount).toBe(0)
    probe.stop()
    expect(disconnect).toHaveBeenCalledTimes(1)
    expect(remove).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    remove.mockRestore()
  })
  it('leaves unsupported or failed observers unknown, not zero', () => {
    vi.stubGlobal('PerformanceObserver', undefined)
    const probe = new ConnectionTimingProbe(); probe.start()
    expect(probe.sample(stats(), performance.now(), 1)).toMatchObject({ longTasksSupported: false, longTaskCount: null, longTaskMaxMs: null })
    probe.stop()
  })
  it('emits a small strict diagnostic envelope with no candidate IDs or endpoints', () => {
    const data = new ConnectionTimingProbe().sample(stats(), 100, 1)
    const message = { data, event: 'video.timing-report', sequence: 1, sessionId: 'session_0123456789abcdef', timestamp: Date.now(), version: 1 }
    expect(validateControlMessage(message).ok).toBe(true)
    for (const extra of [{ url: 'secret' }, { iceRttMs: -1 }, { iceRttMs: Infinity }, { visibility: 'url' }, { longTaskCount: 129 }])
      expect(validateControlMessage({ ...message, data: { ...data, ...extra } }).ok).toBe(false)
    expect(JSON.stringify(data)).not.toContain('pair')
    expect(JSON.stringify(message).length).toBeLessThan(650)
  })
})
