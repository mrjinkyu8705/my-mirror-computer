import type { LatencyDiagnostics, LatencySample } from './latencyMetrics'
import type { ConnectionTiming } from './connectionTiming'

export type DisplayMode = 'fit' | 'actual'
export type VideoProfile = 'low' | 'balanced' | 'high'
export type ConnectionRoute = 'direct' | 'turn' | 'unknown'
export type QualityTone = 'neutral' | 'good' | 'fair' | 'poor'

export interface ConnectionQuality {
  readonly label: string
  readonly tone: QualityTone
}

export interface VideoStatsSample extends LatencySample {
  readonly bytesReceived: number
  readonly framesDecoded: number
  readonly framesPerSecond: number | null
  readonly frameHeight: number | null
  readonly frameWidth: number | null
  readonly jitterSeconds: number | null
  readonly packetsLost: number | null
  readonly packetsReceived: number | null
  readonly qpSum: number | null
  readonly timestampMs: number
}

export interface VideoTelemetry {
  readonly timing?: ConnectionTiming
  readonly delivery?: DeliverySample
  readonly diagnostics?: LatencyDiagnostics
  readonly averageQp: number | null
  readonly decodedFramesPerSecond: number | null
  readonly frameHeight: number | null
  readonly frameWidth: number | null
  readonly jitterMs: number | null
  readonly packetLossPercent: number | null
  readonly receivedBitrateMbps: number | null
}

export interface DeliverySample {
  readonly intervalMs: number
  readonly receivedPackets: number | null
  readonly lostPackets: number | null
  readonly recoveredPackets?: number | null
  readonly receivedBytes?: number | null
  readonly controlRttMs?: number | null
  readonly bufferMs: number | null
  readonly decodeMs: number | null
  readonly decodedFrames: number | null
  readonly droppedFrames: number | null
}

export interface ConnectionRouteTelemetry {
  readonly protocol: string | null
  readonly route: ConnectionRoute
  readonly path?: IcePathDiagnostics
}

export interface IcePathDiagnostics {
  readonly localType: 'host' | 'srflx' | 'prflx' | 'relay' | 'unknown'
  readonly remoteType: IcePathDiagnostics['localType']
  readonly candidateProtocol: 'udp' | 'tcp' | 'unknown'
  readonly relayProtocol: 'udp' | 'tcp' | 'tls' | 'unknown' | 'not-used'
}

interface IceStatsRecord {
  readonly candidateType?: string
  readonly id?: string
  readonly localCandidateId?: string
  readonly nominated?: boolean
  readonly protocol?: string
  readonly relayProtocol?: string
  readonly remoteCandidateId?: string
  readonly selected?: boolean
  readonly selectedCandidatePairId?: string
  readonly state?: string
  readonly type?: string
}

export const EMPTY_VIDEO_TELEMETRY: VideoTelemetry = Object.freeze({
  averageQp: null,
  decodedFramesPerSecond: null,
  frameHeight: null,
  frameWidth: null,
  jitterMs: null,
  packetLossPercent: null,
  receivedBitrateMbps: null,
})

export const EMPTY_CONNECTION_ROUTE: ConnectionRouteTelemetry = Object.freeze({
  protocol: null,
  route: 'unknown',
})

