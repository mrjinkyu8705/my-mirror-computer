import { describe, expect, it } from 'vitest'

import {
  computeVideoTelemetry,
  formatSessionDuration,
  getConnectionQuality,
  getDisplayModeLabel,
  readConnectionRoute,
  type VideoStatsSample,
} from '../src/viewerMetrics'

function sample(overrides: Partial<VideoStatsSample> = {}): VideoStatsSample {
  return {
    bytesReceived: 1_000_000,
    framesDecoded: 100,
    framesPerSecond: null,
    frameHeight: 900,
    frameWidth: 1600,
    jitterSeconds: 0.012,
    packetsLost: 2,
    packetsReceived: 998,
    qpSum: 2_800,
    timestampMs: 1_000,
    ...overrides,
  }
}

describe('getConnectionQuality', () => {
  it('uses conservative RTT-only labels', () => {
    expect(getConnectionQuality(null)).toEqual({ label: '측정 중', tone: 'neutral' })
    expect(getConnectionQuality(80)).toEqual({ label: '원활', tone: 'good' })
    expect(getConnectionQuality(180)).toEqual({ label: '보통', tone: 'fair' })
    expect(getConnectionQuality(181)).toEqual({ label: '지연', tone: 'poor' })
  })

  it('surfaces decoder quality and loss ahead of RTT', () => {
    expect(
      getConnectionQuality(20, {
        averageQp: 40,
        decodedFramesPerSecond: 15,
        frameHeight: 900,
        frameWidth: 1600,
        jitterMs: 10,
        packetLossPercent: 0,
        receivedBitrateMbps: 0.5,
      }).tone,
    ).toBe('poor')
  })
})

describe('computeVideoTelemetry', () => {
  it('derives bitrate, loss, jitter, fps, and decoder QP from deltas', () => {
    const telemetry = computeVideoTelemetry(
      sample(),
      sample({
        bytesReceived: 1_500_000,
        framesDecoded: 130,
        jitterSeconds: 0.025,
        packetsLost: 5,
        packetsReceived: 1_295,
        qpSum: 3_820,
        timestampMs: 3_000,
      }),
    )

    expect(telemetry).toEqual({
      averageQp: 34,
      decodedFramesPerSecond: 15,
      frameHeight: 900,
      frameWidth: 1600,
      jitterMs: 25,
      packetLossPercent: 1,
      receivedBitrateMbps: 2,
    })
  })

  it('does not invent rate deltas for the first sample', () => {
    expect(computeVideoTelemetry(null, sample())).toMatchObject({
      averageQp: null,
      packetLossPercent: null,
      receivedBitrateMbps: null,
    })
  })
})

describe('readConnectionRoute', () => {
  function report(entries: readonly Record<string, unknown>[]): RTCStatsReport {
    return {
      forEach: (callback: (value: RTCStats) => void) => {
        entries.forEach((entry) => callback(entry as unknown as RTCStats))
      },
    } as RTCStatsReport
  }

  it('reports the selected direct UDP candidate pair', () => {
    expect(
      readConnectionRoute(
        report([
          { id: 'transport', selectedCandidatePairId: 'pair', type: 'transport' },
          {
            id: 'pair',
            localCandidateId: 'local',
            remoteCandidateId: 'remote',
            state: 'succeeded',
            type: 'candidate-pair',
          },
          { candidateType: 'host', id: 'local', protocol: 'udp', type: 'local-candidate' },
          { candidateType: 'srflx', id: 'remote', protocol: 'udp', type: 'remote-candidate' },
        ]),
      ),
    ).toEqual({ protocol: 'udp', route: 'direct' })
  })

  it('identifies a TURN relay on either side of the selected pair', () => {
    expect(
      readConnectionRoute(
        report([
          {
            id: 'pair',
            localCandidateId: 'local',
            nominated: true,
            remoteCandidateId: 'remote',
            state: 'succeeded',
            type: 'candidate-pair',
          },
          { candidateType: 'relay', id: 'local', protocol: 'tcp', type: 'local-candidate' },
          { candidateType: 'host', id: 'remote', protocol: 'udp', type: 'remote-candidate' },
        ]),
      ),
    ).toEqual({ protocol: 'tcp', route: 'turn' })
  })
})

describe('formatSessionDuration', () => {
  it('formats elapsed seconds without locale-dependent output', () => {
    expect(formatSessionDuration(0)).toBe('00:00')
    expect(formatSessionDuration(65)).toBe('01:05')
    expect(formatSessionDuration(3_661)).toBe('01:01:01')
  })

  it('clamps invalid and negative durations', () => {
    expect(formatSessionDuration(-1)).toBe('00:00')
    expect(formatSessionDuration(Number.NaN)).toBe('00:00')
  })
})

describe('getDisplayModeLabel', () => {
  it('describes the action rather than the current mode', () => {
    expect(getDisplayModeLabel('fit')).toBe('원본 크기')
    expect(getDisplayModeLabel('actual')).toBe('화면 맞춤')
  })
})
