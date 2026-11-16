import { describe, expect, it } from 'vitest'
import { computeLatencyDiagnostics } from '../src/latencyMetrics'

describe('latency diagnostics', () => {
  const a = { streamId: 'a', framesDecoded: 10, totalDecodeTime: 0.05,
    jitterBufferDelay: 0.3, jitterBufferEmittedCount: 10, framesDropped: 1 }
  const b = { ...a, framesDecoded: 20, totalDecodeTime: 0.09,
    jitterBufferDelay: 0.8, jitterBufferEmittedCount: 20, framesDropped: 3 }
  it('uses interval averages, seconds to milliseconds, and queue bytes separately', () => {
    expect(computeLatencyDiagnostics(a, b, 123, 5000)).toEqual({
      bufferMs: 50, decodeMs: 4, droppedFrames: 2, controlBufferedBytes: 123, controlPeakBytes: 5000,
    })
  })
  it('does not invent delays for missing stats, the first sample, resets or a replaced stream', () => {
    for (const value of [computeLatencyDiagnostics(null, a, 0, 0),
      computeLatencyDiagnostics(b, a, 0, 0),
      computeLatencyDiagnostics(a, { ...b, streamId: 'b' }, 0, 0),
      computeLatencyDiagnostics({ framesDecoded: 1 }, { framesDecoded: 2 }, 0, 0)]) {
      expect(value.bufferMs).toBeNull()
      expect(value.decodeMs).toBeNull()
      expect(value.droppedFrames).toBeNull()
    }
  })
})