export function readConnectionRoute(
  report: Pick<RTCStatsReport, 'forEach'>,
): ConnectionRouteTelemetry {
  const entries = new Map<string, IceStatsRecord>()
  let transportSelectedPairId: string | null = null
  const candidatePairs: IceStatsRecord[] = []

  report.forEach((entry: RTCStats) => {
    const record = entry as IceStatsRecord
    if (record.id) {
      entries.set(record.id, record)
    }
    if (record.type === 'transport' && record.selectedCandidatePairId) {
      transportSelectedPairId = record.selectedCandidatePairId
    } else if (record.type === 'candidate-pair') {
      candidatePairs.push(record)
    }
  })

  const selectedPair =
    (transportSelectedPairId
      ? entries.get(transportSelectedPairId)
      : undefined) ??
    candidatePairs.find((pair) => pair.selected === true) ??
    candidatePairs.find(
      (pair) => pair.nominated === true && pair.state === 'succeeded',
    ) ??
    candidatePairs.find((pair) => pair.state === 'succeeded')

  if (!selectedPair?.localCandidateId || !selectedPair.remoteCandidateId) {
    return EMPTY_CONNECTION_ROUTE
  }
  const localCandidate = entries.get(selectedPair.localCandidateId)
  const remoteCandidate = entries.get(selectedPair.remoteCandidateId)
  if (!localCandidate || !remoteCandidate) {
    return EMPTY_CONNECTION_ROUTE
  }
  const confirmedPair = selectedPair.id === transportSelectedPairId || selectedPair.selected === true ||
    (selectedPair.nominated === true && selectedPair.state === 'succeeded')

  return {
    protocol: localCandidate.protocol ?? remoteCandidate.protocol ?? null,
    // relayProtocol describes ONLY this browser's connection to its TURN
    // server. The remote PC's TURN leg cannot be inferred from ICE protocol.
    ...(confirmedPair ? { path: {
      localType: candidateType(localCandidate.candidateType),
      remoteType: candidateType(remoteCandidate.candidateType),
      candidateProtocol: transportProtocol(localCandidate.protocol),
      relayProtocol: localCandidate.candidateType === 'relay'
        ? relayProtocol(localCandidate.relayProtocol) : 'not-used',
    } } : {}),
    route:
      localCandidate.candidateType === 'relay' ||
      remoteCandidate.candidateType === 'relay'
        ? 'turn'
        : 'direct',
  }
}

function candidateType(value: unknown): IcePathDiagnostics['localType'] {
  return value === 'host' || value === 'srflx' || value === 'prflx' || value === 'relay'
    ? value : 'unknown'
}

function transportProtocol(value: unknown): IcePathDiagnostics['candidateProtocol'] {
  return value === 'udp' || value === 'tcp' ? value : 'unknown'
}

function relayProtocol(value: unknown): IcePathDiagnostics['relayProtocol'] {
  return value === 'udp' || value === 'tcp' || value === 'tls' ? value : 'unknown'
}

function rounded(value: number, digits: number): number {
  const scale = 10 ** digits
  return Math.round(value * scale) / scale
}

export function computeVideoTelemetry(
  previous: VideoStatsSample | null,
  current: VideoStatsSample,
): VideoTelemetry {
  // Counter differences never cross a new stream or a cached/backward sample.
  if (previous?.streamId !== current.streamId) previous = null
  const elapsedSeconds = previous
    ? (current.timestampMs - previous.timestampMs) / 1_000
    : 0
  const bytesDelta = previous ? current.bytesReceived - previous.bytesReceived : -1
  const receivedDelta = previous && previous.packetsReceived !== null && current.packetsReceived !== null
    ? current.packetsReceived - previous.packetsReceived
    : -1
  const lostDelta = previous && previous.packetsLost !== null && current.packetsLost !== null
    ? current.packetsLost - previous.packetsLost : -1
  const frameDelta = previous ? current.framesDecoded - previous.framesDecoded : -1
  const qpDelta =
    previous && current.qpSum !== null && previous.qpSum !== null
      ? current.qpSum - previous.qpSum
      : -1
  const packetDelta = receivedDelta + lostDelta

  return {
    averageQp:
      frameDelta > 0 && qpDelta >= 0 ? rounded(qpDelta / frameDelta, 1) : null,
    decodedFramesPerSecond:
      current.framesPerSecond !== null
        ? rounded(current.framesPerSecond, 1)
        : elapsedSeconds > 0 && frameDelta >= 0
          ? rounded(frameDelta / elapsedSeconds, 1)
          : null,
    frameHeight: current.frameHeight,
    frameWidth: current.frameWidth,
    jitterMs:
      current.jitterSeconds !== null
        ? rounded(current.jitterSeconds * 1_000, 1)
        : null,
    packetLossPercent:
      elapsedSeconds > 0 && packetDelta > 0 && receivedDelta >= 0 && lostDelta >= 0
        ? rounded((lostDelta / packetDelta) * 100, 1)
        : null,
    receivedBitrateMbps:
      elapsedSeconds > 0 && bytesDelta >= 0
        ? rounded((bytesDelta * 8) / elapsedSeconds / 1_000_000, 2)
        : null,
  }
}

