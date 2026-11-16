export interface LatencySample {
  readonly streamId?: string
  readonly framesDecoded: number
  readonly jitterBufferDelay?: number | null
  readonly jitterBufferEmittedCount?: number | null
  readonly totalDecodeTime?: number | null
  readonly framesDropped?: number | null
}

export interface LatencyDiagnostics {
  readonly bufferMs: number | null
  readonly decodeMs: number | null
  readonly droppedFrames: number | null
  readonly controlBufferedBytes: number
  readonly controlPeakBytes: number
}

function delta(a: number | null | undefined, b: number | null | undefined): number | null {
  return typeof a === 'number' && typeof b === 'number' &&
    Number.isFinite(a) && Number.isFinite(b) && b >= a ? b - a : null
}

export function computeLatencyDiagnostics(
  previous: LatencySample | null,
  current: LatencySample,
  controlBufferedBytes: number,
  controlPeakBytes: number,
): LatencyDiagnostics {
  const prior = previous?.streamId === current.streamId ? previous : null
  const emitted = delta(prior?.jitterBufferEmittedCount, current.jitterBufferEmittedCount)
  const buffered = delta(prior?.jitterBufferDelay, current.jitterBufferDelay)
  const decoded = delta(prior?.framesDecoded, current.framesDecoded)
  const decodeTime = delta(prior?.totalDecodeTime, current.totalDecodeTime)
  const averageMs = (seconds: number | null, count: number | null) =>
    seconds !== null && count !== null && count > 0
      ? Math.round(seconds * 1000 / count * 10) / 10 : null
  return {
    bufferMs: averageMs(buffered, emitted),
    decodeMs: averageMs(decodeTime, decoded),
    droppedFrames: delta(prior?.framesDropped, current.framesDropped),
    controlBufferedBytes,
    controlPeakBytes,
  }
}