/** Bounded count deltas, including late loss-counter corrections.
 * A correction is usable only with monotonic received/byte counters and cannot
 * exceed packets received in that interval. Missing/reset data stays unknown.
 */
export function computeDeliverySample(
  previous: VideoStatsSample | null, current: VideoStatsSample, diagnostics: LatencyDiagnostics,
  controlRttMs: number | null = null,
): DeliverySample | undefined {
  if (!previous || previous.streamId !== current.streamId) return undefined
  const intervalMs = current.timestampMs - previous.timestampMs
  if (!Number.isFinite(intervalMs) || intervalMs < 250 || intervalMs > 3_000) return undefined
  const countDelta = (a: number | null | undefined, b: number | null | undefined, max: number) =>
    typeof a === 'number' && typeof b === 'number' && Number.isSafeInteger(a) && Number.isSafeInteger(b) &&
    b >= a && b - a <= max ? b - a : null
  const bounded = (value: number | null) => value !== null && Number.isFinite(value) && value >= 0 && value <= 60_000 ? value : null
  const receivedPackets = countDelta(previous.packetsReceived, current.packetsReceived, 1_000_000)
  const receivedBytes = countDelta(previous.bytesReceived, current.bytesReceived, 67_108_864)
  const lostDelta = typeof previous.packetsLost === 'number' && typeof current.packetsLost === 'number' &&
    Number.isSafeInteger(previous.packetsLost) && Number.isSafeInteger(current.packetsLost)
    ? current.packetsLost - previous.packetsLost : null
  const correction = lostDelta !== null && lostDelta < 0 && receivedPackets !== null &&
    receivedBytes !== null && -lostDelta <= receivedPackets && -lostDelta <= 1_000_000
    ? -lostDelta : null
  const lostPackets = correction !== null ? 0 : countDelta(previous.packetsLost, current.packetsLost, 1_000_000)
  return { intervalMs,
    receivedPackets, lostPackets,
    recoveredPackets: correction ?? (lostPackets !== null ? 0 : null),
    receivedBytes, controlRttMs: bounded(controlRttMs),
    bufferMs: bounded(diagnostics.bufferMs), decodeMs: bounded(diagnostics.decodeMs),
    decodedFrames: countDelta(previous.framesDecoded, current.framesDecoded, 10_000),
    droppedFrames: countDelta(previous.framesDropped, current.framesDropped, 10_000),
  }
}

export function getConnectionQuality(
  roundTripTimeMs: number | null,
  video: VideoTelemetry = EMPTY_VIDEO_TELEMETRY,
): ConnectionQuality {
  if (
    (video.packetLossPercent !== null && video.packetLossPercent >= 5) ||
    (video.averageQp !== null && video.averageQp >= 38)
  ) {
    return { label: '화질 저하', tone: 'poor' }
  }
  if (
    (video.packetLossPercent !== null && video.packetLossPercent >= 1) ||
    (video.jitterMs !== null && video.jitterMs >= 30) ||
    (video.averageQp !== null && video.averageQp >= 32)
  ) {
    return { label: '불안정', tone: 'fair' }
  }
  if (roundTripTimeMs === null || !Number.isFinite(roundTripTimeMs)) {
    return { label: '측정 중', tone: 'neutral' }
  }
  if (roundTripTimeMs <= 80) {
    return { label: '원활', tone: 'good' }
  }
  if (roundTripTimeMs <= 180) {
    return { label: '보통', tone: 'fair' }
  }
  return { label: '지연', tone: 'poor' }
}

export function formatSessionDuration(elapsedSeconds: number): string {
  const safeSeconds = Number.isFinite(elapsedSeconds)
    ? Math.max(0, Math.floor(elapsedSeconds))
    : 0
  const hours = Math.floor(safeSeconds / 3_600)
  const minutes = Math.floor((safeSeconds % 3_600) / 60)
  const seconds = safeSeconds % 60
  const minuteSecond = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  return hours > 0
    ? `${String(hours).padStart(2, '0')}:${minuteSecond}`
    : minuteSecond
}

export function getDisplayModeLabel(mode: DisplayMode): string {
  return mode === 'fit' ? '원본 크기' : '화면 맞춤'
}
